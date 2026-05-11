"""Shared utility functions."""

from urllib.parse import urlsplit

_FEISHU_HOST_SUFFIXES = (".feishu.cn", ".larksuite.com")
_FEISHU_HOSTS = ("feishu.cn", "larksuite.com")


def is_feishu_url(url: str) -> bool:
    """Return True if *url* points to a Feishu / Lark webhook endpoint."""
    try:
        host = (urlsplit(str(url)).hostname or "").lower().rstrip(".")
    except Exception:
        return False
    return host in _FEISHU_HOSTS or any(host.endswith(suffix) for suffix in _FEISHU_HOST_SUFFIXES)
