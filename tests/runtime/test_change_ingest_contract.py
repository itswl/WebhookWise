from __future__ import annotations

from pathlib import Path

import yaml

from tests.helpers.paths import PROJECT_ROOT


def test_change_ingest_examples_use_the_least_privilege_contract() -> None:
    examples = PROJECT_ROOT / "docs/examples/change-ingest"
    paths = (
        examples / "github-actions.yml",
        examples / "gitlab-ci.yml",
        examples / "Jenkinsfile",
        examples / "argocd-notifications.yaml",
    )

    for path in paths:
        text = path.read_text()
        assert "/v1/changes" in text, path
        assert "Authorization" in text and "Bearer" in text, path
        assert "ADMIN_WRITE_KEY" not in text, path
        assert '"external_id"' in text or "external_id:" in text, path
        assert '"started_at"' in text or "started_at:" in text, path

    documents = list(yaml.safe_load_all((examples / "argocd-notifications.yaml").read_text()))
    assert [document["kind"] for document in documents] == ["Secret", "ConfigMap", "Application"]


def test_change_ingest_documentation_links_every_example() -> None:
    guide = (PROJECT_ROOT / "docs/integrations/change-events.md").read_text()

    for filename in ("github-actions.yml", "gitlab-ci.yml", "Jenkinsfile", "argocd-notifications.yaml"):
        assert filename in guide
    assert "CHANGE_INGEST_TOKEN" in guide
    assert "(source, external_id)" in guide


def test_change_ingest_token_is_in_deployment_templates() -> None:
    paths = (
        PROJECT_ROOT / ".env.example",
        PROJECT_ROOT / ".env.example.all",
        PROJECT_ROOT / "deploy/k8s/secret.example.yaml",
    )

    for path in paths:
        assert "CHANGE_INGEST_TOKEN" in Path(path).read_text()
