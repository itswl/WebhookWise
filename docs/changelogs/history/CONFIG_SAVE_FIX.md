# 配置保存问题修复

## 问题1: loadConfig is not defined

### 错误信息
```
Uncaught ReferenceError: loadConfig is not defined
```

### 原因
在 `saveConfig()` 函数中调用了不存在的 `loadConfig()` 函数：

```javascript
if (result.success) {
    alert('✅ 配置保存成功！');
    closeConfigModal();
    loadConfig();  // ❌ 这个函数不存在
}
```

### 修复方案
删除 `loadConfig()` 调用。配置保存成功后不需要刷新，因为：
1. 配置已经写入 `.env` 文件
2. 服务运行时会使用新配置
3. 页面上的配置显示（如果有）可以在下次打开时从服务器重新获取

## 问题2: API key 被重置为空

### 问题描述
用户在配置界面保存配置时，即使没有填写 API key，也会把空值覆盖到 `.env` 文件中，导致原有的 API key 丢失。

### 原因分析

**前端问题**：
```javascript
// 旧代码：总是发送所有字段，包括空值
const data = {
    openai_api_key: document.getElementById('configOpenaiApiKey').value,  // 可能是空字符串
    // ...
};
```

**后端问题**：
```python
# 旧代码：不检查空字符串
typed_val = str(val)
updates[env_var] = (typed_val, typed_val)  # 即使是空字符串也会保存
```

**结果**：
```bash
# .env 文件被覆盖
OPENAI_API_KEY=  # ← 空值！
```

### 修复方案

#### 前端修复（dashboard.html）

**修改前**：
```javascript
const data = {
    forward_url: document.getElementById('configForwardUrl').value,
    openai_api_key: document.getElementById('configOpenaiApiKey').value,
    // ...
};
```

**修改后**：
```javascript
// 获取表单值
const forwardUrl = document.getElementById('configForwardUrl').value;
const apiKey = document.getElementById('configOpenaiApiKey').value;
// ...

// 构建数据对象（只包含非空值）
const data = {
    duplicate_alert_time_window: parseInt(...),
    enable_forward: ...,
    // ...
};

// 只有当用户输入了值时才添加
if (forwardUrl && forwardUrl.trim()) {
    data.forward_url = forwardUrl.trim();
}
if (apiKey && apiKey.trim()) {
    data.openai_api_key = apiKey.trim();
}
// ...
```

**效果**：
- 如果输入框为空，不会包含在请求中
- 服务器不会更新该字段
- 原有配置保持不变 ✅

#### 后端修复（app.py）

**修改前**：
```python
else:  # str
    typed_val = str(val)
    if validator and not validator(typed_val):
        raise ValueError(f"{key} 格式无效")
    updates[env_var] = (typed_val, typed_val)  # ← 空字符串也会保存
```

**修改后**：
```python
else:  # str
    typed_val = str(val).strip()
    # 跳过空字符串（避免覆盖已有配置）
    if not typed_val:
        logger.debug(f"跳过空值配置: {key}")
        continue  # ← 跳过空值
    if validator and not validator(typed_val):
        raise ValueError(f"{key} 格式无效")
    updates[env_var] = (typed_val, typed_val)
```

**效果**：
- 收到空字符串时跳过该字段
- 不会写入 `.env` 文件
- 原有配置保持不变 ✅

## 使用指南

### 修改配置的正确方式

#### 1. 只修改部分配置

```
打开配置界面
→ 只修改需要更改的字段（如修改转发地址）
→ 其他字段留空
→ 保存

结果：
- 只更新填写的字段 ✅
- 空字段保持原值 ✅
```

#### 2. 查看当前配置

```
打开配置界面
→ API key 显示为 "已配置"（出于安全考虑）
→ 其他字段显示实际值

如果要保留 API key：
→ 保持 API key 输入框为空
→ 只修改其他字段
→ 保存
```

#### 3. 修改 API key

