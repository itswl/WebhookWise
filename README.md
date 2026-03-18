# Webhook 接收与 AI 分析服务

一个智能的 Webhook 接收服务，具备 AI 分析、重复告警去重、自动转发等功能。

## 功能特性

### 核心功能

- ✅ **Webhook 接收** - 支持多来源 Webhook 事件接收
- ✅ **AI 智能分析** - 基于 OpenAI API 自动分析事件重要性和风险
- ✅ **重复告警去重** - 智能识别重复告警，避免重复分析和通知
- ✅ **自动转发** - 高风险事件自动转发到飞书等通知平台
- ✅ **数据持久化** - PostgreSQL 数据库存储所有事件记录
- ✅ **可视化界面** - Web 界面查看历史事件和分析结果
- ✅ **灵活配置** - 支持环境变量和 API 动态配置

### 高级特性

- 🔄 **重复告警去重** - 基于关键字段生成唯一标识，智能检测重复告警
- ⏱️ **可配置时间窗口** - 自定义重复检测的时间范围（默认 24 小时）
- 🎯 **转发策略控制** - 灵活配置是否转发重复告警
- 📊 **实时统计** - 重复次数统计和趋势分析
- 🔐 **签名验证** - 支持 HMAC-SHA256 签名验证确保安全

## 快速开始

### 环境要求

- Python 3.8+
- PostgreSQL 12+
- OpenAI API Key（可选，用于 AI 分析）

### 安装步骤

1. **克隆项目**
```bash
git clone <repository-url>
cd webhooks
```

2. **创建虚拟环境**
```bash
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
```

3. **安装依赖**
```bash
pip install -r requirements.txt
```

4. **配置环境变量**
```bash
cp .env.example .env
# 编辑 .env 文件，配置数据库和 API 密钥
```

5. **初始化数据库**
```bash
# 创建数据库
createdb webhooks

# 运行迁移
python -c "from core.models import init_db; init_db()"
python -m migrations.migrate_db  # 添加重复告警去重字段
```

6. **启动服务**
```bash
python main.py
```

服务将在 `http://localhost:8000` 启动


### docker 启动
使用 docker-compose

1. **克隆项目**
```bash
git clone <repository-url>
cd webhooks
```

2. **配置环境变量**
```bash
cp .env.example .env
# 编辑 .env 文件，配置数据库和 API 密钥
```

3. **docker compose 启动** 
```
docker compose up -d --build --force-recreate
```


## 配置说明
环境变量 （docker-compose.yml里的值） >  .env 文件
### 环境变量

在 `.env` 文件中配置以下参数：

```bash
# 服务器配置
PORT=8000
HOST=0.0.0.0
FLASK_ENV=development

# 数据库配置
DATABASE_URL=postgresql://username:password@localhost:5432/webhooks

# 安全配置
WEBHOOK_SECRET=your-secret-key-here

# AI 分析配置
ENABLE_AI_ANALYSIS=true
OPENAI_API_KEY=your-openai-api-key
OPENAI_API_URL=https://openrouter.ai/api/v1
OPENAI_MODEL=anthropic/claude-sonnet-4

# AI Prompt 配置（支持动态加载）
AI_SYSTEM_PROMPT=你是一个专业的 DevOps 和系统运维专家...
AI_USER_PROMPT_FILE=prompts/webhook_analysis.txt  # 推荐：从文件加载
# AI_USER_PROMPT=你的自定义 prompt...  # 或直接设置内容

# 转发配置
ENABLE_FORWARD=true
FORWARD_URL=https://open.feishu.cn/open-apis/bot/v2/hook/YOUR_WEBHOOK_KEY

# 重复告警去重配置
DUPLICATE_ALERT_TIME_WINDOW=24         # 时间窗口（小时）
FORWARD_DUPLICATE_ALERTS=false         # 窗口内重复告警是否转发

# 超时间窗口后的行为配置（新增）
REANALYZE_AFTER_TIME_WINDOW=true      # 超时间窗口后是否重新分析
FORWARD_AFTER_TIME_WINDOW=true        # 超时间窗口后是否推送转发
```

