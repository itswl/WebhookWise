import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import tomllib
import yaml

ROOT = Path(__file__).resolve().parents[1]


def _yaml_documents(path: str) -> list[dict[str, Any]]:
    return [doc for doc in yaml.safe_load_all((ROOT / path).read_text()) if isinstance(doc, dict)]


def _walk_images(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        if isinstance(value.get("image"), str):
            yield value["image"]
        for item in value.values():
            yield from _walk_images(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_images(item)


def _image_is_latest(image: str) -> bool:
    tag = image.rsplit("/", 1)[-1].rsplit(":", 1)
    return len(tag) == 1 or tag[-1] == "latest"


def test_ci_enforces_coverage_gate() -> None:
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())

    assert "--cov=core" in ci
    assert "--cov=api" in ci
    assert "--cov=services" in ci
    assert "--cov-report=xml" in ci
    assert "--cov-fail-under=63" in ci
    assert pyproject["tool"]["coverage"]["report"]["fail_under"] == 63


def test_dockerfile_uses_directory_copy_contract() -> None:
    lines = (ROOT / "Dockerfile").read_text().splitlines()
    copy_lines = [line.strip() for line in lines if line.strip().startswith("COPY ")]

    assert not any(re.match(r"^COPY\s+\./?\s+", line) for line in copy_lines)
    assert "COPY core/ ./core/" in copy_lines
    assert "COPY api/ ./api/" in copy_lines
    assert "COPY services/ ./services/" in copy_lines
    assert "COPY adapters/ ./adapters/" in copy_lines
    assert "COPY scripts/ ./scripts/" in copy_lines


def test_local_observability_images_are_pinned_and_alerts_have_receiver() -> None:
    env_example = (ROOT / ".env.example").read_text()
    compose = yaml.safe_load((ROOT / "docker-compose.observability.yml").read_text())
    alertmanager = yaml.safe_load((ROOT / "deploy/observability/alertmanager.yml").read_text())

    assert ":latest" not in env_example
    assert ":latest" not in (ROOT / "docker-compose.observability.yml").read_text()
    assert not [_image for _image in _walk_images(compose) if _image_is_latest(_image)]

    webhook_env = compose["services"]["webhook-service"]["environment"]
    assert webhook_env["REQUIRE_WEBHOOK_AUTH"] == "false"
    assert webhook_env["ALLOW_UNAUTHENTICATED_WEBHOOK"] == "true"

    receiver = next(item for item in alertmanager["receivers"] if item["name"] == "webhookwise-local")
    webhook_config = receiver["webhook_configs"][0]
    assert webhook_config["url"] == "http://webhook-service:8000/webhook/alertmanager"
    assert webhook_config["send_resolved"] is True
    assert webhook_config["max_alerts"] == 20


def test_k8s_manifests_cover_runtime_and_avoid_latest_images() -> None:
    kustomization = yaml.safe_load((ROOT / "deploy/k8s/kustomization.yaml").read_text())
    resources = set(kustomization["resources"])

    assert (ROOT / "deploy/k8s/secret.example.yaml").exists()
    assert {
        "namespace.yaml",
        "serviceaccount.yaml",
        "configmap.yaml",
        "service-api.yaml",
        "redis-service.yaml",
        "redis-statefulset.yaml",
        "postgres-service.yaml",
        "postgres-statefulset.yaml",
        "deployment-api.yaml",
        "deployment-worker.yaml",
        "deployment-scheduler.yaml",
        "job-migrate.yaml",
    } <= resources

    documents: list[dict[str, Any]] = []
    for resource in resources:
        documents.extend(_yaml_documents(f"deploy/k8s/{resource}"))

    by_kind_name = {(doc.get("kind"), doc.get("metadata", {}).get("name")): doc for doc in documents}
    assert ("Namespace", "webhookwise") in by_kind_name
    assert ("Service", "webhookwise-api") in by_kind_name
    assert ("Deployment", "webhookwise-api") in by_kind_name
    assert ("Deployment", "webhookwise-worker") in by_kind_name
    assert ("Deployment", "webhookwise-scheduler") in by_kind_name
    assert ("Job", "webhookwise-migrate") in by_kind_name
    assert ("StatefulSet", "webhookwise-redis") in by_kind_name
    assert ("StatefulSet", "webhookwise-postgres") in by_kind_name

    images = [image for doc in documents for image in _walk_images(doc)]
    assert images
    assert not [image for image in images if _image_is_latest(image)]

    api_container = by_kind_name[("Deployment", "webhookwise-api")]["spec"]["template"]["spec"]["containers"][0]
    assert api_container["livenessProbe"]["httpGet"]["path"] == "/live"
    assert api_container["readinessProbe"]["httpGet"]["path"] == "/ready"
    assert any(env["name"] == "RUN_MODE" and env["value"] == "api" for env in api_container["env"])

    worker_container = by_kind_name[("Deployment", "webhookwise-worker")]["spec"]["template"]["spec"]["containers"][0]
    scheduler_container = by_kind_name[("Deployment", "webhookwise-scheduler")]["spec"]["template"]["spec"][
        "containers"
    ][0]
    assert worker_container["livenessProbe"]["exec"]["command"] == ["python3", "-m", "scripts.healthcheck"]
    assert scheduler_container["readinessProbe"]["exec"]["command"] == ["python3", "-m", "scripts.healthcheck"]


def test_k8s_docs_include_secret_and_image_promotion_workflow() -> None:
    readme = (ROOT / "deploy/k8s/README.md").read_text()

    assert "kubectl apply -f deploy/k8s/namespace.yaml" in readme
    assert "cp deploy/k8s/secret.example.yaml" in readme
    assert "kubectl apply -k deploy/k8s" in readme
    assert "kubectl -n webhookwise rollout status deploy/webhookwise-api" in readme
    assert "Avoid `latest`" in readme
