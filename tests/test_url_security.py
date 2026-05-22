import ipaddress

import pytest


@pytest.mark.asyncio
async def test_validate_outbound_url_rejects_private_ip_literal() -> None:
    from core.url_security import UnsafeTargetUrlError, validate_outbound_url

    with pytest.raises(UnsafeTargetUrlError):
        await validate_outbound_url("http://10.0.0.1/hook")


@pytest.mark.asyncio
async def test_validate_outbound_url_rejects_private_resolved_host(monkeypatch: pytest.MonkeyPatch) -> None:
    from core import url_security
    from core.url_security import UnsafeTargetUrlError, validate_outbound_url

    url_security._DNS_CACHE.clear()

    def private_ips(host: str, port: int | None) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        return [ipaddress.ip_address("192.168.1.20")]

    monkeypatch.setattr(url_security, "_resolved_ips", private_ips)

    with pytest.raises(UnsafeTargetUrlError):
        await validate_outbound_url("https://example.com/hook")


@pytest.mark.asyncio
async def test_validate_outbound_url_accepts_public_resolved_host(monkeypatch: pytest.MonkeyPatch) -> None:
    from core import url_security
    from core.url_security import validate_outbound_url

    url_security._DNS_CACHE.clear()

    def public_ips(host: str, port: int | None) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        return [ipaddress.ip_address("93.184.216.34")]

    monkeypatch.setattr(url_security, "_resolved_ips", public_ips)

    assert await validate_outbound_url("https://example.com/hook") == "https://example.com/hook"


@pytest.mark.asyncio
async def test_validate_outbound_url_enforces_allowlist(monkeypatch: pytest.MonkeyPatch, temp_config) -> None:
    from core.url_security import UnsafeTargetUrlError, validate_outbound_url

    monkeypatch.setattr(temp_config.security, "FORWARD_TARGET_ALLOWLIST", ".allowed.example")

    with pytest.raises(UnsafeTargetUrlError):
        await validate_outbound_url("https://example.com/hook")


@pytest.mark.asyncio
async def test_validate_outbound_url_caches_dns_for_fixed_targets(monkeypatch: pytest.MonkeyPatch) -> None:
    from core import url_security
    from core.url_security import validate_outbound_url

    url_security._DNS_CACHE.clear()
    calls = 0

    def public_ips(host: str, port: int | None) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        nonlocal calls
        calls += 1
        return [ipaddress.ip_address("93.184.216.34")]

    monkeypatch.setattr(url_security, "_resolved_ips", public_ips)

    assert await validate_outbound_url("https://example.com/hook") == "https://example.com/hook"
    assert await validate_outbound_url("https://example.com/other") == "https://example.com/other"
    assert calls == 1