### AI Prompt 动态配置 🆕

系统支持动态加载和修改 AI 分析的 Prompt 模板，无需修改代码。

#### 配置方式

**方式 1: 从文件加载（推荐）**
```bash
AI_USER_PROMPT_FILE=prompts/webhook_analysis.txt
```

**方式 2: 直接设置内容**
```bash
AI_USER_PROMPT='你的自定义 prompt，支持 {source} 和 {data_json} 变量'
```

#### 快速使用

```bash
# 1. 编辑 prompt 模板
vim prompts/webhook_analysis.txt

# 2. 热重载（无需重启）
curl -X POST http://localhost:5000/api/prompt/reload

# 3. 查看当前 prompt
curl http://localhost:5000/api/prompt
```

#### 预置模板

- `prompts/webhook_analysis.txt` - 默认通用模板
- `prompts/webhook_analysis_simple.txt` - 简化版模板
- `prompts/webhook_analysis_detailed.txt` - 详细版模板

#### 更多信息

- 📖 [Prompt 配置详细文档](PROMPT_CONFIG.md)
- 📖 [Prompt 使用指南](AI_PROMPT_USAGE.md)
- 📝 [更新日志](CHANGELOG_PROMPT.md)

### 重复告警去重配置详解

#### 时间窗口配置
- **参数**: `DUPLICATE_ALERT_TIME_WINDOW`
- **默认值**: 24（小时）
- **说明**: 在此时间窗口内，相同的告警会被识别为重复
- **示例**: 设置为 1 表示 1 小时内的重复告警会被去重
- **取值范围**: 1-168（7天）

#### 窗口内转发策略
- **参数**: `FORWARD_DUPLICATE_ALERTS`
- **默认值**: false
- **选项**:
  - `false`: 窗口内重复告警不自动转发（推荐，减少噪音）
  - `true`: 窗口内重复告警的高风险事件仍然转发
- **说明**: 窗口内重复告警都会跳过 AI 分析，复用原始分析结果

#### 超时间窗口后的行为配置 🆕

**1. 重新分析配置**
- **参数**: `REANALYZE_AFTER_TIME_WINDOW`
- **默认值**: true
- **选项**:
  - `true`: 超过时间窗口后重新调用 AI 分析（产生费用，但保证结果最新）
  - `false`: 复用历史分析结果（节省 AI API 费用）
- **适用场景**:
  - 告警内容可能变化 → 设置 `true`
  - 告警内容固定 → 设置 `false` 节省费用

**2. 转发配置**
- **参数**: `FORWARD_AFTER_TIME_WINDOW`
- **默认值**: true
- **选项**:
  - `true`: 超过时间窗口后仍推送高风险告警（定期提醒）
  - `false`: 不推送，避免重复通知
- **适用场景**:
  - 需要定期关注持续问题 → 设置 `true`
  - 避免通知疲劳 → 设置 `false`

**配置组合建议**:
- **成本敏感**（推荐）: `REANALYZE=false, FORWARD=true` - 节省费用但仍提醒
- **准确性优先**: `REANALYZE=true, FORWARD=true` - 最新分析+定期提醒
- **静默模式**: `REANALYZE=false, FORWARD=false` - 仅记录不通知
- **仅记录**: `REANALYZE=true, FORWARD=false` - 更新分析但不推送

详细说明请参考: [TIME_WINDOW_BEHAVIOR_CONFIG.md](TIME_WINDOW_BEHAVIOR_CONFIG.md)

## API 接口

### Webhook 接收

**POST /webhook**
```bash
curl -X POST http://localhost:8000/webhook \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Source: cloud-monitor" \
  -d '{
    "Type": "AlarmNotification",
    "RuleName": "CPU使用率告警",
    "Level": "critical",
    "Resources": [{"InstanceId": "i-abc123"}]
  }'
```

