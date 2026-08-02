# WebhookWise Lite — one container, no external services.
#
# Build from the repository root so the lite/ package keeps its import path:
#   docker build -f lite/Dockerfile -t webhookwise-lite .
#   docker run -p 8000:8000 -v wwlite:/data webhookwise-lite
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DB_PATH=/data/webhookwise.db

WORKDIR /app

COPY lite/requirements.txt /app/lite/requirements.txt
RUN pip install --no-cache-dir -r /app/lite/requirements.txt

COPY lite/ /app/lite/

# The SQLite file lives on a volume; the app itself is stateless.
RUN mkdir -p /data && useradd -r -u 10001 app && chown -R app:app /data /app
USER app
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=3).status==200 else 1)"

CMD ["uvicorn", "lite.app:app", "--host", "0.0.0.0", "--port", "8000"]
