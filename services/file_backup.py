"""文件备份服务 — 从 crud/webhook.py 提取的文件读写逻辑。"""

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import orjson

from core.compression import decompress_payload
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
        "raw_payload": decompress_payload(raw_payload) if isinstance(raw_payload, bytes) else raw_payload,
        "parsed_data": data,
    }

    # 添加 AI 分析结果
    if ai_analysis:
        full_data["ai_analysis"] = ai_analysis

    # 保存数据
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(full_data, f, indent=2, ensure_ascii=False)

    return filepath


# 文件名中的时间戳正则：{source}_{YYYYMMDD}_{HHMMSS}_{microseconds}.json
_TS_RE = re.compile(r"(\d{8}_\d{6}_\d{6})\.json$")


def _extract_timestamp_from_filename(filepath: Path) -> str:
    """从文件名提取时间戳用于排序；提取失败时 fallback 到 mtime。"""
    m = _TS_RE.search(filepath.name)
    if m:
        return m.group(1)
    # fallback: 用 mtime 生成等价字符串以保持排序一致
    try:
        mtime = filepath.stat().st_mtime
        return datetime.fromtimestamp(mtime).strftime("%Y%m%d_%H%M%S_%f")
    except OSError:
        return "00000000_000000_000000"


def get_webhooks_from_files(limit: int = 50) -> list[dict]:
    """从文件获取 webhook 数据(备份方式)

    惰性加载：先按文件名时间戳排序截取，只解析前 limit 个文件，
    避免目录积压大量文件时一次性全部解析导致 OOM。
    """
    data_dir = Path(Config.server.DATA_DIR)
    if not data_dir.exists():
        return []

    # 1. 只读取文件名列表
    files = [p for p in data_dir.iterdir() if p.suffix == ".json"]

    # 2. 按文件名中的时间戳降序排序（最新在前）
    files.sort(key=_extract_timestamp_from_filename, reverse=True)

    # 3. 截取前 limit 个
    selected = files[:limit]

    # 4. 只对选中的文件执行 JSON 解析
    webhooks: list[dict] = []
    for filepath in selected:
        try:
            webhook_data = orjson.loads(filepath.read_bytes())
            webhook_data["filename"] = filepath.name
            webhooks.append(webhook_data)
        except Exception as e:  # noqa: PERF203
            logger.error(f"读取文件失败 {filepath.name}: {e!s}")

    return webhooks
