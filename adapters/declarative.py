"""Declarative YAML webhook adapters.

Operators can onboard a simple new webhook source *without writing Python* by
dropping a ``<name>.yaml`` spec into ``adapters/specs/``. Each spec is parsed
and validated once at process start (see ``ecosystem_adapters.initialize_adapters``)
and compiled into the same detector + normalizer pair a code adapter registers,
so a declarative source flows through ``normalize_webhook_event`` and produces
identical alert-identity / dedup keys.

Specs are STATIC process configuration: read from disk at startup, never from
the database or Redis, and never mutated at runtime. A malformed spec fails
loudly at load time (raising :class:`DeclarativeSpecError` naming the file and
field) rather than silently disabling the source.

See ``adapters/specs/README.md`` for the spec format.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import yaml

from adapters.normalized import AlertIdentity, with_alert_identity
from adapters.registry import AdapterRegistry
from adapters.simple_adapters import normalize_level
from contracts.webhook_payload import JsonObject, WebhookData
from core.logger import get_logger

logger = get_logger("adapters.declarative")

SPECS_DIR: Final = Path(__file__).resolve().parent / "specs"

# AlertIdentity fields an operator maps from payload paths. ``source`` is handled
# separately (a literal that defaults to the adapter name), so it is not here.
_IDENTITY_PATH_FIELDS: Final = ("name", "resource", "service", "fingerprint", "severity")
# Canonical WebhookData output fields a spec may populate. ``Type`` is a literal
# classification tag; the rest resolve from payload paths.
_OUTPUT_PATH_FIELDS: Final = ("RuleName", "Level", "summary")
_TOP_LEVEL_KEYS: Final = frozenset({"name", "priority", "aliases", "detect", "identity", "output"})
_LEAF_CONDITIONS: Final = frozenset({"key_exists", "key_equals", "key_prefix"})
_COMBINATORS: Final = frozenset({"all", "any"})

# Sentinel distinguishing "path segment absent" from a present ``None`` value.
_MISSING: Final = object()

_Condition = Callable[[JsonObject], bool]


class DeclarativeSpecError(ValueError):
    """Raised when a declarative adapter spec is malformed.

    The message always names the offending spec file and field so a bad spec is
    diagnosable from the failed-startup log alone.
    """


@dataclass(frozen=True, slots=True)
class CompiledSpec:
    """A validated spec compiled into registry-ready callables."""

    name: str
    priority: int
    aliases: frozenset[str]
    detector: Callable[[JsonObject], bool]
    normalizer: Callable[[JsonObject], WebhookData]


# ── Path resolution ─────────────────────────────────────────────────────────


def _resolve_path(payload: Any, path: str) -> Any:
    """Resolve a dotted/indexed path against a JSON-like payload.

    Segments split on ``.``; an all-digit segment indexes a list
    (``alerts.0.labels.alertname``), any other segment is a mapping key.
    Returns :data:`_MISSING` when any segment cannot be resolved, so callers can
    tell an absent path from a present ``null``. Lookups are case-sensitive.
    """
    current: Any = payload
    for segment in path.split("."):
        if isinstance(current, Mapping):
            if segment not in current:
                return _MISSING
            current = current[segment]
        elif isinstance(current, Sequence) and not isinstance(current, (str, bytes, bytearray)):
            if not segment.isdigit():
                return _MISSING
            index = int(segment)
            if index >= len(current):
                return _MISSING
            current = current[index]
        else:
            return _MISSING
    return current


def _first_non_empty(payload: JsonObject, paths: Sequence[str]) -> Any:
    """First candidate path whose resolved value is present and non-blank.

    Mirrors ``_pick_first`` in simple_adapters: a value counts when it is not
    ``None`` and ``str(value).strip()`` is truthy (so ``0`` counts, ``""`` and
    whitespace-only do not). Returns ``None`` when no candidate qualifies.
    """
    for path in paths:
        value = _resolve_path(payload, path)
        if value is _MISSING or value is None:
            continue
        if str(value).strip():
            return value
    return None


# ── Detect condition compilation ─────────────────────────────────────────────


def _require_path_string(value: Any, *, spec: str, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DeclarativeSpecError(f"{spec}: '{where}' must be a non-empty path string")
    return value


def _compile_condition(node: Any, *, spec: str, where: str) -> _Condition:
    if not isinstance(node, Mapping):
        raise DeclarativeSpecError(
            f"{spec}: '{where}' must be a mapping with exactly one condition key, got {type(node).__name__}"
        )
    if len(node) != 1:
        raise DeclarativeSpecError(f"{spec}: '{where}' must have exactly one condition key, got {sorted(node)}")
    key, value = next(iter(node.items()))
    if key in _COMBINATORS:
        return _compile_combinator(str(key), value, spec=spec, where=where)
    if key == "key_exists":
        return _compile_key_exists(value, spec=spec, where=where)
    if key == "key_equals":
        return _compile_key_equals(value, spec=spec, where=where)
    if key == "key_prefix":
        return _compile_key_prefix(value, spec=spec, where=where)
    allowed = ", ".join(sorted(_LEAF_CONDITIONS | _COMBINATORS))
    raise DeclarativeSpecError(f"{spec}: '{where}' has unknown condition '{key}' (expected one of: {allowed})")


def _compile_combinator(kind: str, value: Any, *, spec: str, where: str) -> _Condition:
    if not isinstance(value, list) or not value:
        raise DeclarativeSpecError(f"{spec}: '{where}.{kind}' must be a non-empty list of conditions")
    subs = tuple(
        _compile_condition(item, spec=spec, where=f"{where}.{kind}[{index}]") for index, item in enumerate(value)
    )
    if kind == "all":
        return lambda payload: all(cond(payload) for cond in subs)
    return lambda payload: any(cond(payload) for cond in subs)


def _compile_key_exists(value: Any, *, spec: str, where: str) -> _Condition:
    path = _require_path_string(value, spec=spec, where=f"{where}.key_exists")
    return lambda payload: _resolve_path(payload, path) is not _MISSING


def _compile_key_equals(value: Any, *, spec: str, where: str) -> _Condition:
    if not isinstance(value, Mapping) or "path" not in value or "value" not in value:
        raise DeclarativeSpecError(f"{spec}: '{where}.key_equals' must be a mapping with 'path' and 'value'")
    path = _require_path_string(value["path"], spec=spec, where=f"{where}.key_equals.path")
    expected = value["value"]
    if expected is None or isinstance(expected, (Mapping, list)):
        raise DeclarativeSpecError(f"{spec}: '{where}.key_equals.value' must be a scalar (string/number/bool)")

    def _equals(payload: JsonObject) -> bool:
        resolved = _resolve_path(payload, path)
        if resolved is _MISSING:
            return False
        # Compare permissively so YAML/JSON type mismatches (e.g. 200 vs "200")
        # still match, while exact equality keeps bool/number distinctions.
        return bool(resolved == expected) or str(resolved) == str(expected)

    return _equals


def _compile_key_prefix(value: Any, *, spec: str, where: str) -> _Condition:
    if not isinstance(value, Mapping) or "path" not in value or "prefix" not in value:
        raise DeclarativeSpecError(f"{spec}: '{where}.key_prefix' must be a mapping with 'path' and 'prefix'")
    path = _require_path_string(value["path"], spec=spec, where=f"{where}.key_prefix.path")
    prefix = value["prefix"]
    if not isinstance(prefix, str):
        raise DeclarativeSpecError(f"{spec}: '{where}.key_prefix.prefix' must be a string")

    def _prefix(payload: JsonObject) -> bool:
        resolved = _resolve_path(payload, path)
        if resolved is _MISSING or resolved is None:
            return False
        return str(resolved).startswith(prefix)

    return _prefix


# ── Identity / output path-spec parsing ──────────────────────────────────────


def _normalize_paths(value: Any, *, spec: str, field: str) -> tuple[str, ...]:
    """Coerce a path-spec (a single string or a list of strings) to a tuple."""
    candidates = value if isinstance(value, list) else [value]
    if not candidates:
        raise DeclarativeSpecError(f"{spec}: '{field}' must not be an empty list")
    paths: list[str] = []
    for item in candidates:
        if not isinstance(item, str) or not item.strip():
            raise DeclarativeSpecError(f"{spec}: '{field}' must be a path string or list of non-empty path strings")
        paths.append(item)
    return tuple(paths)


# ── Normalizer construction ──────────────────────────────────────────────────


def _build_normalizer(
    *,
    name: str,
    source: str,
    identity_paths: Mapping[str, tuple[str, ...]],
    output_type: str | None,
    output_paths: Mapping[str, tuple[str, ...]],
) -> Callable[[JsonObject], WebhookData]:
    """Build a normalizer closure mirroring the shape of the code adapters.

    Source-native fields are preserved (``dict(data)``) for downstream analysis,
    canonical WebhookData fields are populated, and identity is attached via
    ``with_alert_identity`` so dedup keying is identical to code adapters.
    """

    def _normalize(data: JsonObject) -> WebhookData:
        res: dict[str, Any] = dict(data)

        name_value = _first_non_empty(data, identity_paths["name"])
        resource_value = _first_non_empty(data, identity_paths["resource"]) if "resource" in identity_paths else None
        service_value = _first_non_empty(data, identity_paths["service"]) if "service" in identity_paths else None
        fingerprint_value = (
            _first_non_empty(data, identity_paths["fingerprint"]) if "fingerprint" in identity_paths else None
        )
        severity_value = (
            normalize_level(_first_non_empty(data, identity_paths["severity"]))
            if "severity" in identity_paths
            else None
        )

        res["Type"] = output_type if output_type is not None else name
        res["event"] = "alert"

        rule_value = _first_non_empty(data, output_paths["RuleName"]) if "RuleName" in output_paths else name_value
        if rule_value is not None:
            res["RuleName"] = str(rule_value)

        if "Level" in output_paths:
            res["Level"] = normalize_level(_first_non_empty(data, output_paths["Level"]))
        elif severity_value is not None:
            res["Level"] = severity_value

        if "summary" in output_paths:
            summary_value = _first_non_empty(data, output_paths["summary"])
            if summary_value is not None:
                res["summary"] = str(summary_value)

        if resource_value is not None:
            res["Resources"] = [{"InstanceId": str(resource_value)}]
        if service_value is not None:
            res["service"] = str(service_value)

        return with_alert_identity(
            res,
            AlertIdentity(
                source=source,
                name=str(name_value) if name_value is not None else None,
                resource=str(resource_value) if resource_value is not None else None,
                service=str(service_value) if service_value is not None else None,
                fingerprint=str(fingerprint_value) if fingerprint_value is not None else None,
                severity=severity_value,
            ),
        )

    return _normalize


# ── Spec compilation ─────────────────────────────────────────────────────────


def _compile_identity(raw: Any, *, spec: str, name: str) -> tuple[str, dict[str, tuple[str, ...]]]:
    if not isinstance(raw, Mapping):
        raise DeclarativeSpecError(f"{spec}: 'identity' is required and must be a mapping")
    allowed = {"source", *_IDENTITY_PATH_FIELDS}
    unknown = set(raw) - allowed
    if unknown:
        raise DeclarativeSpecError(
            f"{spec}: unknown 'identity' fields {sorted(unknown)} (allowed: {', '.join(sorted(allowed))})"
        )

    source = name
    if "source" in raw:
        source_val = raw["source"]
        if not isinstance(source_val, str) or not source_val.strip():
            raise DeclarativeSpecError(f"{spec}: 'identity.source' must be a non-empty string literal")
        source = source_val.strip()

    if "name" not in raw:
        raise DeclarativeSpecError(f"{spec}: 'identity.name' is required so alerts get a stable dedup key")

    identity_paths = {
        field: _normalize_paths(raw[field], spec=spec, field=f"identity.{field}")
        for field in _IDENTITY_PATH_FIELDS
        if field in raw
    }
    return source, identity_paths


def _compile_output(raw: Any, *, spec: str) -> tuple[str | None, dict[str, tuple[str, ...]]]:
    if raw is None:
        return None, {}
    if not isinstance(raw, Mapping):
        raise DeclarativeSpecError(f"{spec}: 'output' must be a mapping")
    allowed = {"Type", *_OUTPUT_PATH_FIELDS}
    unknown = set(raw) - allowed
    if unknown:
        raise DeclarativeSpecError(
            f"{spec}: unknown 'output' fields {sorted(unknown)} (allowed: {', '.join(sorted(allowed))})"
        )

    output_type: str | None = None
    if "Type" in raw:
        type_val = raw["Type"]
        if not isinstance(type_val, str) or not type_val.strip():
            raise DeclarativeSpecError(f"{spec}: 'output.Type' must be a non-empty string literal")
        output_type = type_val.strip()

    output_paths = {
        field: _normalize_paths(raw[field], spec=spec, field=f"output.{field}")
        for field in _OUTPUT_PATH_FIELDS
        if field in raw
    }
    return output_type, output_paths


def compile_spec(raw: Any, *, spec: str) -> CompiledSpec:
    """Validate a parsed spec mapping and compile it into a :class:`CompiledSpec`.

    ``spec`` is the source label (usually the file name) woven into every error
    message. Raises :class:`DeclarativeSpecError` on any malformed field.
    """
    if not isinstance(raw, Mapping):
        raise DeclarativeSpecError(f"{spec}: top-level document must be a mapping, got {type(raw).__name__}")
    unknown = set(raw) - _TOP_LEVEL_KEYS
    if unknown:
        raise DeclarativeSpecError(
            f"{spec}: unknown top-level keys {sorted(unknown)} (allowed: {', '.join(sorted(_TOP_LEVEL_KEYS))})"
        )

    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise DeclarativeSpecError(f"{spec}: 'name' is required and must be a non-empty string")
    name = name.strip()

    priority = raw.get("priority", 0)
    if isinstance(priority, bool) or not isinstance(priority, int):
        raise DeclarativeSpecError(f"{spec}: 'priority' must be an integer")

    aliases_raw = raw.get("aliases", [])
    if not isinstance(aliases_raw, list) or any(not isinstance(a, str) or not a.strip() for a in aliases_raw):
        raise DeclarativeSpecError(f"{spec}: 'aliases' must be a list of non-empty strings")
    aliases = frozenset(a.strip() for a in aliases_raw)

    if "detect" not in raw:
        raise DeclarativeSpecError(f"{spec}: 'detect' is required")
    detector = _compile_condition(raw["detect"], spec=spec, where="detect")

    source, identity_paths = _compile_identity(raw.get("identity"), spec=spec, name=name)
    output_type, output_paths = _compile_output(raw.get("output"), spec=spec)

    normalizer = _build_normalizer(
        name=name,
        source=source,
        identity_paths=identity_paths,
        output_type=output_type,
        output_paths=output_paths,
    )
    return CompiledSpec(name=name, priority=priority, aliases=aliases, detector=detector, normalizer=normalizer)


# ── Loading & registration ───────────────────────────────────────────────────


def _load_one(path: Path) -> CompiledSpec:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise DeclarativeSpecError(f"{path.name}: could not read/parse spec: {exc}") from exc
    if raw is None:
        raise DeclarativeSpecError(f"{path.name}: spec file is empty")
    return compile_spec(raw, spec=path.name)


def load_specs(specs_dir: Path = SPECS_DIR) -> list[CompiledSpec]:
    """Load and compile every ``*.yaml`` / ``*.yml`` spec in ``specs_dir``.

    A missing directory or one with no spec files is a clean no-op (empty list).
    Dotfiles are skipped. Specs load in file-name order for deterministic
    detect ordering among same-priority declarative adapters.
    """
    if not specs_dir.is_dir():
        return []
    paths = sorted(p for p in (*specs_dir.glob("*.yaml"), *specs_dir.glob("*.yml")) if not p.name.startswith("."))
    return [_load_one(path) for path in paths]


def register_declarative_adapters(target: AdapterRegistry | None = None, *, specs_dir: Path = SPECS_DIR) -> list[str]:
    """Load specs from ``specs_dir`` and register them into ``target``.

    Uses the shared global registry when ``target`` is ``None``. Idempotent and
    collision-safe: a spec whose ``name`` is already registered (a second init
    pass, or a name that clashes with a code adapter) is skipped with a warning,
    which is also why code adapters win a name clash — they register first.
    Returns the names actually registered.
    """
    if target is None:
        from adapters.registry import registry

        target = registry

    registered: list[str] = []
    for spec in load_specs(specs_dir):
        if target.find_adapter_by_source(spec.name) is not None:
            logger.warning("[Declarative] Adapter name '%s' already registered; skipping declarative spec", spec.name)
            continue
        target.register_detector(spec.name, priority=spec.priority)(spec.detector)
        target.register(spec.name, aliases=set(spec.aliases))(spec.normalizer)
        registered.append(spec.name)

    if registered:
        logger.info("[Declarative] Registered %d declarative adapter(s): %s", len(registered), ", ".join(registered))
    return registered
