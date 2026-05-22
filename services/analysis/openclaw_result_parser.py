"""OpenClaw result text parsing helpers."""

from core import json
from services.webhooks.types import WebhookData


def extract_robust_json(text: str) -> str | None:
    """Extract the first complete JSON object from mixed text."""
    if not isinstance(text, str):
        return None
    start_idx = text.find("{")
    if start_idx == -1:
        return None
    stack = 0
    for i in range(start_idx, len(text)):
        if text[i] == "{":
            stack += 1
        elif text[i] == "}":
            stack -= 1
            if stack == 0:
                return text[start_idx : i + 1]
    return None


def build_analysis_result_from_openclaw_text(text: str, run_id: str = "") -> WebhookData:
    """Convert OpenClaw text into persisted analysis_result."""
    parsed_result = None
    json_text = extract_robust_json(text)
    if json_text:
        try:
            parsed_result = json.loads(json_text)
        except json.JSONDecodeError:
            parsed_result = None

    if parsed_result and isinstance(parsed_result, dict):
        parsed_result["_openclaw_run_id"] = run_id
        parsed_result["_openclaw_text"] = text
        return dict(parsed_result)
    return {"root_cause": text, "_openclaw_text": text}
