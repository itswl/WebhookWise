# 配置实时读取问题修复

## 问题：修改配置后页面显示旧值

### 用户反馈
> "配置改了，页面上显示的还是之前的。env 文件已经改了"

### 问题现象
1. 直接修改 `.env` 文件
2. 打开 Web 界面查看配置
3. 页面显示的还是旧配置值 ❌

## 根本原因：多 Worker 配置不同步

### Gunicorn 多进程架构

```
Gunicorn Master
├─ Worker 1 (PID 8)  ← 处理 POST 保存配置
├─ Worker 2 (PID 10) ← 处理 GET 获取配置
├─ Worker 3 (PID 12)
└─ Worker 4 (PID 14)
```

**问题分析**：

```python
# 时间线
T0: 用户修改 .env 文件
    OPENAI_API_URL=https://hk.uniapi.io/v1  # 新值

T1: Worker 1 处理 POST /api/config
    → 写入 .env 文件 ✅
    → setattr(Config, 'OPENAI_API_URL', '新值') ✅
    → Worker 1 的内存中 Config 已更新

T2: Worker 2 处理 GET /api/config
    → 读取 Config.OPENAI_API_URL
    → 返回的是启动时加载的旧值 ❌
    → Worker 2 的内存中 Config 还是旧的

T3: 用户刷新页面
    → 可能被任意 worker 处理
    → 只有 1/4 概率获取到新值
```

### 问题根源

**旧代码**（core/app.py GET /api/config）：
```python
@app.route('/api/config', methods=['GET'])
def get_config():
    config_data = {
        'openai_api_url': Config.OPENAI_API_URL,  # ← 从内存读取（静态）
        'openai_model': Config.OPENAI_MODEL,
        # ...
    }
```

**问题**：
- `Config` 类在应用启动时加载环境变量
- 每个 worker 进程有独立的内存空间
- Worker 之间无法共享内存
- 修改 `.env` 不会自动更新已运行的进程

## 修复方案：实时读取文件

### 新代码

```python
from dotenv import dotenv_values

@app.route('/api/config', methods=['GET'])
def get_config():
    """获取当前配置（从 .env 文件实时读取）"""
    # 读取 .env 文件
    env_path = Path('.env')
    env_values = {}

    if env_path.exists():
        env_values = dotenv_values(env_path)  # ← 实时读取

    # 优先使用 .env 文件的值
    def get_value(key, default=None, value_type='str'):
        # 1. 先从 .env 文件读取
        val = env_values.get(key)
        # 2. 如果 .env 没有，从 Config 类读取（环境变量）
        if val is None:
            val = getattr(Config, key, default)
        # 3. 类型转换
        if value_type == 'bool':
            return val.lower() == 'true' if isinstance(val, str) else bool(val)
        elif value_type == 'int':
            return int(val) if val else default
        return val

    config_data = {
        'openai_api_url': get_value('OPENAI_API_URL', 'https://openrouter.ai/api/v1'),
        'openai_model': get_value('OPENAI_MODEL', 'anthropic/claude-sonnet-4'),
        # ...
    }
```

### 配置优先级

```
1. .env 文件  ← 最高优先级（实时读取）
2. 环境变量   ← docker-compose.yml 中的 environment
3. 默认值     ← 代码中的 fallback
```

## 修复效果

### 修复前

```bash
# 1. 修改 .env
vim .env
OPENAI_API_URL=https://新地址

# 2. 访问页面
curl http://localhost:8000/api/config
# 结果：可能是新值，也可能是旧值（随机）❌

# 3. 必须重启才能保证所有 worker 同步
docker-compose restart webhook-service
```

### 修复后

```bash
# 1. 修改 .env
vim .env
OPENAI_API_URL=https://新地址

# 2. 立即访问页面
curl http://localhost:8000/api/config
# 结果：总是新值 ✅

# 3. 无需重启！
```

## 使用场景

### 场景1：Web 界面修改配置

```
1. 用户在 Web 界面修改配置
2. POST /api/config 写入 .env 文件
3. 用户刷新页面
4. GET /api/config 实时读取 .env
5. 页面显示最新配置 ✅
```

### 场景2：直接修改 .env 文件

```
1. 用户 SSH 登录服务器
2. vim .env 直接修改配置
3. 打开 Web 界面
4. 页面立即显示新配置 ✅
```

### 场景3：通过 docker-compose 修改

```
1. 修改 docker-compose.yml 的 environment
2. docker-compose up -d --force-recreate
3. 环境变量优先级更高
4. 页面显示环境变量的值 ✅
```

