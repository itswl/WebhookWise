# AI Prompt 动态配置说明

## 概述

AI 分析模块的 User Prompt 已改为可配置的动态加载方式，支持三种配置方式：

1. **环境变量直接配置** - 最高优先级
2. **从文件加载** - 默认方式
3. **硬编码默认模板** - fallback

## 配置方式

### 方式 1: 环境变量直接配置（最高优先级）

在 `.env` 文件中直接设置 prompt 内容：

```bash
AI_USER_PROMPT='请分析以下 webhook 事件：

**来源**: {source}
**数据内容**:
```json
{data_json}
```

... 你的自定义 prompt ...'
```

**优点**：
- 无需额外文件
- 适合容器化部署
- 可通过环境变量快速切换

**缺点**：
- 不适合很长的 prompt
- 需要转义特殊字符

### 方式 2: 从文件加载（推荐，默认方式）

**配置文件路径**：

在 `.env` 文件中配置：

```bash
AI_USER_PROMPT_FILE=prompts/webhook_analysis.txt
```

**默认路径**：`prompts/webhook_analysis.txt`

**文件格式**：

```text
请分析以下 webhook 事件：

**来源**: {source}
**数据内容**:
```json
{data_json}
```

请按照以下 JSON 格式返回分析结果：
...
```

**模板变量**：

- `{source}` - Webhook 来源系统
- `{data_json}` - Webhook 数据的 JSON 格式字符串

**优点**：
- 易于编辑和管理
- 支持长内容
- 可以版本控制
- 支持热重载

**示例文件**：

已创建默认模板文件 `/Users/imwl/webhooks/prompts/webhook_analysis.txt`

### 方式 3: 硬编码默认模板（fallback）

如果以上两种方式都未配置，系统会使用代码中的默认模板。

## 使用方法

### 修改 Prompt 模板

#### 方法 1: 编辑文件

```bash
# 编辑 prompt 文件
vim prompts/webhook_analysis.txt

# 或者创建自定义 prompt 文件
cp prompts/webhook_analysis.txt prompts/custom_prompt.txt
vim prompts/custom_prompt.txt

# 在 .env 中指定新文件
echo "AI_USER_PROMPT_FILE=prompts/custom_prompt.txt" >> .env
```

#### 方法 2: 通过环境变量

```bash
# 在 .env 中设置
AI_USER_PROMPT='你的自定义 prompt，支持 {source} 和 {data_json} 变量'
```

### 热重载 Prompt

#### 方法 1: 通过 API

```bash
# 重新加载 prompt 模板（无需重启服务）
curl -X POST http://localhost:5000/api/prompt/reload

# 查看当前 prompt 模板
curl http://localhost:5000/api/prompt
```

#### 方法 2: 重启服务

```bash
# 重启 Flask 服务
pkill -f "python main.py"
python main.py
```

## API 接口

### 1. 获取当前 Prompt 模板

**请求**：
```bash
GET /api/prompt
```

**响应**：
```json
{
  "success": true,
  "template": "请分析以下 webhook 事件...",
  "source": "file"  // 可能是 "environment", "file", "default"
}
```

### 2. 重新加载 Prompt 模板

**请求**：
```bash
POST /api/prompt/reload
```

**响应**：
```json
{
  "success": true,
  "message": "Prompt 模板已重新加载",
  "template_length": 1234,
  "preview": "请分析以下 webhook 事件..."
}
```

## 模板编写指南

### 必需的占位符

Prompt 模板中必须包含以下占位符：

- `{source}` - Webhook 来源
- `{data_json}` - Webhook 数据（JSON 格式）

### 推荐的结构

```text
1. 任务描述
   - 告诉 AI 要做什么

2. 输入数据
   - 使用 {source} 和 {data_json}

3. 输出格式要求
   - 指定返回的 JSON 结构

4. 分类标准
   - importance: high/medium/low 的判断标准

5. 特殊规则
   - 针对特定类型数据的处理规则

6. 重要提示
   - JSON 格式要求等注意事项
```

### 示例模板

```text
请分析以下 webhook 事件：

**来源**: {source}
**数据**:
```json
{data_json}
```

**要求**：返回 JSON 格式分析结果：
{{
  "importance": "high/medium/low",
  "summary": "简短摘要",
  "actions": ["建议1", "建议2"]
}}

**重要性判断**：
- high: 严重错误、服务不可用
- medium: 警告、性能问题
- low: 正常事件、信息通知
```

