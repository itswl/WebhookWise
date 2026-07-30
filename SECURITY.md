# Security Policy

## Reporting a Vulnerability

Please report suspected vulnerabilities **privately** through
[GitHub Security Advisories](https://github.com/itswl/WebhookWise/security/advisories/new)
("Report a vulnerability" on the repository's Security tab).

**Do not open a public issue for a security problem.** Public issues are
indexed immediately and put every self-hosted deployment at risk before a fix
exists.

A useful report includes the affected version (release tag or commit SHA), the
deployment mode (Docker Compose, Kubernetes, host processes), reproduction
steps or a proof of concept, and your assessment of the impact.

## Response Expectations

WebhookWise is a self-hosted project maintained on a best-effort basis. There
is no commercial SLA. We aim to acknowledge private reports within a few days,
keep you informed while we investigate, and credit reporters in the advisory
and changelog unless you prefer otherwise. Please allow a reasonable window
for a fix before any public disclosure.

## Supported Versions

Only the **latest minor release** (the most recent `x.y.*` line) receives
security fixes. Older versions should upgrade; migrations are provided and
`CHANGELOG.md` flags anything that needs attention during an upgrade.

## Scope

Areas we especially want to hear about:

- **Webhook ingress authentication** — HMAC-SHA256 signature verification
  (`X-Webhook-Signature`), token authentication, and bypasses of
  `REQUIRE_WEBHOOK_AUTH`.
- **API keys and credential scoping** — `API_KEY`, `ADMIN_WRITE_KEY`,
  `CHANGE_INGEST_TOKEN`, and the source-scoped revocable webhook credentials
  (privilege escalation between them, timing side channels, credential
  leakage in logs or responses).
- **Feishu callback verification** — signed interactive card actions
  (`/v1/integrations/feishu/card-actions`): signature/token verification,
  replay protection, idempotency, and action-value forgery.
- Server-side request forgery through forwarding targets, and injection
  through untrusted webhook payloads.

Out of scope (in general): vulnerabilities purely in third-party dependencies
(report upstream — but do tell us if WebhookWise's usage makes them
exploitable), issues that require an already-compromised host or deliberately
misconfigured deployment, and reports from automated scanners without a
plausible exploitation path.