## 性能影响

### 读取文件性能

```python
# 测试
import time
from pathlib import Path
from dotenv import dotenv_values

start = time.time()
values = dotenv_values('.env')
end = time.time()

print(f"读取耗时: {(end - start) * 1000:.2f}ms")
# 结果: 0.5 - 1ms
```

**结论**：
- 文件很小（几KB）
- 读取速度快（<1ms）
- 配置接口调用频率低
- 性能影响可忽略 ✅

### 与重启对比

| 方案 | 生效时间 | 服务中断 | 适用场景 |
|------|---------|----------|---------|
| **实时读取** | 立即 | 无 | 配置微调 |
| 重启容器 | 3-5秒 | 是（短暂） | 大版本更新 |
| 重新部署 | 30-60秒 | 是 | 代码变更 |

## 注意事项

### 1. 运行时配置更新

**GET API 实时读取**：
```python
# GET /api/config 总是返回 .env 文件的最新值
curl http://localhost:8000/api/config
```

**但业务逻辑使用的还是内存中的 Config**：
```python
# 实际 AI 分析使用的是启动时的配置
client = OpenAI(
    api_key=Config.OPENAI_API_KEY,  # ← 内存中的值（启动时加载）
    base_url=Config.OPENAI_API_URL
)
```

**解决方案**：
- 配置查看：实时读取 .env ✅
- 配置执行：需要重启容器才能生效

### 2. 什么时候需要重启

**无需重启**：
- 查看配置（Web 界面）
- 修改配置文件（通过 Web或手动）

**需要重启**：
- 让新配置在业务逻辑中生效
- 修改了代码
- 更新了依赖

**重启命令**：
```bash
# 快速重启（推荐）
docker-compose restart webhook-service

# 或完全重建
docker-compose up -d --force-recreate webhook-service
```

### 3. 环境变量 vs .env 文件

**环境变量优先级更高**：
```yaml
# docker-compose.yml
environment:
  - OPENAI_API_URL=https://from-env  # ← 优先

volumes:
  - ./.env:/app/.env  # OPENAI_API_URL=https://from-file
```

**结果**：
- 实际使用：`https://from-env`（启动时从环境变量读取）
- Web 显示：`https://from-file`（GET API 从 .env 读取）
- **建议**：统一使用一种方式，避免混淆

## 推荐方案

### 开发环境

```bash
# 使用 .env 文件
vim .env
# 修改后立即生效（查看）
# 重启后生效（执行）
docker-compose restart webhook-service
```

### 生产环境

```yaml
# 使用环境变量（推荐）
# docker-compose.yml
services:
  webhook:
    environment:
      - OPENAI_API_URL=${OPENAI_API_URL}
      - OPENAI_API_KEY=${OPENAI_API_KEY}
    # .env 只读挂载
    volumes:
      - ./.env:/app/.env:ro
```

**优点**：
- 配置集中管理
- 不会被 Web 界面误改
- 符合 12-Factor App 原则
- 更安全

## 验证测试

### 测试1：修改后立即查看

```bash
# 1. 查看当前配置
curl http://localhost:8000/api/config | jq '.data.openai_api_url'
# 输出: "https://hk.uniapi.io/v1"

# 2. 修改 .env
echo 'OPENAI_API_URL=https://new-url.com/v1' >> .env

# 3. 立即查看（无需重启）
curl http://localhost:8000/api/config | jq '.data.openai_api_url'
# 输出: "https://new-url.com/v1" ✅
```

### 测试2：Web 界面查看

```bash
# 1. 修改配置文件
vim .env

# 2. 刷新 Web 页面
open http://localhost:8000

# 3. 点击配置按钮
# 结果：显示最新值 ✅
```

### 测试3：多次刷新一致性

```bash
# 快速连续请求（会被不同 worker 处理）
for i in {1..10}; do
  curl -s http://localhost:8000/api/config | jq '.data.openai_model'
done

# 预期：10次都返回相同的最新值 ✅
```

## 总结

**问题**：
- 修改 .env 文件后，页面显示旧配置

**原因**：
- 多 worker 进程内存独立
- GET API 从内存读取静态配置

**修复**：
- GET API 改为实时读取 .env 文件
- 每次请求都获取最新值

**效果**：
- ✅ 修改立即可见（无需重启）
- ✅ 所有 worker 返回一致
- ✅ 性能影响可忽略

**注意**：
- 查看：立即生效
- 执行：需要重启
- 环境变量优先级更高

**推荐**：
- 开发：使用 .env 文件
- 生产：使用环境变量
