"""
core/routes/config.py
=====================
配置管理相关路由（GET/POST /api/config）及辅助函数。
"""
from fastapi import APIRouter, Request, Body

from core.config import Config
from core.logger import logger
from core.routes import _ok, _fail
from dotenv import dotenv_values

config_bp = Blueprint('config', __name__)


# ── 辅助函数 ─────────────────────────────────────────────────────────────────

def _load_env_values(env_path: str = '.env') -> dict:
    from pathlib import Path
    path = Path(env_path)
    if not path.exists():
        return {}
    return dict(dotenv_values(path))


def _coerce_config_value(value, value_type: str, default=None):
    if value_type == 'bool':
        if isinstance(value, str):
            return value.lower() == 'true'
        return bool(value)
    if value_type == 'int':
        return int(value) if value not in (None, '') else default
    if value_type == 'float':
        return float(value) if value not in (None, '') else default
    return value


def _resolve_config_value(env_values: dict, key: str, default=None, value_type: str = 'str'):
    value = env_values.get(key)
    if value is None:
        value = getattr(Config, key, default)
    return _coerce_config_value(value, value_type, default)


def _build_config_response(env_values: dict) -> dict:
    api_key = _resolve_config_value(env_values, 'OPENAI_API_KEY', '')
    masked_key = '已配置' if api_key else '未配置'

    return {
        'forward_url': _resolve_config_value(env_values, 'FORWARD_URL', ''),
        'enable_forward': _resolve_config_value(env_values, 'ENABLE_FORWARD', False, 'bool'),
        'enable_ai_analysis': _resolve_config_value(env_values, 'ENABLE_AI_ANALYSIS', True, 'bool'),
        'openai_api_key': masked_key,
        'openai_api_url': _resolve_config_value(env_values, 'OPENAI_API_URL', Config.OPENAI_API_URL),
        'openai_model': _resolve_config_value(env_values, 'OPENAI_MODEL', Config.OPENAI_MODEL),
        'ai_system_prompt': _resolve_config_value(env_values, 'AI_SYSTEM_PROMPT', Config.AI_SYSTEM_PROMPT),
        'log_level': _resolve_config_value(env_values, 'LOG_LEVEL', 'INFO'),
        'duplicate_alert_time_window': _resolve_config_value(env_values, 'DUPLICATE_ALERT_TIME_WINDOW', Config.DUPLICATE_ALERT_TIME_WINDOW, 'int'),
        'forward_duplicate_alerts': _resolve_config_value(env_values, 'FORWARD_DUPLICATE_ALERTS', False, 'bool'),
        'reanalyze_after_time_window': _resolve_config_value(env_values, 'REANALYZE_AFTER_TIME_WINDOW', True, 'bool'),
        'forward_after_time_window': _resolve_config_value(env_values, 'FORWARD_AFTER_TIME_WINDOW', True, 'bool'),
        'enable_alert_noise_reduction': _resolve_config_value(env_values, 'ENABLE_ALERT_NOISE_REDUCTION', True, 'bool'),
        'noise_reduction_window_minutes': _resolve_config_value(env_values, 'NOISE_REDUCTION_WINDOW_MINUTES', 5, 'int'),
        'root_cause_min_confidence': _resolve_config_value(env_values, 'ROOT_CAUSE_MIN_CONFIDENCE', 0.65, 'float'),
        'suppress_derived_alert_forward': _resolve_config_value(env_values, 'SUPPRESS_DERIVED_ALERT_FORWARD', True, 'bool')
    }


def _parse_update_value(key: str, raw_value, value_type: str, validator):
    if value_type == 'bool':
        if isinstance(raw_value, bool):
            typed_value = raw_value
        elif isinstance(raw_value, str):
            typed_value = raw_value.lower() == 'true'
        else:
            raise ValueError(f"{key} 应为布尔类型")
        return str(typed_value).lower(), typed_value

    if value_type == 'int':
        typed_value = int(raw_value)
        if validator and not validator(typed_value):
            raise ValueError(f"{key} 值超出有效范围")
        return str(typed_value), typed_value

    if value_type == 'float':
        typed_value = float(raw_value)
        if validator and not validator(typed_value):
            raise ValueError(f"{key} 值超出有效范围")
        return str(typed_value), typed_value

    typed_value = str(raw_value).strip()
    if not typed_value:
        return None, None
    if validator and not validator(typed_value):
        raise ValueError(f"{key} 格式无效")
    return typed_value, typed_value


