# 前端问题排查指南

## 问题：点击告警列表无反应

### 步骤 1: 检查浏览器控制台

1. 打开浏览器访问 `http://localhost:5000`
2. 按 `F12` 打开开发者工具
3. 切换到 `Console` 标签
4. 点击告警卡片，查看是否有错误信息

### 步骤 2: 检查是否有 JavaScript 错误

在控制台中应该看到以下日志：
```
正在显示告警列表，数量: X
```

点击告警时应该看到：
```
尝试切换告警详情: 1 <div.alert-item>
告警详情已 展开
```

### 步骤 3: 检查元素是否正确渲染

1. 在开发者工具中切换到 `Elements (元素)` 标签
2. 查找 `<div class="alert-item" data-alert-id="...">`
3. 确认 `onclick` 属性存在
4. 确认内部有 `<div class="alert-details">` 元素

### 步骤 4: 手动测试

在浏览器控制台中执行：
```javascript
// 测试函数是否存在
console.log(typeof toggleAlertDetails);  // 应该显示 "function"

// 手动触发展开
toggleAlertDetails(1, null);

// 检查元素
document.querySelector('[data-alert-id="1"]');
```

### 步骤 5: 测试简化版本

打开测试页面：
```
http://localhost:5000/../test_alert_toggle.html
```

或直接在浏览器中打开文件：
```
file:///Users/imwl/webhooks/test_alert_toggle.html
```

### 常见问题

#### 1. 没有数据显示
- 检查是否有告警数据
- 查看 Network 标签，确认 `/api/webhooks` 请求成功

#### 2. 点击无反应且无错误
- 尝试硬刷新页面 `Ctrl+Shift+R` (Windows) 或 `Cmd+Shift+R` (Mac)
- 清除浏览器缓存

#### 3. JavaScript 错误
常见错误：
- `toggleAlertDetails is not defined` - 函数未正确加载
- `Cannot read property 'classList'` - 元素未找到
- `event is not defined` - 事件对象传递问题

### 临时解决方案

如果以上都不行，可以尝试在浏览器控制台中执行：

```javascript
// 为所有告警卡片添加点击事件
document.querySelectorAll('.alert-item').forEach((item, index) => {
    item.addEventListener('click', function(e) {
        // 忽略按钮点击
        if (e.target.tagName === 'BUTTON' || e.target.closest('button')) {
            return;
        }
        this.classList.toggle('expanded');
        console.log('手动添加的事件触发', index);
    });
});
```

### 联系支持

如果问题仍然存在，请提供：
1. 浏览器控制台的完整错误信息
2. 浏览器类型和版本
3. Network 标签中 `/api/webhooks` 请求的响应
