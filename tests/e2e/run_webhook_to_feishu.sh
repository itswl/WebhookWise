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
"${COMPOSE[@]}" up -d --build

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
import time
import urllib.error
import urllib.request

from core import json


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


wait_json("http://localhost:18080/ready")
wait_json("http://localhost:19090/ready")
wait_json("http://localhost:19091/ready")

payload = {
    "alert_name": "checkout-critical-5xx",
    "event_type": "prometheus_alert",
    "Level": "critical",
    "service": "checkout-api",
    "CurrentValue": 25,
    "Threshold": 5,
}
request = urllib.request.Request(
    "http://localhost:18080/webhook/prometheus",
    data=json.dumps(payload).encode("utf-8"),
    headers={"content-type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(request, timeout=10) as resp:
    if resp.status != 202:
        raise SystemExit(f"unexpected webhook status: {resp.status}")
    accepted = json.loads(resp.read().decode("utf-8"))

deadline = time.time() + 90
requests = []
while time.time() < deadline:
    requests = wait_json("http://localhost:19090/requests", timeout=5)
    serialized = json.dumps(requests, ensure_ascii=False)
    if "interactive" in serialized and "Webhook 事件通知" in serialized and "AI E2E 摘要" in serialized:
        openai_requests = wait_json("http://localhost:19091/requests", timeout=5)
        if not any(req.get("path") == "/v1/chat/completions" for req in openai_requests):
            raise SystemExit("fake OpenAI did not receive a chat completion request")
        print(
            json.dumps(
                {
                    "event_id": accepted["event_id"],
                    "feishu_requests": len(requests),
                    "openai_requests": len(openai_requests),
                },
                ensure_ascii=False,
            )
        )
        break
    time.sleep(2)
else:
    raise SystemExit("fake Feishu did not receive the expected interactive card: " + json.dumps(requests, ensure_ascii=False))
PY
