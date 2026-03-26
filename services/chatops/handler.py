"""ChatOps 消息处理核心 - 接收 Bot 回调，路由到对应处理器"""

import logging
import json
import time
from typing import Dict, Optional, Any

logger = logging.getLogger(__name__)


class ChatOpsHandler:
    """ChatOps 消息处理器"""
    
    def __init__(self):
        self._event_cache: Dict[str, float] = {}  # 用于消息去重
    
    def handle_feishu_callback(self, data: dict) -> dict:
        """处理飞书 Bot 回调消息
        
        飞书回调类型：
        - url_verification: 验证回调地址
        - event_callback: 收到消息/事件
        """
        # 1. 处理 URL 验证（飞书在配置回调地址时会发送验证请求）
        if data.get('type') == 'url_verification':
            return {'challenge': data.get('challenge', '')}
        
        # 2. 处理事件回调
        event = data.get('event', {})
        header = data.get('header', {})
        
        # 消息去重（飞书可能重复推送）
        event_id = header.get('event_id', '')
        if event_id and event_id in self._event_cache:
            return {'code': 0, 'msg': 'duplicate event'}
        if event_id:
            self._event_cache[event_id] = time.time()
            # 清理过期缓存（>5分钟）
            self._cleanup_event_cache()
        
        # 3. 提取消息内容
        message = event.get('message', {})
        msg_type = message.get('message_type', '')
        
        # 只处理文本消息
        if msg_type != 'text':
            logger.info(f"跳过非文本消息: type={msg_type}")
            return {'code': 0, 'msg': 'ignored non-text message'}
        
        try:
            content = json.loads(message.get('content', '{}'))
            text = content.get('text', '').strip()
        except json.JSONDecodeError:
            text = ''
        
        if not text:
            return {'code': 0, 'msg': 'empty message'}
        
        # 移除 @机器人 的前缀（飞书消息可能包含 @xxx）
        text = self._remove_at_mention(text)
        
        # 4. 获取发送者信息
        sender = event.get('sender', {}).get('sender_id', {})
        chat_id = message.get('chat_id', '')
        
        logger.info(f"收到 ChatOps 消息: chat_id={chat_id}, text={text[:50]}...")
        
        # 5. 路由到 NLP 处理
        from .nlp_router import nlp_router
        response = nlp_router.process(text, {
            'sender': sender,
            'chat_id': chat_id,
            'message_id': message.get('message_id', '')
        })
        
        # 6. 发送回复（通过飞书 API）
        if response and chat_id:
            self._send_feishu_reply(chat_id, response)
        
        return {'code': 0, 'msg': 'ok'}
    
    def handle_wecom_callback(self, data: dict) -> dict:
        """处理企业微信 Bot 回调（预留）"""
        logger.info("WeChat Work callback received (not yet implemented)")
        return {'errcode': 0, 'errmsg': 'ok'}
    
    def _remove_at_mention(self, text: str) -> str:
        """移除 @ 机器人的部分"""
        import re
        # 移除 @xxx 格式的提及（飞书格式）
        text = re.sub(r'@\S+\s*', '', text)
        return text.strip()
    
    def _send_feishu_reply(self, chat_id: str, content: dict):
        """通过飞书 API 发送消息回复"""
        import requests
        from core.config import Config
        
        # 获取 tenant_access_token
        token = self._get_feishu_token()
        if not token:
            logger.error("Failed to get Feishu access token")
            return
        
        url = "https://open.feishu.cn/open-apis/im/v1/messages"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        
        # 构建消息体
        payload = {
            "receive_id": chat_id,
            "msg_type": "interactive",
            "content": json.dumps(content, ensure_ascii=False)
        }
        
        try:
            resp = requests.post(
                f"{url}?receive_id_type=chat_id", 
                headers=headers, 
                json=payload, 
                timeout=10
            )
            if resp.status_code == 200:
                result = resp.json()
                if result.get('code') == 0:
                    logger.info(f"Feishu reply sent to {chat_id}")
                else:
                    logger.error(f"Feishu reply failed: {result}")
            else:
                logger.error(f"Feishu reply failed: status={resp.status_code}, body={resp.text}")
        except Exception as e:
            logger.error(f"Failed to send Feishu reply: {e}")
    
    def _get_feishu_token(self) -> Optional[str]:
        """获取飞书 tenant_access_token"""
        import requests
        from core.config import Config
        
        app_id = getattr(Config, 'FEISHU_BOT_APP_ID', '')
        app_secret = getattr(Config, 'FEISHU_BOT_APP_SECRET', '')
        
        if not app_id or not app_secret:
            logger.warning("Feishu Bot credentials not configured")
            return None
        
        url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
        try:
            resp = requests.post(url, json={
                "app_id": app_id,
                "app_secret": app_secret
            }, timeout=10)
            data = resp.json()
            if data.get('code') == 0:
                return data.get('tenant_access_token')
            else:
                logger.error(f"Failed to get Feishu token: {data}")
                return None
        except Exception as e:
            logger.error(f"Failed to get Feishu token: {e}")
            return None
    
    def _cleanup_event_cache(self):
        """清理过期的事件缓存"""
        now = time.time()
        expired = [k for k, v in self._event_cache.items() if now - v > 300]
        for k in expired:
            del self._event_cache[k]


# 全局单例
chatops_handler = ChatOpsHandler()
