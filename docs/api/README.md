# WebhookWise API Docs

FastAPI exposes interactive OpenAPI docs automatically when the API service is running:

- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`
- OpenAPI JSON: `http://localhost:8000/openapi.json`

Offline exports are generated on demand and are not checked in:

```bash
OTEL_ENABLED=false python scripts/export_openapi.py
```

The default output directory is `build/openapi`. Pass `--output-dir <dir>` to write somewhere else.
