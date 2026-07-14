from scripts.ops.expire_taskiq_results import is_taskiq_result_key


def test_taskiq_result_key_match_is_narrow() -> None:
    assert is_taskiq_result_key("0123456789abcdef0123456789abcdef")
    assert not is_taskiq_result_key("taskiq:schedule:0123456789abcdef0123456789abcdef")
    assert not is_taskiq_result_key("webhook:queue")
    assert not is_taskiq_result_key("0123456789abcdef")