**响应示例**
```json
{
  "success": true,
  "webhook_id": 1,
  "is_duplicate": false,
  "duplicate_of": null,
  "ai_analysis": {
    "importance": "high",
    "summary": "服务器CPU使用率过高，需要立即处理"
  },
  "forward_status": "success"
}
```

### 开源生态专用入口（新增）

支持以下来源路由（也支持继续使用 `/webhook` 自动识别）：

- `POST /webhook/prometheus`
- `POST /webhook/grafana`
- `POST /webhook/pagerduty`
- `POST /webhook/datadog`

详细字段映射和完整示例参考：
[docs/features/OPEN_ECOSYSTEM_INTEGRATION.md](docs/features/OPEN_ECOSYSTEM_INTEGRATION.md)

### 告警智能降噪 + 根因分析（新增）

- 在短时间窗口内自动关联相似告警，识别 `root_cause / derived / standalone`
- 支持对衍生告警自动抑制转发（可配置）
- 分析结果新增 `ai_analysis.noise_reduction` 字段，包含置信度与关联 ID

详细说明：
[docs/features/ALERT_NOISE_REDUCTION_ROOT_CAUSE.md](docs/features/ALERT_NOISE_REDUCTION_ROOT_CAUSE.md)

### 配置管理
保护配置
```
xxx.com {
    
    @block_config {
        path /api/config
    }

    respond @block_config 403

    @browser {
        header User-Agent *Mozilla*
    }

    basicauth @browser {
        admin $2a$14$87cnh0YeeXg6u.028MH7xOqt9YD284r527.Bt8Ii3le3rgo.4YwZ6
    }

    log {
        format json
        level INFO
        output file /tmp/dejavu.prod.common-infra.hony.love.log {
            roll_size 100mb
            roll_keep 10
            roll_keep_for 7d
        }
    }

    reverse_proxy * http://localhost:8000 {
        transport http {
            dial_timeout 300s
            response_header_timeout 3000s
            read_timeout 3000s
            write_timeout 3000s
        }
    }
}

```

**获取配置**
```bash
GET /api/config
```

**更新配置**
```bash
POST /api/config
Content-Type: application/json

{
  "duplicate_alert_time_window": 12,
  "forward_duplicate_alerts": true,
  "reanalyze_after_time_window": false,
  "forward_after_time_window": true
}
```

### 其他接口

- `GET /` - Web 管理界面
- `GET /api/webhooks` - 获取 Webhook 历史列表
- `GET /health` - 健康检查
- `POST /api/reanalyze/:id` - 重新分析指定事件
- `POST /api/forward/:id` - 手动转发指定事件

## 重复告警去重机制

### 工作原理

1. **唯一标识生成**
   - 提取关键字段：来源、告警类型、规则名、资源ID、指标名、级别
   - 生成 SHA256 哈希值作为唯一标识

2. **重复检测**
   - 在配置的时间窗口内查询相同哈希值
   - 找到则标记为重复，复用原始分析结果

3. **处理策略**
   - **新告警**: 执行 AI 分析 → 保存 → 根据风险等级转发
   - **重复告警**: 跳过 AI 分析 → 保存 → 根据配置决定是否转发

### 示例场景

**场景 1: 窗口内不转发，窗口外复用分析但推送（推荐）**
```bash
DUPLICATE_ALERT_TIME_WINDOW=24
FORWARD_DUPLICATE_ALERTS=false         # 窗口内不转发
REANALYZE_AFTER_TIME_WINDOW=false     # 窗口外不重新分析（节省费用）
FORWARD_AFTER_TIME_WINDOW=true        # 窗口外仍推送（定期提醒）
```
- 第 1 次告警（10:00）：✅ AI 分析 + 转发（高风险）
- 第 2 次告警（11:00，1h后）：✅ 复用分析 + ❌ 不转发（窗口内）
- 第 3 次告警（第2天 11:00，25h后）：✅ 复用分析 + ✅ 转发（窗口外）

