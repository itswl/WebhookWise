"""企业微信 Bot 适配器（预留接口）"""

import logging

logger = logging.getLogger(__name__)


class WeComBot:
    """企业微信 Bot 消息构建和发送"""
    
    def send_text(self, webhook_url: str, content: str):
        """发送文本消息
        
        Args:
            webhook_url: 企业微信机器人 Webhook 地址
            content: 文本内容
        """
        logger.info("WeChat Work bot text message not yet implemented")
        pass
    
    def send_markdown(self, webhook_url: str, content: str):
        """发送 Markdown 消息
        
        Args:
            webhook_url: 企业微信机器人 Webhook 地址
            content: Markdown 内容
        """
        logger.info("WeChat Work bot markdown message not yet implemented")
        pass
    
    def send_card(self, webhook_url: str, card: dict):
        """发送卡片消息
        
        Args:
            webhook_url: 企业微信机器人 Webhook 地址
            card: 卡片内容
        """
        logger.info("WeChat Work bot card message not yet implemented")
        pass


# 全局单例
wecom_bot = WeComBot()