def _collect_config_updates(payload: dict) -> tuple[dict, list]:
    """解析请求体，返回 (updates_dict, errors_list)。"""
    updates = {}
    errors = []

    config_key_map = {
        'forward_url': ('FORWARD_URL', 'str', None),
        'enable_forward': ('ENABLE_FORWARD', 'bool', None),
        'enable_ai_analysis': ('ENABLE_AI_ANALYSIS', 'bool', None),
        'openai_api_url': ('OPENAI_API_URL', 'str', None),
        'openai_model': ('OPENAI_MODEL', 'str', None),
        'ai_system_prompt': ('AI_SYSTEM_PROMPT', 'str', None),
        'log_level': ('LOG_LEVEL', 'str', lambda v: v in {'DEBUG', 'INFO', 'WARNING', 'ERROR'}),
        'duplicate_alert_time_window': ('DUPLICATE_ALERT_TIME_WINDOW', 'int', lambda v: 1 <= v <= 168),
        'forward_duplicate_alerts': ('FORWARD_DUPLICATE_ALERTS', 'bool', None),
        'reanalyze_after_time_window': ('REANALYZE_AFTER_TIME_WINDOW', 'bool', None),
        'forward_after_time_window': ('FORWARD_AFTER_TIME_WINDOW', 'bool', None),
        'enable_alert_noise_reduction': ('ENABLE_ALERT_NOISE_REDUCTION', 'bool', None),
        'noise_reduction_window_minutes': ('NOISE_REDUCTION_WINDOW_MINUTES', 'int', lambda v: 1 <= v <= 60),
        'root_cause_min_confidence': ('ROOT_CAUSE_MIN_CONFIDENCE', 'float', lambda v: 0.0 <= v <= 1.0),
        'suppress_derived_alert_forward': ('SUPPRESS_DERIVED_ALERT_FORWARD', 'bool', None),
    }

    for client_key, (env_key, value_type, validator) in config_key_map.items():
        if client_key in payload:
            try:
                str_val, typed_val = _parse_update_value(client_key, payload[client_key], value_type, validator)
                if str_val is not None:
                    updates[env_key] = str_val
                    setattr(Config, env_key, typed_val)
            except (ValueError, TypeError) as e:
                errors.append(f"{client_key}: {str(e)}")

    return updates, errors


def _merge_env_lines(lines: list[str], updates: dict) -> list[str]:
    """将 updates 合并到 .env 行列表中。"""
    updated_keys = set(updates.keys())
    result = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            result.append(line)
            continue
        key = stripped.split('=', 1)[0].strip()
        if key in updated_keys:
            result.append(f"{key}={updates[key]}\n")
        else:
            result.append(line)
    for key, val in updates.items():
        if not any(line.strip().startswith(f"{key}=") for line in result):
            result.append(f"{key}={val}\n")
    return result


def _persist_config_updates(updates: dict, env_file: str = '.env'):
    """将配置更新写入 .env 文件，同时更新 Config 属性和 os.environ。"""
    from pathlib import Path
    env_path = Path(env_file)
    lines = env_path.read_text(encoding='utf-8').splitlines(keepends=True) if env_path.exists() else []
    merged_lines = _merge_env_lines(lines, updates)

    with open(env_path, 'w', encoding='utf-8') as f:
        f.writelines(merged_lines)
        f.flush()

    for var_name, (_, typed_value) in updates.items():
        setattr(Config, var_name, typed_value)
        import os
        os.environ[var_name] = str(typed_value).lower() if isinstance(typed_value, bool) else str(typed_value)


# ── 路由 ─────────────────────────────────────────────────────────────────────

@config_router.route('/api/config', methods=['GET'])
def get_config():
    """获取当前配置（从 .env 文件实时读取）"""
    try:
        env_values = _load_env_values('.env')
        return _ok(_build_config_response(env_values), 200)
    except Exception as e:
        logger.error(f"获取配置失败: {str(e)}")
        return _fail(str(e), 500)


@config_router.route('/api/config', methods=['POST'])
def update_config():
    """更新配置"""
    try:
        payload = request.get_json(silent=True) or {}
        if not payload:
            return _fail('请求体为空', 400)

        updates, errors = _collect_config_updates(payload)
        if errors:
            return _fail('; '.join(errors), 400)

        try:
            _persist_config_updates(updates, '.env')
        except PermissionError as e:
            logger.error(f"权限错误，无法写入 .env 文件: {str(e)}")
            return _fail('权限错误: 无法写入配置文件。请检查 .env 文件权限或使用环境变量配置。', 500)
        except Exception as e:
            logger.error(f"更新 .env 文件失败: {str(e)}", exc_info=True)
            raise

        logger.info(f"配置已更新: {list(updates.keys())}")
        return _ok(status=200, message='配置更新成功')

    except Exception as e:
        logger.error(f"更新配置失败: {str(e)}", exc_info=True)
        return _fail(str(e), 500)
