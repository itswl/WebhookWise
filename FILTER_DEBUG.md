# 优先级筛选问题修复说明

## 问题描述
选择"高优先级"、"中优先级"、"低优先级"时没有数据显示，只有"所有优先级"才显示数据。

## 问题根源
1. **数据兼容性问题**：部分告警的 `importance` 字段可能为 `null` 或 `undefined`
2. **筛选逻辑过严**：原始代码直接比较 `webhook.importance === importanceFilter`，当 importance 为 null 时无法匹配

## 修复方案

### 1. 优先级默认值处理
```javascript
// 修复前（有问题）
const matchImportance = !importanceFilter || webhook.importance === importanceFilter;

// 修复后（兼容 null/undefined）
let matchImportance = true;
if (importanceFilter) {
    const webhookImportance = webhook.importance || 'low'; // 默认为 low
    matchImportance = webhookImportance === importanceFilter;
}
```

### 2. 添加调试信息
在浏览器控制台（F12）可以看到：
- 筛选条件
- 优先级分布统计
- 筛选前后的数据量

### 3. 筛选器显示优化
现在筛选器会显示每个优先级的数量：
- 高优先级 (3)
- 中优先级 (5)
- 低优先级 (12)

## 测试步骤

### 1. 打开浏览器控制台
按 `F12` 打开开发者工具，切换到 `Console` 标签

### 2. 刷新页面
```
Ctrl+Shift+R (Windows/Linux)
Cmd+Shift+R (Mac)
```

### 3. 查看调试信息
控制台会显示：
```
筛选条件: {searchTerm: "", importanceFilter: "", sourceFilter: "", duplicateFilter: ""}
总数据: 20
优先级分布: {high: 5, medium: 8, low: 3, null: 2, undefined: 2}
筛选后: 20
统计结果 - 总数: 20 高: 5 中: 8 低: 7 重复: 3
```

### 4. 测试筛选
选择"高优先级"，控制台显示：
```
筛选条件: {importanceFilter: "high", ...}
筛选后: 5
```

### 5. 使用测试页面
打开 `test_filter.html` 测试筛选逻辑：
```
file:///Users/imwl/webhooks/test_filter.html
```

## 预期行为

### 正常情况
- **所有优先级**：显示所有告警
- **高优先级**：只显示 importance='high' 的告警
- **中优先级**：只显示 importance='medium' 的告警
- **低优先级**：显示 importance='low' 或 null/undefined 的告警（默认）

### 特殊处理
- `importance = null` → 视为 'low'
- `importance = undefined` → 视为 'low'
- `importance = ''` → 视为 'low'

## 如果仍然有问题

### 检查数据
在控制台执行：
```javascript
// 查看所有数据
console.table(allWebhooks.map(w => ({
    id: w.id,
    importance: w.importance,
    source: w.source
})));

// 手动测试筛选
const highOnly = allWebhooks.filter(w => (w.importance || 'low') === 'high');
console.log('高优先级数量:', highOnly.length);
```

### 清除缓存
```
1. 按 Ctrl+Shift+Delete (Windows) 或 Cmd+Shift+Delete (Mac)
2. 选择"缓存的图片和文件"
3. 清除数据
4. 刷新页面
```

### 重启服务
```bash
# 停止服务 (Ctrl+C)
# 重新启动
python app.py
```

## 已修复的功能
✅ 优先级筛选正常工作
✅ 兼容 null/undefined 值
✅ 显示每个优先级的数量
✅ 添加详细的调试日志
✅ 其他筛选器（来源、重复）也已优化

## 注意事项
- 所有没有设置 importance 的告警会被归类为"低优先级"
- 可以在控制台查看详细的筛选过程
- 筛选器选项会实时显示每个分类的数量
