#!/usr/bin/env python3
"""Export the FastAPI OpenAPI schema for offline API documentation."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def export_openapi(output_dir: Path) -> dict[str, Any]:
    os.environ.setdefault("OTEL_ENABLED", "false")
    from core.app import app

    schema = app.openapi()
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "openapi.json").write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n")
    (output_dir / "openapi.yaml").write_text(yaml.safe_dump(schema, allow_unicode=True, sort_keys=False))
    return schema


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default="docs/api", help="Directory for openapi.json and openapi.yaml")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    if not output_dir.is_absolute():
        output_dir = ROOT / output_dir

    schema = export_openapi(output_dir)
    print(
        f"Exported OpenAPI {schema.get('openapi')} for {schema.get('info', {}).get('title')} "
        f"with {len(schema.get('paths', {}))} paths to {output_dir}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