## 环境变量参考

```bash
# System Prompt（AI 角色设定）
AI_SYSTEM_PROMPT='你是一个专业的 DevOps 专家...'

# User Prompt 文件路径
AI_USER_PROMPT_FILE=prompts/webhook_analysis.txt

# User Prompt 直接内容（优先级高于文件）
AI_USER_PROMPT='你的 prompt 内容...'

# OpenAI API 配置
OPENAI_API_KEY=sk-xxx
OPENAI_API_URL=https://openrouter.ai/api/v1
OPENAI_MODEL=anthropic/claude-sonnet-4

# AI 功能开关
ENABLE_AI_ANALYSIS=true
```

## 最佳实践

### 1. 开发环境

使用文件方式，方便随时修改：

```bash
AI_USER_PROMPT_FILE=prompts/webhook_analysis.txt
```

### 2. 生产环境

两种推荐方案：

**方案 A：文件 + 版本控制**
```bash
# 将 prompt 文件纳入版本控制
git add prompts/webhook_analysis.txt
git commit -m "update: AI prompt template"
```

**方案 B：环境变量（容器化）**
```dockerfile
# Dockerfile
ENV AI_USER_PROMPT_FILE=/app/prompts/webhook_analysis.txt
COPY prompts/webhook_analysis.txt /app/prompts/
```

### 3. 测试不同 Prompt

创建多个模板文件：

```bash
prompts/
├── webhook_analysis.txt          # 默认
├── webhook_analysis_detailed.txt # 详细版
├── webhook_analysis_simple.txt   # 简化版
└── webhook_analysis_custom.txt   # 自定义
```

通过修改 `.env` 切换：

```bash
# 测试详细版
AI_USER_PROMPT_FILE=prompts/webhook_analysis_detailed.txt

# 重新加载
curl -X POST http://localhost:5000/api/prompt/reload
```

## 故障排查

### 问题 1: Prompt 未生效

**检查加载顺序**：
```bash
# 查看当前使用的 prompt 来源
curl http://localhost:5000/api/prompt | jq '.source'

# 可能的值：
# - "environment" - 使用环境变量 AI_USER_PROMPT
# - "file" - 使用文件 AI_USER_PROMPT_FILE
# - "default" - 使用硬编码默认值
```

**解决方案**：
1. 检查 `.env` 文件配置
2. 检查文件路径是否正确
3. 调用 `/api/prompt/reload` 重新加载

### 问题 2: 文件找不到

**错误日志**：
```
WARNING - Prompt 模板文件不存在: /path/to/file，使用默认模板
```

**解决方案**：
```bash
# 检查文件是否存在
ls -la prompts/webhook_analysis.txt

# 检查相对路径（相对于 services/ai_analyzer.py 所在目录）
# 或使用绝对路径
AI_USER_PROMPT_FILE=/absolute/path/to/prompt.txt
```

### 问题 3: 模板变量错误

**错误**：
```python
KeyError: 'data_json'
```

**原因**：模板中使用了未定义的变量

**解决方案**：只使用 `{source}` 和 `{data_json}` 两个变量

## 高级用法

### 1. 针对不同来源使用不同 Prompt

修改 `ai_analyzer.py` 增加逻辑：

```python
def load_user_prompt_template(source: str = None) -> str:
    """根据 source 加载不同的 prompt"""
    if source:
        custom_file = f"prompts/{source}_analysis.txt"
        if Path(custom_file).exists():
            # 加载特定来源的 prompt
            ...
    # fallback 到默认 prompt
    ...
```

### 2. 使用模板引擎

安装 Jinja2：

```bash
pip install jinja2
```

在 prompt 文件中使用 Jinja2 语法：

```jinja2
请分析以下来自 {{ source }} 的 webhook 事件：

{% if source == 'aliyun' %}
特别注意：这是阿里云监控告警
{% endif %}

数据：
{{ data_json }}
```

## 总结

- ✅ **默认方式**：使用文件 `prompts/webhook_analysis.txt`
- ✅ **热重载**：调用 `POST /api/prompt/reload` 无需重启
- ✅ **版本控制**：Prompt 文件可纳入 Git
- ✅ **灵活切换**：通过环境变量快速切换不同 prompt
- ✅ **容器友好**：支持环境变量和文件两种方式

修改 prompt 后，记得调用 reload API 或重启服务使其生效！