```
打开配置界面
→ 在 API key 输入框输入新的 key
→ 保存

结果：
- API key 被更新为新值 ✅
```

## 安全考虑

### API key 显示逻辑

**后端返回**（core/app.py GET /api/config）：
```python
'openai_api_key': '已配置' if Config.OPENAI_API_KEY else ''
```

**前端处理**（dashboard.html）：
```javascript
document.getElementById('configOpenaiApiKey').value =
    c.openai_api_key === '已配置' ? '' : c.openai_api_key || '';
```

**效果**：
- 如果已配置：显示为空（占位符提示"已配置"）
- 如果未配置：显示为空（占位符提示输入）
- 不会明文显示 API key ✅

### 建议

1. **生产环境**：
   - ✅ 使用 docker-compose.yml 环境变量
   - ✅ 不要通过 Web 界面修改敏感配置
   - ❌ 避免在日志中记录 API key

2. **开发环境**：
   - ✅ 可以使用 Web 界面修改
   - ✅ 确保 .env 文件不提交到 Git
   - ✅ 使用 .env.example 作为模板

## 测试验证

### 测试1: 不覆盖空字段

```bash
# 1. 确认当前 API key
grep OPENAI_API_KEY .env

# 2. 打开 Web 界面，只修改转发地址，API key 留空
# 3. 保存

# 4. 验证 API key 没有被清空
grep OPENAI_API_KEY .env
# 应该看到原来的值
```

### 测试2: 更新 API key

```bash
# 1. 打开 Web 界面，输入新的 API key
# 2. 保存

# 3. 验证更新成功
grep OPENAI_API_KEY .env
# 应该看到新的值
```

### 测试3: loadConfig 错误已修复

```bash
# 1. 打开浏览器控制台
# 2. 打开配置界面，修改任意配置
# 3. 保存

# 4. 控制台不应该有 "loadConfig is not defined" 错误
```

## 回滚方案

如果 API key 被误删除：

### 方法1: 从备份恢复

```bash
# 如果有 .env 备份
cp .env.backup .env
```

### 方法2: 从 Git 历史恢复

```bash
# 查看 .env 的历史
git log -p .env

# 恢复特定版本
git checkout <commit-hash> -- .env
```

### 方法3: 直接编辑

```bash
# 手动添加回去
vim .env

# 添加：
OPENAI_API_KEY=your-key-here

# 重启服务
docker-compose restart webhook-service
```

### 方法4: 通过 Web 界面重新输入

```bash
# 1. 打开配置界面
# 2. 在 API key 输入框输入正确的 key
# 3. 保存
```

## 防止问题再次发生

### 1. .env 文件备份

```bash
# 定期备份
cp .env .env.backup.$(date +%Y%m%d)

# 或使用 cron
0 0 * * * cp /path/to/.env /path/to/backups/.env.$(date +\%Y\%m\%d)
```

### 2. 使用环境变量（推荐）

```yaml
# docker-compose.yml
services:
  webhook:
    environment:
      - OPENAI_API_KEY=${OPENAI_API_KEY}  # 从宿主机读取
```

```bash
# .bashrc 或 .zshrc
export OPENAI_API_KEY="sk-xxx"
```

### 3. 只读挂载 .env（生产环境）

```yaml
# docker-compose.yml
volumes:
  - ./.env:/app/.env:ro  # 只读
```

此时 Web 界面无法保存到文件，但更安全。

## 总结

**修复内容**：
- ✅ 删除不存在的 `loadConfig()` 调用
- ✅ 前端只发送非空字段
- ✅ 后端跳过空字符串
- ✅ 防止误覆盖配置

**效果**：
- ✅ 配置保存不再报错
- ✅ API key 不会被空值覆盖
- ✅ 可以只更新部分配置
- ✅ 更安全、更灵活

**建议**：
- 生产环境使用环境变量
- 定期备份 .env 文件
- 敏感信息不通过 Web 界面修改
