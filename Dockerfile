# ====== 构建阶段 ======
FROM python:3.12-slim AS builder

WORKDIR /app

# 优先使用锁文件确保可重现构建
COPY requirements.lock requirements.txt ./
RUN pip install --no-cache-dir --user -r requirements.lock -i https://pypi.tuna.tsinghua.edu.cn/simple


# ====== 运行阶段 ======
FROM python:3.12-slim

# 设置时区为中国上海
ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && \
    echo $TZ > /etc/timezone

# 安装 jemalloc 内存分配器（运行阶段）
# jemalloc 替换 glibc malloc，减少内存碎片 30-40%，降低 RSS 占用
# 对 Python 多 Worker 长时间运行的服务尤其显著
# LD_PRELOAD 在 entrypoint.sh 中动态设置，兼容 x86_64/aarch64
RUN apt-get update && apt-get install -y --no-install-recommends libjemalloc2 && rm -rf /var/lib/apt/lists/*

# 创建非 root 用户
RUN useradd -m -u 1000 appuser

# 设置工作目录
WORKDIR /app

# 设置环境变量
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    PATH=/home/appuser/.local/bin:$PATH

# 从构建阶段复制已安装的依赖
COPY --from=builder /root/.local /home/appuser/.local

# 复制项目文件
COPY .env.example .env.example
COPY main.py .
COPY worker.py .
COPY entrypoint.sh .
COPY gunicorn_config.py .
COPY core/ ./core/
COPY api/ ./api/
COPY models/ ./models/
COPY db/ ./db/
COPY services/ ./services/
COPY adapters/ ./adapters/
COPY templates/ ./templates/
COPY prompts/ ./prompts/
COPY schemas/ ./schemas/
COPY scripts/ ./scripts/
COPY alembic.ini .
COPY alembic/ ./alembic/

# 注意: 不复制 .env 文件以避免敏感信息打包进镜像
# 部署时通过挂载卷或环境变量方式注入配置

# 创建必要的目录并设置权限
RUN mkdir -p logs webhooks_data /tmp/prometheus_multiproc && \
    chmod +x entrypoint.sh && \
    chown -R appuser:appuser /app /tmp/prometheus_multiproc

# 切换到非 root 用户
USER appuser

# 暴露端口
EXPOSE 8000

# 健康检查（使用 Python 原生方式，无需 curl）
# - api/all 模式：探测 HTTP /health
# - worker 模式：探测 Redis/DB 连通性（worker 不监听 8000，避免误判为 unhealthy）
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD ["python3", "-m", "scripts.healthcheck"]

# 设置启动入口点（自动执行数据库初始化和迁移）
ENTRYPOINT ["./entrypoint.sh"]

# 使用 Gunicorn + UvicornWorker 运行应用(生产环境)
# 选择 Gunicorn 而非纯 Uvicorn 的原因：
#   - Gunicorn 提供进程管理（自动重启崩溃的 Worker）、graceful restart、信号处理
#   - 多 Worker 模式（workers=4）充分利用多核 CPU，提升吞吐量
#   - 如果只需单 Worker 且不需要进程管理，可直接使用：
#     CMD ["uvicorn", "core.app:app", "--host", "0.0.0.0", "--port", "8000"]
# timeout 120 秒：OpenClaw 分析已改为异步轮询，handler 不再长时间阻塞
CMD ["gunicorn", "-c", "gunicorn_config.py", "--bind", "0.0.0.0:8000", "--workers", "4", "-k", "uvicorn.workers.UvicornWorker", "--timeout", "120", "--graceful-timeout", "30", "-e", "UVICORN_LOOP=uvloop", "-e", "UVICORN_HTTP=httptools", "core.app:app"]
