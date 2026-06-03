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
    env_example = (PROJECT_ROOT / ".env.example.all").read_text()

    assert f"ARG APP_VERSION={version}" in dockerfile
    assert "APP_VERSION=$APP_VERSION" in dockerfile
    assert "OTEL_SERVICE_VERSION=$APP_VERSION" in dockerfile
    assert kustomization["images"] == [{"name": "ghcr.io/itswl/webhookwise", "newTag": version}]
    assert configmap["data"]["APP_VERSION"] == version
    assert configmap["data"]["OTEL_SERVICE_VERSION"] == version
    assert f"APP_VERSION={version}" in env_example
    assert f"OTEL_SERVICE_VERSION={version}" in env_example


def test_release_workflow_publishes_versioned_ghcr_image() -> None:
    workflow = (PROJECT_ROOT / ".github/workflows/release.yml").read_text()

    assert 'tags:\n      - "v*.*.*"' in workflow
    assert "ghcr.io/${{ github.repository }}" in workflow
    assert "docker/build-push-action@v6" in workflow
    assert "APP_VERSION=${{ needs.verify.outputs.version }}" in workflow
    assert 'tomllib.load(fh)["project"]["version"]' in workflow
    assert 'grep -Eq "^## \\\\[$version\\\\] - [0-9]{4}-[0-9]{2}-[0-9]{2}$" CHANGELOG.md' in workflow
