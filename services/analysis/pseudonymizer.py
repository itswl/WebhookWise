"""Reversible identifier masking for AI-bound text.

Absorbed from OpenSRE's intake step: redaction destroys signal one way, so the
identifiers a prompt must not leak are swapped for STABLE placeholders on the
way out (`anon-host-1`, `anon-ip-2`, `anon-term-3`) and swapped back in the
model's answer on the way in. The model reasons over a consistent, coherent
world — every mention of the same host is the same token — and the operator
reads a report about the real one.

This sits ABOVE `core.sensitive_data` redaction, which still runs first and is
one-way on purpose: a credential must never be recoverable. Pseudonymization is
for the identifiers that are sensitive as ESTATE knowledge — internal hostnames,
addresses, the org's own names for things — where the model needs referential
integrity and the provider needs to learn nothing.

Masking is string-level over the fully assembled prompt, deliberately: one
interception point covers payload, identity block, KB snippets, correction
prior and evidence pack alike, and keeps every mention consistent across all of
them. The map travels with the request (in-process for the sync path, on the
DeepAnalysis row for the gateway round-trip) and the answer is unmasked before
anything persists or notifies.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from core.logger import get_logger

logger = get_logger("analysis.pseudonymizer")

_KIND_TERM = "term"
_KIND_HOST = "host"
_KIND_IP = "ip"

_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_TOKEN_RE = re.compile(r"\banon-(?:term|host|ip)-\d+\b")

# Left alone: masking these buys nothing and costs the model context.
# Values to SKIP masking (nothing binds here; these are match literals).
_IP_PASSTHROUGH = frozenset({"0.0.0.0", "127.0.0.1", "255.255.255.255"})  # nosec B104


@dataclass(frozen=True, slots=True)
class PseudonymPolicy:
    enabled: bool
    mask_ips: bool
    host_suffixes: tuple[str, ...]
    terms: tuple[str, ...]

    @classmethod
    def from_config(cls) -> PseudonymPolicy:
        from core.app_context import get_config_manager
        from core.text import split_csv_lower
        from services.operations import runtime_settings as rt

        cfg = get_config_manager().ai
        enabled = bool(
            rt.override_or("AI_PSEUDONYMIZE_ENABLED", bool(getattr(cfg, "AI_PSEUDONYMIZE_ENABLED", False)))
        )
        mask_ips = bool(rt.override_or("AI_PSEUDONYMIZE_IPS", bool(getattr(cfg, "AI_PSEUDONYMIZE_IPS", True))))
        suffixes_raw = rt.override_or(
            "AI_PSEUDONYMIZE_HOST_SUFFIXES", str(getattr(cfg, "AI_PSEUDONYMIZE_HOST_SUFFIXES", "") or "")
        )
        terms_raw = rt.override_or("AI_PSEUDONYMIZE_TERMS", str(getattr(cfg, "AI_PSEUDONYMIZE_TERMS", "") or ""))
        suffixes = tuple(s.lstrip(".") for s in split_csv_lower(suffixes_raw) if s.strip("."))
        # Terms stay case-sensitive (they are exact estate names); longest first
        # so an overlapping shorter term cannot pre-empt a longer one.
        raw_terms = (part.strip() for part in str(terms_raw).split(","))
        terms = tuple(sorted({t for t in raw_terms if t}, key=len, reverse=True))
        return cls(enabled=enabled, mask_ips=mask_ips, host_suffixes=suffixes, terms=terms)

    @property
    def active(self) -> bool:
        return self.enabled and bool(self.mask_ips or self.host_suffixes or self.terms)


class PseudonymSession:
    """One request's real->token assignments, reversible either direction."""

    def __init__(self, policy: PseudonymPolicy) -> None:
        self._policy = policy
        self._token_by_real: dict[str, str] = {}
        self._real_by_token: dict[str, str] = {}
        self._counters: dict[str, int] = {}
        self._host_re = _build_host_pattern(policy.host_suffixes)

    @property
    def mapping(self) -> dict[str, str]:
        """token -> real value, the shape that persists for later unmasking."""
        return dict(self._real_by_token)

    def _token_for(self, kind: str, real: str) -> str:
        token = self._token_by_real.get(real)
        if token is None:
            self._counters[kind] = self._counters.get(kind, 0) + 1
            token = f"anon-{kind}-{self._counters[kind]}"
            self._token_by_real[real] = token
            self._real_by_token[token] = real
        return token

    def mask_text(self, text: str) -> str:
        if not text:
            return text
        masked = text
        # Terms first: operator-named identifiers may themselves contain a host
        # or an address, and the explicit name should win as one unit.
        for term in self._policy.terms:
            if term in masked:
                masked = masked.replace(term, self._token_for(_KIND_TERM, term))
        if self._host_re is not None:
            masked = self._host_re.sub(
                lambda match: self._token_for(_KIND_HOST, match.group(0)), masked
            )
        if self._policy.mask_ips:
            masked = _IPV4_RE.sub(
                lambda match: match.group(0)
                if match.group(0) in _IP_PASSTHROUGH
                else self._token_for(_KIND_IP, match.group(0)),
                masked,
            )
        return masked

    def unmask_text(self, text: str) -> str:
        return unmask_text_with_map(text, self._real_by_token)

    def unmask_obj(self, obj: object) -> object:
        return unmask_obj_with_map(obj, self._real_by_token)


def _build_host_pattern(suffixes: tuple[str, ...]) -> re.Pattern[str] | None:
    if not suffixes:
        return None
    alternatives = "|".join(re.escape(suffix) for suffix in suffixes)
    # A labelled host under the suffix, or the bare suffix domain itself.
    return re.compile(
        rf"\b(?:[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?\.)?(?:{alternatives})\b",
        re.IGNORECASE,
    )


def build_pseudonym_session(policy: PseudonymPolicy | None = None) -> PseudonymSession | None:
    """A session when the policy is on and has anything to mask; else None."""
    policy = policy or PseudonymPolicy.from_config()
    if not policy.active:
        return None
    return PseudonymSession(policy)


def unmask_text_with_map(text: str, mapping: dict[str, str] | None) -> str:
    if not text or not mapping:
        return text
    return _TOKEN_RE.sub(lambda match: mapping.get(match.group(0), match.group(0)), text)


def unmask_obj_with_map(obj: object, mapping: dict[str, str] | None) -> object:
    """Deep-unmask every string in a JSON-shaped structure."""
    if not mapping:
        return obj
    if isinstance(obj, str):
        return unmask_text_with_map(obj, mapping)
    if isinstance(obj, dict):
        return {key: unmask_obj_with_map(value, mapping) for key, value in obj.items()}
    if isinstance(obj, list):
        return [unmask_obj_with_map(item, mapping) for item in obj]
    return obj
