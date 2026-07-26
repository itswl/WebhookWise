# Feishu Interactive Incident Cards

WebhookWise supports two Feishu delivery modes:

1. An incoming-webhook bot sends view-only cards.
2. An optional Feishu custom app sends incident cards with Acknowledge,
   Resolve, and Add note actions.

Feishu incoming-webhook bots are send-only, so interactive actions cannot be
securely added to the existing webhook URL. Feishu's
[IM FAQ](https://open.feishu.cn/document/server-docs/im-v1/faq) and
[interactive card documentation](https://open.feishu.cn/document/historical-version/interactive-message-card-sending/message-card-reference)
describe app callbacks as the interaction path.

## Configure the app

Create a Feishu custom app with a bot, grant it permission to send messages,
add it to the target chat, and configure this callback URL:

```text
https://<webhookwise-host>/v1/integrations/feishu/card-actions
```

Set the app's verification token and configure the following variables:

```dotenv
FEISHU_CARD_ACTIONS_ENABLED=true
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=<random-app-secret>
FEISHU_INCIDENT_CHAT_ID=oc_xxx
FEISHU_CARD_VERIFICATION_TOKEN=<feishu-verification-token>
FEISHU_CARD_ACTION_SECRET=<independent-random-hmac-secret>

# Optional comma-separated restrictions. Empty means any verified app tenant
# or operator can act.
FEISHU_ALLOWED_TENANT_KEYS=tenant-key
FEISHU_ALLOWED_OPERATOR_OPEN_IDS=ou_operator_a,ou_operator_b

# Signed action lifetime, 60 seconds to 30 days. Default: 7 days.
FEISHU_CARD_ACTION_TTL_SECONDS=604800
```

Restart or roll out the API, Worker, and Scheduler after changing static
configuration. In production, startup fails when the feature is enabled with
an incomplete credential set.

## Security and delivery behavior

- The callback uses `FEISHU_CARD_VERIFICATION_TOKEN`, not `API_KEY` or
  `ADMIN_WRITE_KEY`.
- Every button value is HMAC-signed, bound to an incident and action, and
  expires.
- The operator is taken from Feishu's callback `open_id`; card values cannot
  choose the audit actor.
- Optional tenant and operator allowlists are checked before a mutation.
- Feishu `event_id` plus a request-body digest forms a durable idempotency
  receipt. A repeated event returns the stored result; the same event id with
  different content is rejected.
- Workflow mutation, audit record, and idempotency receipt commit in one
  database transaction.
- Cards set forwarding off and multi-recipient updates on. No management
  credential is embedded in a button or URL.
- App messages use the existing forwarding outbox, so transport retries and
  expiry policy remain consistent with other channels.

Feishu's
[card callback reference](https://open.feishu.cn/document/feishu-cards/card-components/interactive-components/input)
documents the operator, event id, form values, and verification token fields.

When the custom-app settings are disabled or incomplete, WebhookWise continues
using the configured `DEEP_ANALYSIS_FEISHU_WEBHOOK` or report webhook and only
adds a safe dashboard link when `DASHBOARD_PUBLIC_URL` is set.
