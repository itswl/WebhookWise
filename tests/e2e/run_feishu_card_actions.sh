#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE_FILE="$ROOT_DIR/tests/e2e/docker-compose.yml"
COMPOSE=(docker compose -f "$COMPOSE_FILE")

cleanup() {
  "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
}
dump_logs() {
  status=$?
  if [ "$status" -ne 0 ]; then
    "${COMPOSE[@]}" ps || true
    "${COMPOSE[@]}" logs --no-color --tail=200 || true
  fi
  cleanup
  exit "$status"
}
trap dump_logs EXIT

cleanup
# CI pre-builds the app image with a persistent layer cache and sets
# E2E_SKIP_BUILD=1 so this run reuses it; locally the build runs by default so
# source changes are always picked up.
up_args=(up -d)
if [ "${E2E_SKIP_BUILD:-}" != "1" ]; then
  up_args+=(--build)
fi
"${COMPOSE[@]}" "${up_args[@]}"

wait_container_running() {
  local service="$1"
  local deadline=$((SECONDS + 60))
  local container_id=""

  while [ "$SECONDS" -lt "$deadline" ]; do
    container_id="$("${COMPOSE[@]}" ps -q "$service")"
    if [ -n "$container_id" ] && [ "$(docker inspect -f '{{.State.Running}}' "$container_id" 2>/dev/null)" = "true" ]; then
      return 0
    fi
    sleep 2
  done

  echo "$service did not reach running state" >&2
  return 1
}

wait_container_running scheduler

python - <<'PY'
import json
import time
import urllib.error
import urllib.request

API = "http://localhost:18080"
FAKE_FEISHU = "http://localhost:19090"
API_KEY = "e2e-api-key"
VERIFICATION_TOKEN = "e2e-verify-token"


def wait_json(url: str, timeout: int = 120):
    deadline = time.time() + timeout
    last_error = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            last_error = exc
            time.sleep(2)
    raise SystemExit(f"timed out waiting for {url}: {last_error}")


