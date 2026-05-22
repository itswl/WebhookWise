#!/usr/bin/env python3
"""Export the FastAPI OpenAPI schema for offline API documentation."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import json  # noqa: E402


def _render_schema(schema: dict[str, Any]) -> tuple[str, str]:
    json_text = json.dumps(schema, indent=True) + "\n"
    yaml_text = yaml.safe_dump(schema, allow_unicode=True, sort_keys=False)
    return json_text, yaml_text


def _load_schema() -> dict[str, Any]:
    os.environ.setdefault("OTEL_ENABLED", "false")
    os.environ.setdefault("LOG_LEVEL", "WARNING")
    os.environ.setdefault("THIRD_PARTY_LOG_LEVEL", "WARNING")
    from core.app import app

    return app.openapi()


def export_openapi(output_dir: Path) -> dict[str, Any]:
    schema = _load_schema()
    json_text, yaml_text = _render_schema(schema)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "openapi.json").write_text(json_text)
    (output_dir / "openapi.yaml").write_text(yaml_text)
    return schema


def check_openapi(output_dir: Path) -> dict[str, Any]:
    schema = _load_schema()
    json_text, yaml_text = _render_schema(schema)
    expected = {
        output_dir / "openapi.json": json_text,
        output_dir / "openapi.yaml": yaml_text,
    }
    stale = [path for path, content in expected.items() if not path.exists() or path.read_text() != content]
    if stale:
        formatted = ", ".join(str(path.relative_to(ROOT)) for path in stale)
        raise RuntimeError(f"OpenAPI docs are stale: {formatted}. Run scripts/export_openapi.py.")
    return schema


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default="docs/api", help="Directory for openapi.json and openapi.yaml")
    parser.add_argument("--check", action="store_true", help="Fail when exported OpenAPI docs are stale")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    if not output_dir.is_absolute():
        output_dir = ROOT / output_dir

    try:
        schema = check_openapi(output_dir) if args.check else export_openapi(output_dir)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(
        f"{'Checked' if args.check else 'Exported'} OpenAPI {schema.get('openapi')} "
        f"for {schema.get('info', {}).get('title')} with {len(schema.get('paths', {}))} paths to {output_dir}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
