from __future__ import annotations

import re
import tomllib

import yaml

from tests.helpers.paths import PROJECT_ROOT


def _project_version() -> str:
    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())
    version = pyproject["project"]["version"]
    assert isinstance(version, str)
    return version


def test_release_version_is_semver_and_runtime_default(monkeypatch) -> None:
    from core.observability.resource import get_service_version
    from core.version import __version__

    version = _project_version()
    assert re.fullmatch(r"\d+\.\d+\.\d+", version)
    assert __version__ == version
    assert f"## [{version}] - " in (PROJECT_ROOT / "CHANGELOG.md").read_text()

    monkeypatch.delenv("OTEL_SERVICE_VERSION", raising=False)
    monkeypatch.delenv("SERVICE_VERSION", raising=False)
    monkeypatch.delenv("APP_VERSION", raising=False)
    assert get_service_version() == version


def test_container_and_k8s_versions_match_project_version() -> None:
    version = _project_version()
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text()
    kustomization = yaml.safe_load((PROJECT_ROOT / "deploy/k8s/kustomization.yaml").read_text())
    configmap = yaml.safe_load((PROJECT_ROOT / "deploy/k8s/configmap.yaml").read_text())
    compose_text = (PROJECT_ROOT / "deploy/compose/docker-compose.yml").read_text()
    k8s_readme = (PROJECT_ROOT / "deploy/k8s/README.md").read_text()
    env_example = (PROJECT_ROOT / ".env.example.all").read_text()
    minimal_env_example = (PROJECT_ROOT / ".env.example").read_text()

    assert f"ARG APP_VERSION={version}" in dockerfile
    assert "APP_VERSION=$APP_VERSION" in dockerfile
    assert "OTEL_SERVICE_VERSION=$APP_VERSION" in dockerfile
    assert kustomization["images"] == [{"name": "ghcr.io/itswl/webhookwise", "newTag": version}]
    assert configmap["data"]["APP_VERSION"] == version
    assert configmap["data"]["OTEL_SERVICE_VERSION"] == version
    assert f"APP_VERSION={version}" in env_example
    assert f"OTEL_SERVICE_VERSION={version}" in env_example
    assert f"APP_VERSION={version}" in minimal_env_example
    assert f"OTEL_SERVICE_VERSION={version}" in minimal_env_example
    assert f"OTEL_SERVICE_VERSION: ${{OTEL_SERVICE_VERSION:-{version}}}" in compose_text
    assert compose_text.count(f"APP_VERSION: ${{APP_VERSION:-{version}}}") == 4
    assert f"ghcr.io/itswl/webhookwise:{version}" in k8s_readme

    for manifest_name in (
        "deployment-api.yaml",
        "deployment-worker.yaml",
        "deployment-scheduler.yaml",
        "job-migrate.yaml",
    ):
        manifest = (PROJECT_ROOT / "deploy/k8s" / manifest_name).read_text()
        image_tags = re.findall(r"image: ghcr\.io/itswl/webhookwise:([^\s]+)", manifest)
        assert image_tags and set(image_tags) == {version}


def test_release_workflow_publishes_versioned_ghcr_image() -> None:
    workflow = (PROJECT_ROOT / ".github/workflows/release.yml").read_text()

    assert 'tags:\n      - "v*.*.*"' in workflow
    assert "actions: read" in workflow
    assert "GHCR_IMAGE: ghcr.io/itswl/webhookwise" in workflow
    assert "DOCKERHUB_IMAGE: ${{ vars.DOCKERHUB_IMAGE }}" in workflow
    assert "IMAGE_PLATFORMS: linux/amd64,linux/arm64" in workflow
    assert "ci-gate:" in workflow
    # The gate dereferences annotated tags to their commit and queries runs
    # via plain REST (github.sha for an annotated tag push is the TAG OBJECT,
    # which never matches any CI run's head_sha).
    assert 'gh api "repos/${GITHUB_REPOSITORY}/commits/${RELEASE_SHA}"' in workflow
    assert "actions/runs?head_sha=${release_commit}" in workflow
    assert '.github/workflows/ci.yml" and .head_branch == "main"' in workflow
    assert "Waiting for ci.yml to pass" in workflow
    # Pin the CAPABILITY, not the action version: qemu is what makes the
    # arm64 half of the manifest possible, and asserting "@v3" turned every
    # routine dependabot bump into a red gate.
    assert "docker/setup-qemu-action@" in workflow
    assert "Log in to Docker Hub" in workflow
    assert "DOCKERHUB_TOKEN" in workflow
    assert "docker/build-push-action@" in workflow
    assert "platforms: ${{ env.IMAGE_PLATFORMS }}" in workflow
    assert "APP_VERSION=${{ needs.verify.outputs.version }}" in workflow
    assert "pytest -q --cov=core --cov=api --cov=services --cov=models --cov=adapters --cov=db" not in workflow
    assert "tests/e2e/run_webhook_to_feishu.sh" not in workflow
    assert "      - ci-gate" in workflow
    assert "      - test\n      - docker-e2e" not in workflow
    assert 'tomllib.load(fh)["project"]["version"]' in workflow
    assert 'grep -Eq "^## \\\\[$version\\\\] - [0-9]{4}-[0-9]{2}-[0-9]{2}$" CHANGELOG.md' in workflow
