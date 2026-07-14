import re
import tomllib
from collections.abc import Iterator
from typing import Any

import yaml

from tests.helpers.paths import PROJECT_ROOT

ROOT = PROJECT_ROOT


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

    assert re.search(r"(?m)^  pull_request:\s*$", ci)
    assert re.search(r"(?m)^  push:\s*$", ci)
    assert "shellcheck entrypoint.sh tests/e2e/run_webhook_to_feishu.sh" in ci
    assert "--cov=core" in ci
    assert "--cov=api" in ci
    assert "--cov=services" in ci
    assert "--cov-report=xml" in ci
    # Branch coverage enabled; gate is a ratchet (>=85, raised incrementally).
    assert "--cov-branch" in ci
    assert "--cov-fail-under=85" in ci
    assert "python scripts/observability/webhookwise_observe.py contract" in ci
    assert pyproject["tool"]["coverage"]["run"]["branch"] is True
    assert pyproject["tool"]["coverage"]["report"]["fail_under"] >= 85


def test_dependency_updates_and_ai_dev_entrypoint_are_configured() -> None:
    dependabot = yaml.safe_load((ROOT / ".github/dependabot.yml").read_text())
    ecosystems = {item["package-ecosystem"] for item in dependabot["updates"]}
    guide = (ROOT / "CLAUDE.md").read_text()

    assert ecosystems == {"docker", "github-actions", "pip"}
    assert "ruff check ." in guide
    assert "shellcheck entrypoint.sh tests/e2e/run_webhook_to_feishu.sh" in guide
    assert "Keep metrics labels stable and machine-readable" in guide


def test_dockerfile_uses_single_context_copy_with_dockerignore_boundary() -> None:
    lines = (ROOT / "Dockerfile").read_text().splitlines()
    copy_lines = [line.strip() for line in lines if line.strip().startswith("COPY ")]
    dockerignore = (ROOT / ".dockerignore").read_text()

    assert "COPY . ." in copy_lines
    assert "tests/" in dockerignore
    assert "docs/" in dockerignore
    assert ".github/" in dockerignore


def test_local_compose_quickstart_uses_infra_service_dns() -> None:
    readme = (ROOT / "README.md").read_text()
    env_example = (ROOT / ".env.example").read_text()
    env_example_all = (ROOT / ".env.example.all").read_text()

    assert not (ROOT / "docker-compose.yml").exists()
    assert (ROOT / "compose.yaml").exists()
    assert (ROOT / "deploy/compose/docker-compose.yml").exists()
    assert (ROOT / "deploy/compose/docker-compose.infra.yml").exists()
    assert (ROOT / "deploy/compose/docker-compose.observability.yml").exists()
    assert "docker compose up -d --build" in readme
    root_compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    assert root_compose["name"] == "webhookwise"
    assert [item["path"] for item in root_compose["include"]] == [
        "deploy/compose/docker-compose.infra.yml",
        "deploy/compose/docker-compose.yml",
    ]
    assert (
        "DATABASE_URL=postgresql://webhook_user:please-change-postgres-password@postgres:5432/webhooks" in env_example
    )
    assert "REDIS_URL=redis://redis:6379/0" in env_example
    assert (
        "DATABASE_URL=postgresql://webhook_user:please-change-postgres-password@postgres:5432/webhooks"
        in env_example_all
    )
    assert "POSTGRES_PASSWORD=please-change-postgres-password" in env_example_all


