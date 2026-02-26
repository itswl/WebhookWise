# 修复配置保存权限错误

## 问题

在 Web 界面保存配置时出现：
```
❌ 保存失败: [Errno 1] Operation not permitted
```

## 快速解决

### 方法 1: 使用环境变量（推荐 ⭐）

不通过 Web 界面修改配置，直接在 `docker-compose.yml` 中配置：

```yaml
version: '3.8'

services:
  webhook:
    image: your-image
    environment:
      # AI 配置
      - ENABLE_AI_ANALYSIS=true
      - OPENAI_API_KEY=your-key-here
      - OPENAI_API_URL=https://openrouter.ai/api/v1
      - OPENAI_MODEL=anthropic/claude-sonnet-4

      # Prompt 配置
      - AI_USER_PROMPT_FILE=prompts/webhook_analysis_detailed.txt

      # 转发配置
      - ENABLE_FORWARD=true
      - FORWARD_URL=https://your-webhook-url

      # 去重配置
      - DUPLICATE_ALERT_TIME_WINDOW=24
      - FORWARD_DUPLICATE_ALERTS=false

    # 挂载 prompt 文件（可选）
    volumes:
      - ./prompts:/app/prompts:ro  # 只读挂载
```

修改后重启：
```bash
docker-compose down
docker-compose up -d
```

### 方法 2: 修复 Docker 容器权限

如果必须使用 Web 界面保存配置，修改 `docker-compose.yml`：

```yaml
services:
  webhook:
    volumes:
      - ./.env:/app/.env:rw  # 确保可读写
    user: "${UID:-1000}:${GID:-1000}"  # 使用宿主机用户 ID
```

设置环境变量：
```bash
# .bashrc 或 .zshrc
export UID=$(id -u)
export GID=$(id -g)
```

重启容器：
```bash
docker-compose down
docker-compose up -d
```

### 方法 3: 只查看配置，不保存

Web 界面只用于查看配置，所有修改通过以下方式：

1. **临时修改**（重启后失效）：
   ```bash
   docker exec webhook-container env ENABLE_AI_ANALYSIS=false
   ```

2. **永久修改**：
   ```bash
   # 编辑 docker-compose.yml
   vim docker-compose.yml

   # 重启
   docker-compose up -d
   ```

## 诊断工具

运行诊断脚本检查权限：

```bash
# 本地环境
./diagnose_permissions.sh

# Docker 容器内
docker exec webhook-container /app/diagnose_permissions.sh
```

## 已实现的改进

1. **更好的错误处理**
   - 使用临时文件 + 原子替换
   - 捕获 PermissionError 并提供友好提示
   - 详细的错误日志

2. **前端优化**
   - 权限错误时显示详细帮助信息
   - 控制台输出完整调试信息
   - 保存成功后自动刷新配置

3. **文档完善**
   - `CONFIG_SAVE_ISSUE.md` - 详细解决方案
   - `FIX_PERMISSION_ERROR.md` - 快速修复指南
   - `diagnose_permissions.sh` - 自动诊断脚本

## 推荐方案对比

| 方案 | 优点 | 缺点 | 适用场景 |
|------|------|------|----------|
| **环境变量** ⭐ | 安全、简单、无权限问题 | 需要重启容器 | 生产环境 |
| 修复权限 | Web 界面可用 | 配置复杂、有安全风险 | 测试环境 |
| 只读模式 | 查看方便 | 无法在线修改 | 只读访问 |

## 为什么推荐环境变量？

1. ✅ **安全性**: 敏感信息不写入文件
2. ✅ **简单性**: 无需处理文件权限
3. ✅ **标准化**: 符合 12-Factor App
4. ✅ **云原生**: 易于 K8s、Docker 部署
5. ✅ **灵活性**: 不同环境不同配置

## 配置示例

### 开发环境

```bash
# .env 文件
ENABLE_AI_ANALYSIS=true
OPENAI_API_KEY=sk-xxx
# ...其他配置

# 直接运行
python app.py
```

### Docker 开发

```yaml
# docker-compose.yml
services:
  webhook:
    env_file: .env
    volumes:
      - .:/app
```

### Docker 生产

```yaml
# docker-compose.yml
services:
  webhook:
    environment:
      - ENABLE_AI_ANALYSIS=true
      - OPENAI_API_KEY=${OPENAI_API_KEY}  # 从宿主机环境读取
```

### Kubernetes

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: webhook-config
data:
  ENABLE_AI_ANALYSIS: "true"
---
apiVersion: v1
kind: Secret
metadata:
  name: webhook-secrets
stringData:
  OPENAI_API_KEY: "your-key"
```

## 验证配置

```bash
# 查看当前配置
curl http://localhost:5000/api/config

# 查看容器环境变量
docker exec webhook-container env | grep OPENAI

# 测试 AI 功能
curl -X POST http://localhost:5000/api/reanalyze/1
```

## 常见问题

### Q: Web 界面的配置保存功能还能用吗？

**A**: 取决于你的部署方式：
- 本地运行：✅ 可用
- Docker（默认配置）：❌ 可能报错
- Docker（修复权限后）：✅ 可用
- 推荐：使用环境变量，禁用 Web 保存

### Q: 如何在不重启容器的情况下修改配置？

**A**: 使用 Prompt 热重载功能：
```bash
# 修改 prompt 文件
vim prompts/webhook_analysis_detailed.txt

# 热重载
curl -X POST http://localhost:5000/api/prompt/reload
```

其他配置建议使用环境变量 + 容器重启。

### Q: 生产环境最佳实践？

**A**:
1. 使用环境变量或 K8s ConfigMap/Secret
2. 禁用 Web 配置保存功能
3. Prompt 文件只读挂载
4. 所有配置纳入版本控制（除敏感信息）

## 总结

✅ **推荐方案**: 使用环境变量配置
✅ **备选方案**: 修复 Docker 权限
⚠️ **不推荐**: 在生产环境使用 Web 界面修改配置

如有问题，运行 `./diagnose_permissions.sh` 诊断。