**场景 2: 完全重新处理（准确性优先）**
```bash
REANALYZE_AFTER_TIME_WINDOW=true      # 窗口外重新分析
FORWARD_AFTER_TIME_WINDOW=true        # 窗口外推送
```
- 第 1 次告警：✅ AI 分析 + 转发
- 窗口内重复：✅ 复用分析 + ❌ 不转发
- 窗口外重复：✅ **重新分析** + ✅ 转发

**场景 3: 静默模式（避免重复通知）**
```bash
REANALYZE_AFTER_TIME_WINDOW=false     # 窗口外不重新分析
FORWARD_AFTER_TIME_WINDOW=false       # 窗口外不推送
```
- 第 1 次告警：✅ AI 分析 + 转发
- 窗口内重复：✅ 复用分析 + ❌ 不转发
- 窗口外重复：✅ 复用分析 + ❌ 不转发（仅记录）

## 数据库结构

### webhook_events 表

| 字段 | 类型 | 说明 |
|------|------|------|
| id | Integer | 主键 |
| source | String | 来源系统 |
| alert_hash | String | 告警唯一标识（用于去重） |
| is_duplicate | Integer | 是否为重复告警（0/1） |
| duplicate_of | Integer | 原始告警ID |
| duplicate_count | Integer | 重复次数 |
| ai_analysis | JSON | AI 分析结果 |
| importance | String | 重要性等级（high/medium/low） |
| forward_status | String | 转发状态 |
| timestamp | DateTime | 事件时间 |

## 使用示例

### 测试重复告警去重

```bash
# 运行测试脚本
python test_duplicate_alert.py

# 运行可配置功能测试
python test_configurable_dedup.py
```

### Docker 部署

```bash
# 构建镜像
docker-compose build

# 启动服务
docker-compose up -d

# 查看日志
docker-compose logs -f webhook-service
```

## 最佳实践

### 1. 时间窗口设置

- **短周期告警**（如每分钟检测）：设置为 1-2 小时
- **长周期告警**（如每小时检测）：设置为 12-24 小时
- **偶发告警**：设置为 24-72 小时

### 2. 转发策略

**推荐配置**：`FORWARD_DUPLICATE_ALERTS=false`
- ✅ 减少通知噪音
- ✅ 节省转发带宽
- ✅ 保持首次告警的及时性

**特殊场景**：`FORWARD_DUPLICATE_ALERTS=true`
- 需要持续提醒的关键告警
- 告警频率本身就是重要指标

### 3. 告警去重字段

确保告警数据包含以下字段以提高去重准确性：
- `Type` 或 `event` - 告警类型
- `RuleName` 或 `alert_name` - 规则名称
- `Resources` 或 `resource_id` - 资源标识
- `MetricName` - 指标名称
- `Level` - 告警级别

## 故障排查

### 问题：重复告警未被识别

**检查清单**：
1. 确认告警数据包含关键字段
2. 检查时间窗口配置是否合理
3. 查看日志中的 `alert_hash` 值

### 问题：重复告警仍在转发

**解决方案**：
1. 检查 `FORWARD_DUPLICATE_ALERTS` 配置
2. 确认配置已通过 API 更新
3. 重启服务以加载最新配置

### 问题：AI 分析失败

**解决方案**：
1. 检查 `OPENAI_API_KEY` 是否正确
2. 确认 API 配额是否充足
3. 服务会自动降级为规则分析

## 性能优化

- ✅ 数据库索引：`alert_hash`、`timestamp`、`importance`
- ✅ 查询优化：仅查询时间窗口内的数据
- ✅ 缓存策略：重复告警直接复用分析结果
- ✅ 连接池：数据库连接池管理

## 安全建议

- 🔒 使用强密钥配置 `WEBHOOK_SECRET`
- 🔒 启用签名验证确保 Webhook 来源可信
- 🔒 限制 API 访问（推荐使用反向代理）
- 🔒 定期轮换 API 密钥
- 🔒 生产环境禁用 DEBUG 模式

## 技术栈

