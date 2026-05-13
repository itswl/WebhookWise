from core.logger import mask_url


def test_mask_url_hides_credentials_query_and_token_path_tail():
    masked = mask_url("https://user:pass@open.feishu.cn/open-apis/bot/v2/hook/secret-token?sign=abc")

    assert masked == "https://***@open.feishu.cn/open-apis/..."
    assert "user" not in masked
    assert "pass" not in masked
    assert "secret-token" not in masked
    assert "sign" not in masked


def test_mask_url_keeps_host_and_port_for_diagnostics():
    assert mask_url("http://120.25.176.44:8085/api/messages") == "http://***@120.25.176.44:8085/api/..."


def test_mask_url_masks_invalid_or_empty_values():
    assert mask_url("") == "***"
    assert mask_url("not-a-url") == "***"
