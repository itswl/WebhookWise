# WebhookWise API Docs

FastAPI exposes interactive OpenAPI docs automatically when the API service is running:

- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`
- OpenAPI JSON: `http://localhost:8000/openapi.json`

Offline exports are kept here:

- `openapi.json`
- `openapi.yaml`

Regenerate them from the repository root when API contracts change:

```bash
OTEL_ENABLED=false python scripts/export_openapi.py
```
