"""Shared utility functions."""


def is_feishu_url(url: str) -> bool:
    """Return True if *url* points to a Feishu / Lark webhook endpoint."""
    return "feishu.cn" in url or "larksuite.com" in url