def get_json(url: str, headers: dict | None = None):
    request = urllib.request.Request(url, headers=headers or {}, method="GET")
    with urllib.request.urlopen(request, timeout=10) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def post_json(url: str, body: bytes, headers: dict | None = None):
    request = urllib.request.Request(
        url,
        data=body,
        headers={"content-type": "application/json", **(headers or {})},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


wait_json(f"{API}/ready")
wait_json(f"{FAKE_FEISHU}/ready")
wait_json("http://localhost:19091/ready")

# Two correlated alerts: same source (webhook path) and same rule/service so
# incident grouping pairs them into one incident on the next background scan.
for current_value in (25, 31):
    alert = {
        "alert_name": "e2e-cards-critical-5xx",
        "event_type": "prometheus_alert",
        "Level": "critical",
        "service": "cards-api",
        "instance": f"cards-api-{current_value}",
        "CurrentValue": current_value,
        "Threshold": 5,
    }
    status, accepted = post_json(f"{API}/v1/webhook/prometheus", json.dumps(alert).encode("utf-8"))
    if status != 200:
        raise SystemExit(f"unexpected webhook status: {status} {accepted}")

# Incident grouping runs on the background scan interval (30s floor) and the
# outbox then delivers the interactive card through the fake app API.
deadline = time.time() + 240
message_request = None
requests_snapshot = []
while time.time() < deadline:
    requests_snapshot = wait_json(f"{FAKE_FEISHU}/requests", timeout=5)
    for recorded in requests_snapshot:
        if str(recorded.get("path", "")).startswith("/open-apis/im/v1/messages"):
            message_request = recorded
    if message_request is not None:
        break
    time.sleep(3)
if message_request is None:
    raise SystemExit(
        "fake Feishu did not receive an im/v1/messages request: "
        + json.dumps(requests_snapshot, ensure_ascii=False)
    )

if not any(
    str(recorded.get("path", "")).startswith("/open-apis/auth/v3/tenant_access_token/internal")
    for recorded in requests_snapshot
):
    raise SystemExit("fake Feishu did not receive a tenant_access_token request")

if "receive_id_type=chat_id" not in str(message_request["path"]):
    raise SystemExit(f"im/v1/messages was not addressed by chat_id: {message_request['path']}")
message_headers = {key.lower(): value for key, value in message_request.get("headers", {}).items()}
if message_headers.get("authorization") != "Bearer t-e2e":
    raise SystemExit(f"im/v1/messages did not carry the tenant token: {message_headers.get('authorization')}")
message_body = message_request["json"]
if not str(message_body.get("uuid") or ""):
    raise SystemExit(f"im/v1/messages request had no dedup uuid: {json.dumps(message_body, ensure_ascii=False)}")
if message_body.get("receive_id") != "oc_e2e" or message_body.get("msg_type") != "interactive":
    raise SystemExit(f"unexpected im/v1/messages body: {json.dumps(message_body, ensure_ascii=False)}")

card = json.loads(message_body["content"])
ack_value = None
for element in card.get("elements", []):
    if element.get("tag") != "action":
        continue
    for action in element.get("actions", []):
        if action.get("text", {}).get("content") == "Acknowledge":
            ack_value = action.get("value")
if not isinstance(ack_value, dict):
    raise SystemExit("card has no Acknowledge action value: " + json.dumps(card, ensure_ascii=False))
incident_id = int(ack_value["resource_id"])

# Real card.action.trigger envelope: verification token + create_time live in
# the header, the operator identity and signed action value in the event.
callback = {
    "header": {
        "event_id": "e2e-evt-1",
        "event_type": "card.action.trigger",
        "token": VERIFICATION_TOKEN,
        "create_time": str(int(time.time() * 1000)),
        "tenant_key": "e2e-tenant",
    },
    "event": {
        "operator": {"open_id": "ou_e2e"},
        "action": {"value": ack_value},
    },
}
callback_bytes = json.dumps(callback, ensure_ascii=False).encode("utf-8")
callback_url = f"{API}/v1/integrations/feishu/card-actions"

status, first_response = post_json(callback_url, callback_bytes)
if status != 200 or first_response.get("toast", {}).get("type") != "success":
    raise SystemExit(f"unexpected card action response: {status} {json.dumps(first_response, ensure_ascii=False)}")

status, detail = get_json(f"{API}/v1/incidents/{incident_id}", headers={"x-api-key": API_KEY})
workflow_status = detail.get("data", {}).get("workflow_status")
if status != 200 or workflow_status != "acknowledged":
    raise SystemExit(f"incident {incident_id} was not acknowledged: {status} workflow_status={workflow_status}")

# Replaying the exact same event bytes must hit the stored receipt: HTTP 200
# with the identical result, and no second state transition.
status, replay_response = post_json(callback_url, callback_bytes)
if status != 200 or replay_response != first_response:
    raise SystemExit(
        f"replayed callback was not idempotent: {status} {json.dumps(replay_response, ensure_ascii=False)}"
    )
status, detail = get_json(f"{API}/v1/incidents/{incident_id}", headers={"x-api-key": API_KEY})
if detail.get("data", {}).get("workflow_status") != "acknowledged":
    raise SystemExit("incident workflow_status changed after the replayed callback")

# A wrong verification token must be rejected before any processing.
bad_callback = json.loads(callback_bytes.decode("utf-8"))
bad_callback["header"]["token"] = "wrong-token"
try:
    post_json(callback_url, json.dumps(bad_callback, ensure_ascii=False).encode("utf-8"))
except urllib.error.HTTPError as error:
    if error.code != 401:
        raise SystemExit(f"wrong verification token returned HTTP {error.code}, expected 401")
else:
    raise SystemExit("callback with a wrong verification token was accepted")

print(
    json.dumps(
        {
            "incident_id": incident_id,
            "workflow_status": "acknowledged",
            "message_uuid": message_body["uuid"],
            "toast": first_response["toast"],
            "feishu_requests": len(requests_snapshot),
        },
        ensure_ascii=False,
    )
)
PY
