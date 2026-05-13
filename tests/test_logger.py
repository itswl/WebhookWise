import logging
import os

import core.logger as logger_module
from core.logger import mask_url
from core.logging_levels import apply_log_levels


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


def test_setup_logger_reinitializes_in_new_process():
    logger_module.stop_log_listener()
    service_logger = logging.getLogger("webhook_service")
    inherited_handlers = list(service_logger.handlers)
    logger_module._logger_pid = -1

    configured_logger = logger_module.setup_logger()

    assert configured_logger is service_logger
    assert logger_module._logger_pid == os.getpid()
    assert service_logger.handlers
    assert not any(handler in inherited_handlers for handler in service_logger.handlers)


def test_apply_log_levels_splits_project_and_third_party_loggers():
    root_logger = logging.getLogger()
    original_levels = {
        "": root_logger.level,
        "webhook_service": logging.getLogger("webhook_service").level,
        "config": logging.getLogger("config").level,
        "models": logging.getLogger("models").level,
        "taskiq": logging.getLogger("taskiq").level,
        "httpx": logging.getLogger("httpx").level,
    }

    try:
        apply_log_levels("INFO", "WARNING")

        assert logging.getLogger("webhook_service").getEffectiveLevel() == logging.INFO
        assert logging.getLogger("config").getEffectiveLevel() == logging.INFO
        assert logging.getLogger("models.webhook").getEffectiveLevel() == logging.INFO
        assert logging.getLogger("taskiq.receiver.receiver").getEffectiveLevel() == logging.WARNING
        assert logging.getLogger("httpx").getEffectiveLevel() == logging.WARNING
    finally:
        root_logger.setLevel(original_levels[""])
        for logger_name, level in original_levels.items():
            if logger_name:
                logging.getLogger(logger_name).setLevel(level)
