"""Gunicorn 配置。

Observability is exported via OTLP from the application process, so Gunicorn no
longer needs Prometheus multiprocess cleanup hooks.
"""
