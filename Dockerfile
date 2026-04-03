# ====== 构建阶段 ======
FROM python:3.12-slim AS builder

WORKDIR /app

# 复制并安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple


# ====== 运行阶段 ======
FROM python:3.12-slim

# 设置时区为中国上海
ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && \
    echo $TZ > /etc/timezone 

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
COPY main.py .
COPY entrypoint.sh .
COPY core/ ./core/
COPY services/ ./services/
COPY adapters/ ./adapters/
COPY templates/ ./templates/
COPY prompts/ ./prompts/
COPY migrations/ ./migrations/

# 注意: 不复制 .env 文件以避免敏感信息打包进镜像
# 部署时通过挂载卷或环境变量方式注入配置

# 创建必要的目录并设置权限
RUN mkdir -p logs webhooks_data && \
    chmod +x entrypoint.sh && \
    chown -R appuser:appuser /app

# 切换到非 root 用户
USER appuser

# 暴露端口
EXPOSE 8000

# 健康检查（使用 Python 原生方式，无需 curl）
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

# 设置启动入口点（自动执行数据库初始化和迁移）
ENTRYPOINT ["./entrypoint.sh"]

# 使用 gunicorn 运行应用(生产环境)
# timeout 120 秒：OpenOcta 分析已改为异步轮询，handler 不再长时间阻塞
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "4", "--timeout", "120", "--graceful-timeout", "30", "main:app"]
