import os
from pathlib import Path

from dotenv import dotenv_values
from fastapi import APIRouter, Body

from core.config import Config
from core.logger import logger
from core.routes import _fail, _ok

admin_router = APIRouter()


_CONFIG_SCHEMA = {
    'forward_url': ('FORWARD_URL', 'str', lambda x: x.startswith('http')),
    'enable_forward': ('ENABLE_FORWARD', 'bool', None),
    'enable_ai_analysis': ('ENABLE_AI_ANALYSIS', 'bool', None),
    'openai_api_key': ('OPENAI_API_KEY', 'str', None),
    'openai_api_url': ('OPENAI_API_URL', 'str', lambda x: x.startswith('http')),
    'openai_model': ('OPENAI_MODEL', 'str', lambda x: len(x) > 0),
    'ai_system_prompt': ('AI_SYSTEM_PROMPT', 'str', None),
    'log_level': ('LOG_LEVEL', 'str', lambda x: x.upper() in ['DEBUG', 'INFO', 'WARNING', 'ERROR']),
    'duplicate_alert_time_window': ('DUPLICATE_ALERT_TIME_WINDOW', 'int', lambda x: 1 <= x <= 168),
    'forward_duplicate_alerts': ('FORWARD_DUPLICATE_ALERTS', 'bool', None),
    'reanalyze_after_time_window': ('REANALYZE_AFTER_TIME_WINDOW', 'bool', None),
    'forward_after_time_window': ('FORWARD_AFTER_TIME_WINDOW', 'bool', None),
    'enable_alert_noise_reduction': ('ENABLE_ALERT_NOISE_REDUCTION', 'bool', None),
    'noise_reduction_window_minutes': ('NOISE_REDUCTION_WINDOW_MINUTES', 'int', lambda x: 1 <= x <= 60),
    'root_cause_min_confidence': ('ROOT_CAUSE_MIN_CONFIDENCE', 'float', lambda x: 0 <= x <= 1),
    'suppress_derived_alert_forward': ('SUPPRESS_DERIVED_ALERT_FORWARD', 'bool', None)
}


def _load_env_values(env_path: str = '.env') -> dict:
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


def _collect_config_updates(payload: dict) -> tuple[dict, list[str]]:
    updates = {}
    errors = []

    for key, raw_value in payload.items():
        if key not in _CONFIG_SCHEMA:
            continue

        env_var, value_type, validator = _CONFIG_SCHEMA[key]
        try:
            string_value, typed_value = _parse_update_value(key, raw_value, value_type, validator)
            if string_value is None:
                logger.debug(f"跳过空值配置: {key}")
                continue
            updates[env_var] = (string_value, typed_value)
        except ValueError as e:
            errors.append(str(e))

    return updates, errors


def _merge_env_lines(lines: list[str], updates: dict) -> list[str]:
    updated_vars = set()
    merged = []

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            merged.append(line)
            continue

        if '=' not in stripped:
            merged.append(line)
            continue

        var_name = stripped.split('=', 1)[0].strip()
        if var_name in updates:
            new_value, _ = updates[var_name]
            merged.append(f'{var_name}={new_value}\n')
            updated_vars.add(var_name)
        else:
            merged.append(line)

    for var_name, (string_value, _) in updates.items():
        if var_name not in updated_vars:
            merged.append(f'{var_name}={string_value}\n')

    return merged


def _persist_config_updates(updates: dict, env_file: str = '.env') -> None:
    env_path = Path(env_file)
    lines = env_path.read_text(encoding='utf-8').splitlines(keepends=True) if env_path.exists() else []
    merged_lines = _merge_env_lines(lines, updates)

    tmp_path = env_path.with_name(env_path.name + '.tmp')
    with open(tmp_path, 'w', encoding='utf-8') as f:
        f.writelines(merged_lines)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, env_path)

    for var_name, (_, typed_value) in updates.items():
        setattr(Config, var_name, typed_value)
        os.environ[var_name] = str(typed_value).lower() if isinstance(typed_value, bool) else str(typed_value)


def _build_prompt_source() -> str:
    if Config.AI_USER_PROMPT:
        return 'environment'
    if Config.AI_USER_PROMPT_FILE:
        return 'file'
    return 'default'


def _reload_prompt_template() -> str:
    from services.ai_analyzer import reload_user_prompt_template

    new_template = reload_user_prompt_template()
    logger.info("AI Prompt 模板已重新加载")
    return new_template


def _load_current_prompt_template() -> str:
    from services.ai_analyzer import load_user_prompt_template

    return load_user_prompt_template()


def _run_add_unique_constraint_migration() -> bool:
    from migrations.migrations_tool import add_unique_constraint

    logger.info("开始执行数据库迁移：添加唯一约束")
    return add_unique_constraint()


@admin_router.get('/api/config')
def get_config():
    try:
        env_values = _load_env_values('.env')
        return _ok(_build_config_response(env_values), 200)
    except Exception as e:
        logger.error(f"获取配置失败: {str(e)}")
        return _fail(str(e), 500)


@admin_router.post('/api/config')
def update_config(payload: dict = Body(default_factory=dict)):
    try:
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
            return _fail(str(e), 500)

        logger.info(f"配置已更新: {list(updates.keys())}")
        return _ok(status=200, message='配置更新成功')

    except Exception as e:
        logger.error(f"更新配置失败: {str(e)}", exc_info=True)
        return _fail(str(e), 500)


@admin_router.post('/api/prompt/reload')
def reload_prompt():
    try:
        new_template = _reload_prompt_template()
        return _ok(
            status=200,
            message='Prompt 模板已重新加载',
            template_length=len(new_template),
            preview=new_template[:200] + '...' if len(new_template) > 200 else new_template
        )
    except Exception as e:
        logger.error(f"重新加载 prompt 模板失败: {str(e)}", exc_info=True)
        return _fail(str(e), 500)


@admin_router.get('/api/prompt')
def get_prompt():
    try:
        template = _load_current_prompt_template()
        return _ok(
            status=200,
            template=template,
            source=_build_prompt_source()
        )
    except Exception as e:
        logger.error(f"获取 prompt 模板失败: {str(e)}", exc_info=True)
        return _fail(str(e), 500)


@admin_router.post('/api/migrations/add_unique_constraint')
def migration_add_unique_constraint():
    try:
        success = _run_add_unique_constraint_migration()
        if success:
            return _ok(status=200, message='数据库迁移成功：唯一约束已添加')
        return _fail('数据库迁移失败，请查看日志', 500)
    except Exception as e:
        logger.error(f"执行迁移失败: {e}")
        return _fail(str(e), 500)
