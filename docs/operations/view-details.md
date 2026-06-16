# Viewing Event Details

The Dashboard list endpoint returns only summary fields for quickly rendering the event list; when you expand a specific event, the frontend calls the detail endpoint on demand to load the redacted raw data and the full analysis result.

## Dashboard

1. Open `http://localhost:8000`.
2. Click any alert item to expand its details.
3. Switch between the `Overview`, `Raw Data`, `AI Analysis`, and `Deep Analysis` tabs.

## API

List summary:

```bash
curl "http://localhost:8000/v1/webhooks?page=1&page_size=100" \
  -H "Authorization: Bearer $API_KEY" | jq .
```

Single detail:

```bash
curl "http://localhost:8000/v1/webhooks/8265" \
  -H "Authorization: Bearer $API_KEY" | jq .
```

The detail response includes troubleshooting fields such as `raw_payload`, `headers`, `parsed_data`, `alert_hash`, `ai_analysis`, and `processing_status`. Common sensitive headers and secret/token/password fields are redacted.
