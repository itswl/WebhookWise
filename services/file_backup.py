"""文件备份服务 — 从 crud/webhook.py 提取的文件读写逻辑。"""

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from core.config import Config
from core.logger import logger

WebhookData = dict[str, Any]
HeadersDict = dict[str, str]
AnalysisResult = dict[str, Any]


def save_webhook_to_file(
    data: WebhookData,
    source: str = "unknown",
    raw_payload: bytes | None = None,
    headers: HeadersDict | None = None,
    client_ip: str | None = None,
    ai_analysis: AnalysisResult | None = None,
) -> str:
    """保存 webhook 数据到文件(备份方式)"""
    # 创建数据目录
    os.makedirs(Config.server.DATA_DIR, exist_ok=True)

    # 生成文件名(基于时间戳)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    filename = f"{source}_{timestamp}.json"
    filepath = str(Path(Config.server.DATA_DIR) / filename)

    # 准备保存的完整数据
    full_data = {
        "timestamp": datetime.now().isoformat(),
        "source": source,
        "client_ip": client_ip,
        "headers": dict(headers) if headers else {},
        "raw_payload": raw_payload.decode("utf-8") if raw_payload else None,
        "parsed_data": data,
    }

    # 添加 AI 分析结果
    if ai_analysis:
        full_data["ai_analysis"] = ai_analysis

    # 保存数据
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(full_data, f, indent=2, ensure_ascii=False)

    return filepath


def get_webhooks_from_files(limit: int = 50) -> list[dict]:
    """从文件获取 webhook 数据(备份方式)"""
    if not os.path.exists(Config.server.DATA_DIR):
        return []

    webhooks = []
    files = [f for f in os.listdir(Config.server.DATA_DIR) if f.endswith(".json")]

    # 读取所有文件
    for filename in files:
        filepath = str(Path(Config.server.DATA_DIR) / filename)
        try:
            with open(filepath, encoding="utf-8") as f:
                webhook_data = json.load(f)
                webhook_data["filename"] = filename
                webhooks.append(webhook_data)
        except Exception as e:
            logger.error(f"读取文件失败 {filename}: {e!s}")

    # 按 timestamp 字段倒序排序（最新的在前面）
    webhooks.sort(key=lambda x: x.get("timestamp", ""), reverse=True)

    # 返回限制数量的结果
    return webhooks[:limit]
