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
python models.py
python migrate_db.py  # 添加重复告警去重字段
```

6. **启动服务**
```bash
python app.py
```

服务将在 `http://localhost:8000` 启动

## 配置说明

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

# 转发配置
ENABLE_FORWARD=true
FORWARD_URL=https://open.feishu.cn/open-apis/bot/v2/hook/YOUR_WEBHOOK_KEY

# 重复告警去重配置
DUPLICATE_ALERT_TIME_WINDOW=24  # 时间窗口（小时）
FORWARD_DUPLICATE_ALERTS=false  # 是否转发重复告警
```

### 重复告警去重配置详解

#### 时间窗口配置
- **参数**: `DUPLICATE_ALERT_TIME_WINDOW`
- **默认值**: 24（小时）
- **说明**: 在此时间窗口内，相同的告警会被识别为重复
- **示例**: 设置为 1 表示 1 小时内的重复告警会被去重

#### 转发策略配置
- **参数**: `FORWARD_DUPLICATE_ALERTS`
- **默认值**: false
- **选项**:
  - `false`: 重复告警不自动转发（推荐，减少噪音）
  - `true`: 重复告警的高风险事件仍然转发
- **说明**: 无论如何设置，重复告警都会跳过 AI 分析，复用原始分析结果

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
  "forward_duplicate_alerts": true
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

**场景 1: 关闭重复告警转发（推荐）**
```bash
FORWARD_DUPLICATE_ALERTS=false
```
- 第 1 次告警：AI 分析 + 转发（如果是高风险）
- 第 2 次告警：复用分析 + 不转发
- 第 3 次告警：复用分析 + 不转发

**场景 2: 开启重复告警转发**
```bash
FORWARD_DUPLICATE_ALERTS=true
```
- 第 1 次告警：AI 分析 + 转发（如果是高风险）
- 第 2 次告警：复用分析 + 转发（如果是高风险）
- 第 3 次告警：复用分析 + 转发（如果是高风险）

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
├── test_webhook.py             # 基础测试
├── test_duplicate_alert.py     # 去重功能测试
├── test_configurable_dedup.py  # 可配置功能测试
├── templates/
│   └── dashboard.html          # Web 管理界面
├── requirements.txt            # Python 依赖
├── Dockerfile                  # Docker 构建文件
├── docker-compose.yml          # Docker Compose 配置
├── .env.example                # 环境变量示例
└── README.md                   # 项目文档
```

## 更新日志

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
