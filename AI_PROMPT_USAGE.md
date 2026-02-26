# AI Prompt 动态配置使用指南

## 快速开始

### 1. 查看当前 Prompt

```bash
# 通过 API 查看
curl http://localhost:5000/api/prompt | jq

# 或者运行测试
python test_prompt_loading.py
```

### 2. 修改 Prompt 模板

**方法 A: 编辑文件（推荐）**

```bash
# 编辑默认模板文件
vim prompts/webhook_analysis.txt

# 保存后重新加载（无需重启服务）
curl -X POST http://localhost:5000/api/prompt/reload
```

**方法 B: 使用环境变量**

```bash
# 在 .env 中添加
AI_USER_PROMPT='你的自定义 prompt 内容，支持 {source} 和 {data_json} 变量'

# 重启服务
python app.py
```

### 3. 验证修改

```bash
# 查看是否生效
curl http://localhost:5000/api/prompt

# 测试分析功能
curl -X POST http://localhost:5000/api/reanalyze/1
```

## 配置说明

### 环境变量

```bash
# 方式 1: 从文件加载（推荐）
AI_USER_PROMPT_FILE=prompts/webhook_analysis.txt

# 方式 2: 直接配置内容（优先级更高）
AI_USER_PROMPT='你的 prompt 内容'
```

### 优先级

1. `AI_USER_PROMPT` 环境变量（最高优先级）
2. `AI_USER_PROMPT_FILE` 指定的文件
3. 默认硬编码模板（fallback）

## 模板变量

在 Prompt 模板中可以使用以下变量：

- `{source}` - Webhook 来源系统名称
- `{data_json}` - Webhook 数据的 JSON 格式字符串

**示例**：

```text
请分析以下来自 {source} 的事件：

数据：
```json
{data_json}
```

请返回 JSON 格式的分析结果...
```

## API 接口

### 获取当前 Prompt

```bash
GET /api/prompt

# 示例
curl http://localhost:5000/api/prompt
```

**响应**：
```json
{
  "success": true,
  "template": "请分析以下 webhook 事件...",
  "source": "file"
}
```

### 重新加载 Prompt

```bash
POST /api/prompt/reload

# 示例
curl -X POST http://localhost:5000/api/prompt/reload
```

**响应**：
```json
{
  "success": true,
  "message": "Prompt 模板已重新加载",
  "template_length": 970,
  "preview": "请分析以下 webhook 事件..."
}
```

## 常见场景

### 场景 1: 开发环境快速迭代

```bash
# 1. 编辑 prompt 文件
vim prompts/webhook_analysis.txt

# 2. 保存后立即重载（无需重启）
curl -X POST http://localhost:5000/api/prompt/reload

# 3. 测试效果
curl -X POST http://localhost:5000/api/reanalyze/1
```

### 场景 2: 针对不同环境使用不同 Prompt

**开发环境** (`.env.development`):
```bash
AI_USER_PROMPT_FILE=prompts/webhook_analysis_dev.txt
```

**生产环境** (`.env.production`):
```bash
AI_USER_PROMPT_FILE=prompts/webhook_analysis_prod.txt
```

### 场景 3: A/B 测试不同 Prompt

```bash
# 创建多个版本
cp prompts/webhook_analysis.txt prompts/v1.txt
cp prompts/webhook_analysis.txt prompts/v2.txt

# 编辑 v2 版本
vim prompts/v2.txt

# 切换测试
export AI_USER_PROMPT_FILE=prompts/v2.txt
curl -X POST http://localhost:5000/api/prompt/reload
```

### 场景 4: Docker 部署

**Dockerfile**:
```dockerfile
# 复制 prompt 模板
COPY prompts/webhook_analysis.txt /app/prompts/

# 设置环境变量
ENV AI_USER_PROMPT_FILE=/app/prompts/webhook_analysis.txt
```

**docker-compose.yml**:
```yaml
services:
  webhook:
    environment:
      - AI_USER_PROMPT_FILE=/app/prompts/webhook_analysis.txt
    volumes:
      - ./prompts:/app/prompts  # 支持热更新
```

## 故障排查

### 问题 1: 修改不生效

**原因**: 使用了缓存

**解决**:
```bash
# 调用重载 API
curl -X POST http://localhost:5000/api/prompt/reload

# 或重启服务
pkill -f "python app.py"
python app.py
```

### 问题 2: 文件找不到

**错误日志**:
```
WARNING - Prompt 模板文件不存在: prompts/webhook_analysis.txt，使用默认模板
```

**检查**:
```bash
# 确认文件存在
ls -la prompts/webhook_analysis.txt

# 检查路径配置
grep AI_USER_PROMPT_FILE .env
```

### 问题 3: 变量格式错误

**错误**:
```python
KeyError: 'data_json'
```

**解决**: 确保模板中只使用 `{source}` 和 `{data_json}` 两个变量

## 测试

### 运行测试脚本

```bash
python test_prompt_loading.py
```

**预期输出**:
```
============================================================
测试 AI Prompt 动态加载功能
============================================================

1️⃣  检查配置
   AI_USER_PROMPT_FILE: prompts/webhook_analysis.txt
   AI_USER_PROMPT (env): 未设置

2️⃣  加载 Prompt 模板
   ✅ 成功加载，长度: 970 字符

...

✅ 所有测试通过
```

## 进阶用法

### 自定义多个 Prompt 模板

```bash
prompts/
├── webhook_analysis.txt          # 默认通用模板
├── aliyun_analysis.txt           # 阿里云专用
├── github_analysis.txt           # GitHub 专用
└── custom_analysis.txt           # 自定义模板
```

### 编程方式切换

```python
from ai_analyzer import reload_user_prompt_template
import os

# 临时切换 prompt
os.environ['AI_USER_PROMPT_FILE'] = 'prompts/aliyun_analysis.txt'
reload_user_prompt_template()

# 分析数据
...

# 切换回默认
os.environ['AI_USER_PROMPT_FILE'] = 'prompts/webhook_analysis.txt'
reload_user_prompt_template()
```

## 最佳实践

1. ✅ **版本控制**: 将 prompt 文件纳入 Git
2. ✅ **环境分离**: 不同环境使用不同 prompt
3. ✅ **定期审查**: 定期优化 prompt 内容
4. ✅ **测试验证**: 修改后通过真实数据验证效果
5. ✅ **备份**: 重要修改前备份原始 prompt

## 相关文件

- `ai_analyzer.py` - AI 分析核心代码
- `config.py` - 配置管理
- `prompts/webhook_analysis.txt` - 默认 Prompt 模板
- `PROMPT_CONFIG.md` - 详细配置文档
- `test_prompt_loading.py` - 测试脚本

## 技术支持

如有问题，请查看：
1. 日志输出: `logs/webhook.log`
2. 配置文档: `PROMPT_CONFIG.md`
3. 运行测试: `python test_prompt_loading.py`
