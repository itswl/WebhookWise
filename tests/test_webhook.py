#!/usr/bin/env python3
"""
测试 webhook 服务的脚本
"""
import requests
import json
import hmac
import hashlib
from config import Config


def generate_signature(payload, secret):
    """生成 webhook 签名"""
    return hmac.new(
        secret.encode('utf-8'),
        payload.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()


def test_webhook():
    """测试 webhook 接收"""
    
    # 测试数据
    test_data = {
        'event': 'user.created',
        'user_id': 12345,
        'username': 'test_user',
        'email': 'test@example.com',
        'timestamp': '2024-01-01T12:00:00Z'
    }
    
    # 转换为 JSON 字符串
    payload = json.dumps(test_data)
    
    # 生成签名
    signature = generate_signature(payload, Config.WEBHOOK_SECRET)
    
    # 发送请求
    url = f'http://localhost:{Config.PORT}/webhook'
    headers = {
        'Content-Type': 'application/json',
        'X-Webhook-Signature': signature,
        'X-Webhook-Source': 'test-client'
    }
    
    print(f"发送测试 webhook 到: {url}")
    print(f"数据: {test_data}")
    print(f"签名: {signature}")
    
    try:
        response = requests.post(url, data=payload, headers=headers)
        print(f"\n响应状态码: {response.status_code}")
        print(f"响应内容: {response.json()}")
    except Exception as e:
        print(f"请求失败: {str(e)}")


def test_webhook_without_signature():
    """测试不带签名的 webhook"""
    
    test_data = {
        'event': 'test.event',
        'message': 'This is a test without signature'
    }
    
    url = f'http://localhost:{Config.PORT}/webhook/github'
    headers = {
        'Content-Type': 'application/json',
        'X-Webhook-Source': 'github'
    }
    
    print(f"\n发送无签名测试 webhook 到: {url}")
    
    try:
        response = requests.post(url, json=test_data, headers=headers)
        print(f"响应状态码: {response.status_code}")
        print(f"响应内容: {response.json()}")
    except Exception as e:
        print(f"请求失败: {str(e)}")


def test_health():
    """测试健康检查接口"""
    url = f'http://localhost:{Config.PORT}/health'
    
    print(f"\n测试健康检查: {url}")
    
    try:
        response = requests.get(url)
        print(f"响应状态码: {response.status_code}")
        print(f"响应内容: {response.json()}")
    except Exception as e:
        print(f"请求失败: {str(e)}")


if __name__ == '__main__':
    print("=" * 50)
    print("Webhook 服务测试")
    print("=" * 50)
    
    # 测试健康检查
    test_health()
    
    # 测试带签名的 webhook
    test_webhook()
    
    # 测试不带签名的 webhook
    test_webhook_without_signature()
    
    print("\n" + "=" * 50)
    print("测试完成")
    print("=" * 50)
