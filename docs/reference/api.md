# WebhookWise API Docs

FastAPI exposes interactive OpenAPI docs automatically when the API service is running:

- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`
- OpenAPI JSON: `http://localhost:8000/openapi.json`

WebhookWise business endpoints are versioned under `/v1`. Health checks
(`/live`, `/ready`) and dashboard assets are operational endpoints and are not
part of the business API version.

Offline exports are generated on demand and are not checked in:

```bash
OTEL_ENABLED=false python scripts/export_openapi.py
```

The default output directory is `build/openapi`. Pass `--output-dir <dir>` to write somewhere else.
