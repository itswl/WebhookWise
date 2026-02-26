# AI Prompt 动态配置 - 更新日志

## 功能概述

将原本硬编码在 `ai_analyzer.py` 中的 `user_prompt` 改为可配置的动态加载方式。

## 主要改进

### 1. 支持三种配置方式

| 方式 | 优先级 | 使用场景 |
|------|--------|----------|
| 环境变量 `AI_USER_PROMPT` | 最高 | 容器化部署、临时测试 |
| 文件 `AI_USER_PROMPT_FILE` | 中等 | 开发环境、版本控制（推荐） |
| 硬编码默认值 | 最低 | Fallback，确保系统可用 |

### 2. 支持热重载

- 无需重启服务即可更新 prompt
- 提供 API 接口 `/api/prompt/reload`
- 自动缓存，提高性能

### 3. 易于管理

- Prompt 内容可纳入版本控制
- 支持不同环境使用不同 prompt
- 清晰的配置优先级

## 新增文件

```
/Users/imwl/webhooks/
├── prompts/
│   └── webhook_analysis.txt       # 默认 Prompt 模板文件
├── ai_analyzer.py                 # 修改：添加动态加载逻辑
├── config.py                      # 修改：添加 Prompt 配置项
├── app.py                         # 修改：添加 Prompt API 端点
├── .env.example                   # 修改：添加配置示例
├── PROMPT_CONFIG.md               # 新增：详细配置文档
├── AI_PROMPT_USAGE.md             # 新增：使用指南
├── test_prompt_loading.py         # 新增：功能测试脚本
└── CHANGELOG_PROMPT.md            # 本文件
```

## 代码变更

### 1. `ai_analyzer.py`

**新增函数**:
- `load_user_prompt_template()` - 加载 Prompt 模板
- `reload_user_prompt_template()` - 重新加载模板（清除缓存）

**修改函数**:
- `analyze_with_openai()` - 使用动态加载的模板替代硬编码

**关键代码**:
```python
# 加载模板
prompt_template = load_user_prompt_template()

# 格式化
user_prompt = prompt_template.format(
    source=source,
    data_json=data_json
)
```

### 2. `config.py`

**新增配置**:
```python
# AI User Prompt 配置
AI_USER_PROMPT_FILE = os.getenv('AI_USER_PROMPT_FILE', 'prompts/webhook_analysis.txt')
AI_USER_PROMPT = os.getenv('AI_USER_PROMPT', '')
```

### 3. `app.py`

**新增 API 端点**:

- `GET /api/prompt` - 获取当前 Prompt 模板
- `POST /api/prompt/reload` - 重新加载 Prompt 模板

### 4. `.env.example`

**新增配置项**:
```bash
# User Prompt 配置
AI_USER_PROMPT_FILE=prompts/webhook_analysis.txt
# AI_USER_PROMPT=直接内容（可选）
```

## 使用示例

### 开发环境快速迭代

```bash
# 1. 编辑模板
vim prompts/webhook_analysis.txt

# 2. 热重载
curl -X POST http://localhost:5000/api/prompt/reload

# 3. 测试
curl -X POST http://localhost:5000/api/reanalyze/1
```

### 生产环境部署

**Docker Compose**:
```yaml
services:
  webhook:
    environment:
      - AI_USER_PROMPT_FILE=/app/prompts/webhook_analysis.txt
    volumes:
      - ./prompts:/app/prompts
```

### 测试验证

```bash
# 运行测试
python test_prompt_loading.py

# 输出
✅ 所有测试通过
```

## 向后兼容性

- ✅ 完全兼容旧版本
- ✅ 未配置时使用默认模板（与原硬编码相同）
- ✅ 不影响现有功能

## API 文档

### GET /api/prompt

获取当前使用的 Prompt 模板

**响应**:
```json
{
  "success": true,
  "template": "完整的 prompt 内容",
  "source": "file"  // 来源: environment/file/default
}
```

### POST /api/prompt/reload

重新加载 Prompt 模板（清除缓存）

**响应**:
```json
{
  "success": true,
  "message": "Prompt 模板已重新加载",
  "template_length": 970,
  "preview": "前200字符预览..."
}
```

## 配置优先级

```
AI_USER_PROMPT (环境变量直接内容)
    ↓ 如果未设置
AI_USER_PROMPT_FILE (文件路径)
    ↓ 如果文件不存在
默认硬编码模板
```

## 模板变量

Prompt 模板中可用的变量：

- `{source}` - Webhook 来源系统
- `{data_json}` - Webhook 数据的 JSON 字符串

**示例**:
```text
请分析来自 {source} 的事件：

```json
{data_json}
```
```

## 性能优化

- ✅ 模板加载后自动缓存
- ✅ 避免重复读取文件
- ✅ 仅在需要时重载（调用 reload API）

## 安全性

- ✅ 模板变量使用 `str.format()` 安全格式化
- ✅ 仅支持预定义变量 `{source}` 和 `{data_json}`
- ✅ 没有代码注入风险

## 测试覆盖

✅ 配置加载测试
✅ 文件读取测试
✅ 变量格式化测试
✅ 重载功能测试
✅ Fallback 机制测试

## 未来扩展

可能的增强方向：

1. **多模板支持**: 根据 source 自动选择不同模板
2. **模板版本管理**: 支持模板版本回滚
3. **在线编辑**: 通过 Web 界面编辑 prompt
4. **模板市场**: 预置多种场景的模板供选择
5. **A/B 测试**: 支持同时运行多个 prompt 版本对比效果

## 迁移指南

### 从旧版本迁移

**步骤**:

1. 更新代码到最新版本
2. （可选）创建自定义 prompt 文件
3. （可选）在 `.env` 中配置 `AI_USER_PROMPT_FILE`
4. 重启服务

**注意**: 如果不做任何配置，系统会使用默认模板，功能与之前完全相同。

## 故障排查

### Prompt 未生效

```bash
# 检查配置
curl http://localhost:5000/api/prompt | jq '.source'

# 重新加载
curl -X POST http://localhost:5000/api/prompt/reload
```

### 文件找不到

```bash
# 检查文件路径
ls -la prompts/webhook_analysis.txt

# 查看日志
tail -f logs/webhook.log | grep prompt
```

## 相关文档

- 📖 [详细配置文档](PROMPT_CONFIG.md)
- 📖 [使用指南](AI_PROMPT_USAGE.md)
- 🧪 [测试脚本](test_prompt_loading.py)

## 贡献者

- 实现日期: 2026-02-26
- 功能: AI Prompt 动态配置

## 许可

与主项目相同