- **Backend**: Python 3.12 + Flask
- **Database**: PostgreSQL
- **AI**: OpenAI API (Claude Sonnet 4)
- **Frontend**: HTML + JavaScript
- **Deployment**: Docker + Docker Compose

## 目录结构

```
webhooks/
├── app.py                      # Flask 应用主文件
├── models.py                   # 数据库模型
├── config.py                   # 配置管理
├── utils.py                    # 工具函数（含去重逻辑）
├── ai_analyzer.py              # AI 分析模块
├── logger.py                   # 日志配置
├── migrate_db.py               # 数据库迁移脚本
├── migrations_tool.py          # 迁移工具
├── init_migrations.py          # 自动迁移脚本
├── entrypoint.sh               # 容器启动脚本
│
├── templates/                  # HTML 模板
│   └── dashboard.html          # Web 管理界面
│
├── prompts/                    # AI 提示词模板
│   ├── webhook_analysis.txt
│   ├── webhook_analysis_simple.txt
│   └── webhook_analysis_detailed.txt
│
├── migrations/                 # 数据库迁移 SQL
│   └── sql/
│       └── add_unique_constraint.sql
│
├── tests/                      # 测试文件
│   ├── test_webhook.py
│   ├── test_duplicate_alert.py
│   ├── test_*.py
│   └── html/                   # 测试用 HTML
│       └── test_*.html
│
├── scripts/                    # 工具脚本
│   ├── check_importance.py
│   ├── debug_hash.py
│   └── apply_unique_constraint.py
│
├── docs/                       # 📚 文档目录
│   ├── README.md               # 文档索引
│   ├── setup/                  # 部署设置
│   │   └── AUTO_MIGRATION.md
│   ├── features/               # 功能说明
│   │   ├── DEDUPLICATION_FIX.md
│   │   ├── ALERT_NOISE_REDUCTION_ROOT_CAUSE.md
│   │   ├── DUPLICATE_TIME_WINDOW.md
│   │   ├── OPEN_ECOSYSTEM_INTEGRATION.md
│   │   ├── TIME_WINDOW_BEHAVIOR_CONFIG.md
│   │   └── PROMPT_CONFIG.md
│   ├── troubleshooting/        # 故障排查
│   │   └── TROUBLESHOOTING.md
│   ├── performance/            # 性能优化
│   │   └── PERFORMANCE_OPTIMIZATION.md
│   └── changelogs/             # 变更日志
│       └── history/            # 历史记录
│
├── requirements.txt            # Python 依赖
├── Dockerfile                  # Docker 构建文件
├── docker-compose.yml          # Docker Compose 配置
├── .env.example                # 环境变量示例
└── README.md                   # 项目文档（本文件）
```

> 📖 **查看详细文档**: [docs/README.md](docs/README.md) - 包含所有功能和配置的详细说明

## 更新日志

### v2.1.0 (2026-02-28)
- ✨ 新增：超时间窗口后行为独立配置
  - `REANALYZE_AFTER_TIME_WINDOW` - 控制是否重新调用 AI 分析
  - `FORWARD_AFTER_TIME_WINDOW` - 控制是否推送转发
- 🔧 优化：去重检测逻辑支持窗口外历史告警识别
- 📝 文档：新增 [TIME_WINDOW_BEHAVIOR_CONFIG.md](TIME_WINDOW_BEHAVIOR_CONFIG.md) 详细说明
- 💡 场景：支持"节省费用但仍提醒"等灵活配置组合

### v2.0.0 (2025-11-07)
- ✨ 新增：可配置的重复告警去重功能
- ✨ 新增：自定义时间窗口配置
- ✨ 新增：重复告警转发策略配置
- 🔧 优化：API 配置管理接口
- 📝 文档：完善配置说明和使用示例

### v1.0.0
- 🎉 首次发布
- ✨ Webhook 接收和 AI 分析
- ✨ 自动转发到飞书
- ✨ Web 管理界面

## 贡献指南

欢迎提交 Issue 和 Pull Request！

## 许可证

MIT License

## 联系方式

如有问题或建议，请提交 Issue。
