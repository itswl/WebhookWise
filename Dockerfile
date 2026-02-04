# ====== 构建阶段 ======
FROM swr.cn-north-4.myhuaweicloud.com/ddn-k8s/docker.io/library/python:3.12-slim AS builder

WORKDIR /app

# 复制并安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple


# ====== 运行阶段 ======
FROM swr.cn-north-4.myhuaweicloud.com/ddn-k8s/docker.io/library/python:3.12-slim

# 设置时区为中国上海
ENV TZ=Asia/Shanghai
RUN apt-get update && \
    apt-get install -y --no-install-recommends tzdata curl && \
    ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && \
    echo $TZ > /etc/timezone && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

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
COPY .env.example .env
COPY ai_analyzer.py .
COPY app.py .
COPY config.py .
COPY logger.py .
COPY migrate_db.py .
COPY models.py .
COPY utils.py .
COPY templates/ ./templates/

# 注意: 不复制 .env 文件以避免敏感信息打包进镜像
# 部署时通过挂载卷或环境变量方式注入配置

# 创建必要的目录并设置权限
RUN mkdir -p logs webhooks_data && \
    chown -R appuser:appuser /app

# 切换到非 root 用户
USER appuser

# 暴露端口
EXPOSE 8000

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# 使用 gunicorn 运行应用(生产环境)
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "4", "--timeout", "120", "app:app"]
