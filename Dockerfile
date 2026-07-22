# ====== Build stage ======
FROM python:3.14-slim AS builder

WORKDIR /app

# Prefer the lock file to ensure reproducible builds.
COPY requirements.lock requirements.txt ./
RUN python -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir --upgrade pip && \
    /opt/venv/bin/pip install --no-cache-dir -r requirements.lock


# ====== Runtime stage ======
FROM python:3.14-slim

ARG APP_VERSION=3.4.0

# Set the timezone to Asia/Shanghai.
ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && \
    echo $TZ > /etc/timezone

# Install the jemalloc memory allocator (runtime stage).
# jemalloc replaces glibc malloc, cutting memory fragmentation by 30-40% and
# lowering RSS. This is especially significant for long-running Python
# multi-worker services.
# LD_PRELOAD is set dynamically per x86_64/aarch64 in entrypoint.sh.
# libjemalloc2: memory allocator (see above). postgresql-client: provides
# pg_dump/pg_restore used by scripts.ops.backup_db / restore_db (backup service
# and in-container restore) and the healthcheck's DB tooling.
RUN apt-get update && apt-get install -y --no-install-recommends libjemalloc2 postgresql-client && rm -rf /var/lib/apt/lists/*

# Create a non-root user.
RUN useradd -m -u 1000 appuser

# Set the working directory.
WORKDIR /app

# Set environment variables.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8000 \
    API_WORKERS=2 \
    APP_VERSION=$APP_VERSION \
    OTEL_SERVICE_VERSION=$APP_VERSION \
    PYTHONPATH=/app \
    PATH=/opt/venv/bin:$PATH

LABEL org.opencontainers.image.title="WebhookWise" \
      org.opencontainers.image.description="Webhook ingestion, analysis, forwarding, and observability service" \
      org.opencontainers.image.version=$APP_VERSION \
      org.opencontainers.image.source="https://github.com/itswl/WebhookWise"

# Copy the installed dependencies from the build stage.
COPY --from=builder /opt/venv /opt/venv

# Copy runtime project files; the exclusion boundary is maintained centrally in
# .dockerignore so newly added directories are not missed.
COPY . .

# Note: do not copy .env files, to avoid baking secrets into the image.
# Inject configuration at deploy time via mounted volumes or environment variables.

# Create the required directories and set permissions.
RUN mkdir -p logs && \
    rm -f .env .env.* && \
    chmod +x entrypoint.sh && \
    chown -R appuser:appuser /app

# Switch to the non-root user.
USER appuser

# Expose the port.
EXPOSE 8000

# Health check (uses native Python, no curl required).
# - api/all mode: probes HTTP /ready
# - worker mode: probes Redis/DB connectivity (workers don't listen on 8000, so
#   an HTTP probe would wrongly mark them unhealthy)
# start-period=30s: the lifespan opens PostgreSQL + Redis connections (and, when
#   enabled, the MCP session manager) before /ready is meaningful, so failures
#   during a slow dependency start must not burn the --retries budget.
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD ["python3", "-m", "scripts.healthcheck"]

# Set the startup entrypoint (dispatches the process by RUN_MODE; migrations run
# as a separate migrate job).
ENTRYPOINT ["./entrypoint.sh"]

# Run the app with Gunicorn + UvicornWorker (production).
# Why Gunicorn rather than plain Uvicorn:
#   - Gunicorn provides process management (auto-restart of crashed workers),
#     graceful restart, and signal handling.
#   - Multi-worker mode (workers=4) makes full use of multiple CPU cores for
#     higher throughput.
#   - If you only need a single worker and no process management, use directly:
#     CMD ["uvicorn", "api.app:app", "--host", "0.0.0.0", "--port", "8000"]
# timeout 120s: OpenClaw analysis now uses async polling, so the handler no
# longer blocks for long periods.
CMD ["gunicorn", "-c", "gunicorn_config.py", "api.app:app"]
