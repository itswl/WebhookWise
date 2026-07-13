"""Immutable data contracts used by the forwarding domain."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ForwardRuleSnapshot:
    """A forwarding rule detached from its persistence model."""

    id: int | None
    name: str
    match_event_type: str
    match_importance: str
    match_source: str
    match_duplicate: str
    match_payload: str
    target_type: str
    target_url: str
    stop_on_match: bool
    target_name: str = ""
    match_project: str = ""
    match_region: str = ""
    match_environment: str = ""
