# 使用官方 Python 运行时作为基础镜像
FROM swr.cn-north-4.myhuaweicloud.com/ddn-k8s/docker.io/library/python:3.11-slim


# 设置时区为中国上海
ENV TZ=Asia/Shanghai
RUN apt-get update && \
    apt-get install -y tzdata && \
    ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && \
    echo $TZ > /etc/timezone && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# 设置工作目录
WORKDIR /app

# 设置环境变量
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

# 复制依赖文件
COPY requirements.txt .

# 安装依赖
RUN pip install --no-cache-dir -r requirements.txt  -i https://pypi.tuna.tsinghua.edu.cn/simple

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

# 创建必要的目录
RUN mkdir -p logs webhooks_data

# 暴露端口
EXPOSE 8000

# 使用 gunicorn 运行应用(生产环境)
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "4", "--timeout", "120", "app:app"]