def test_local_observability_images_are_pinned_and_alerts_have_receiver() -> None:
    env_example = (ROOT / ".env.example.all").read_text()
    compose = yaml.safe_load((ROOT / "deploy/compose/docker-compose.observability.yml").read_text())
    alertmanager = yaml.safe_load((ROOT / "deploy/observability/alertmanager/alertmanager.yml").read_text())

    assert ":latest" not in env_example
    assert ":latest" not in (ROOT / "deploy/compose/docker-compose.observability.yml").read_text()
    assert not [_image for _image in _walk_images(compose) if _image_is_latest(_image)]

    assert compose["name"] == "webhookwise-observability"
    assert not {"webhook-service", "worker", "scheduler", "migrate"} & set(compose["services"])
    assert compose["services"]["beyla"]["pid"] == "container:webhook-receiver"
    assert compose["services"]["beyla"]["profiles"] == ["diagnostics"]
    assert compose["services"]["tempo"]["profiles"] == ["diagnostics"]
    assert compose["services"]["pyroscope"]["profiles"] == ["diagnostics"]
    assert compose["networks"]["webhook_net"] == {
        "name": "webhookwise_webhook_net",
        "external": True,
    }

    receiver = next(item for item in alertmanager["receivers"] if item["name"] == "webhookwise-local")
    webhook_config = receiver["webhook_configs"][0]
    assert webhook_config["url"] == "http://webhook-service:8000/v1/webhook/alertmanager"
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

    config = by_kind_name[("ConfigMap", "webhookwise-config")]["data"]
    # Defaults to "false": OTEL_ENABLED "true" with a blank OTEL_EXPORTER_OTLP_ENDPOINT
    # is a silent no-op, so the k8s baseline stays off until a collector endpoint is set.
    assert config["OTEL_ENABLED"] == "false"
    assert config["OTEL_LOGS_ENABLED"] == "true"
    assert config["OTEL_SERVICE_NAMESPACE"] == "webhookwise"
    assert config["OTEL_SCHEMA_URL"] == "https://opentelemetry.io/schemas/1.41.0"
    assert config["OTEL_SEMCONV_VERSION"] == "1.41.0"
    assert config["OTEL_METRICS_EXEMPLAR_FILTER"] == "trace_based"
    assert config["OTEL_TRACES_SAMPLER"] == "parentbased_traceidratio"
    assert config["OTEL_TRACES_SAMPLER_ARG"] == "0.1"
    assert "LOG_FORMAT" not in config
    assert "MAX_CONCURRENT_WEBHOOK_TASKS" not in config
    assert "WEBHOOK_TASK_SLOT_LEASE_SECONDS" not in config
    assert config["PROCESSING_LOCK_DISTRIBUTED_ENABLED"] == "true"
    assert config["PROCESSING_LOCK_TTL_SECONDS"] == "180"
    assert config["PROCESSING_LOCK_WAIT_TIMEOUT_SECONDS"] == "15"
    assert config["PROCESSING_LOCK_POLL_INTERVAL_MS"] == "100"
    assert config["PROCESSING_LOCK_FAILFAST_THRESHOLD"] == "20"
    assert config["PROCESSING_LOCK_FAILFAST_WINDOW_SECONDS"] == "10"

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


def test_stateful_datastores_run_non_root() -> None:
    """Redis/Postgres StatefulSets must declare a non-root securityContext, like
    the app Deployments — they were the unhardened outliers (review #25)."""
    for resource in ("postgres-statefulset.yaml", "redis-statefulset.yaml"):
        doc = _yaml_documents(f"deploy/k8s/{resource}")[0]
        pod_spec = doc["spec"]["template"]["spec"]
        assert pod_spec["securityContext"]["runAsNonRoot"] is True, resource
        assert pod_spec["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault", resource
        container = pod_spec["containers"][0]
        assert container["securityContext"]["allowPrivilegeEscalation"] is False, resource
        assert container["securityContext"]["capabilities"]["drop"] == ["ALL"], resource


def test_long_running_workloads_have_startup_probe_and_spread() -> None:
    """worker/scheduler need a startupProbe (review #28); multi-replica
    workloads need topology spread so replicas don't co-locate (review #29)."""
    for resource in ("deployment-worker.yaml", "deployment-scheduler.yaml"):
        doc = _yaml_documents(f"deploy/k8s/{resource}")[0]
        container = doc["spec"]["template"]["spec"]["containers"][0]
        assert "startupProbe" in container, resource

    for resource in ("deployment-api.yaml", "deployment-worker.yaml"):
        doc = _yaml_documents(f"deploy/k8s/{resource}")[0]
        pod_spec = doc["spec"]["template"]["spec"]
        constraints = pod_spec.get("topologySpreadConstraints") or []
        assert any(c.get("topologyKey") == "kubernetes.io/hostname" for c in constraints), resource
