"""Static configuration metadata backed by ``core.config.defaults.get_settings``."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal, TypedDict, get_args, get_origin

from pydantic_settings import BaseSettings

from core.config.defaults import get_settings

ConfigValueType = Literal["str", "int", "float", "bool"]


class ConfigKeyMeta(TypedDict):
    type: ConfigValueType
    sub: str


_CONFIG_TYPE_BY_PY_TYPE: dict[type[object], ConfigValueType] = {
    bool: "bool",
    int: "int",
    float: "float",
    str: "str",
}


def _config_type_for_annotation(annotation: object) -> ConfigValueType | None:
    if annotation in _CONFIG_TYPE_BY_PY_TYPE:
        return _CONFIG_TYPE_BY_PY_TYPE[annotation]
    if get_origin(annotation) is not None:
        for arg in get_args(annotation):
            if arg in _CONFIG_TYPE_BY_PY_TYPE:
                return _CONFIG_TYPE_BY_PY_TYPE[arg]
    return None


@lru_cache
def get_config_keys() -> dict[str, ConfigKeyMeta]:
    settings = get_settings()
    keys: dict[str, ConfigKeyMeta] = {}
    for sub_name in type(settings).model_fields:
        sub_config = getattr(settings, sub_name)
        if not isinstance(sub_config, BaseSettings):
            continue
        for key, field in type(sub_config).model_fields.items():
            value_type = _config_type_for_annotation(field.annotation)
            if value_type is None:
                continue
            keys[key] = {"type": value_type, "sub": sub_name}
    return keys
