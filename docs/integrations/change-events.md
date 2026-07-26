# Change-event integrations

WebhookWise correlates normalized deployment, configuration, feature-flag, and
infrastructure changes with nearby incidents. A successful ingestion does not
create an alert and never triggers remediation. It provides evidence for the
incident-intelligence panel and inserts strong candidate changes into the
incident timeline.

## Configure a least-privilege token

Generate a random token:

```bash
openssl rand -hex 32
```

Set it as `CHANGE_INGEST_TOKEN` in the WebhookWise environment and restart or
roll out the API service. Store the same value in the CI/CD system's secret
store. CI/CD systems should not receive `API_KEY` or `ADMIN_WRITE_KEY`.

The endpoint accepts either:

```text
Authorization: Bearer <CHANGE_INGEST_TOKEN>
X-Change-Ingest-Token: <CHANGE_INGEST_TOKEN>
```

`ADMIN_WRITE_KEY` is accepted as an operator fallback for manual recovery. A
management `API_KEY` does not grant change-ingestion permission.

## Normalized contract

Send a JSON object to `POST /v1/changes`. The pair `(source, external_id)` is
the idempotency key, so retrying a delivery updates the existing change rather
than creating a duplicate.

Required fields:

| Field | Description |
| --- | --- |
| `external_id` | Stable deployment or change identifier from the source system. |
| `source` | Machine-readable source, such as `github-actions` or `argocd`. |
| `change_type` | `deployment`, `config`, `feature_flag`, `infrastructure`, or `other`. |
| `started_at` | ISO 8601 timestamp. |

Add `service`, `project`, `environment`, `region`, `resource_type`, and
`resource_id` whenever available. These identity fields improve deterministic
correlation. `version_from`, `version_to`, `actor`, `status`, `finished_at`,
and `source_url` make the timeline useful to operators.

Successful requests return `201` for a new event and `200` for an idempotent
update. Treat any other response as retryable according to the source system's
normal delivery policy.

## Ready-to-adapt examples

- [GitHub Actions reusable workflow](../examples/change-ingest/github-actions.yml)
- [GitLab CI job](../examples/change-ingest/gitlab-ci.yml)
- [Jenkins declarative pipeline](../examples/change-ingest/Jenkinsfile)
- [Argo CD Notifications configuration](../examples/change-ingest/argocd-notifications.yaml)

Run the reporter only after a deployment succeeds. Keep `external_id` stable
across retries, and use a new identifier for a rollback or a later deployment.
