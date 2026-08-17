"""One-click postmortem draft: assemble an incident's full story as Markdown.

Everything a postmortem needs is already recorded — member alerts, the
decision-trace outcomes, workflow timestamps (ack / escalation / resolution),
and the AI incident summary. This module is pure assembly over those rows (no
LLM call): the export is a *draft* for a human to edit, with the system's
facts filled in so nobody reconstructs a timeline from chat scrollback.

The scaffolding (headings, field labels) is Chinese: the export is read and
edited by Chinese-language operations, like the recap card. Machine terms
(outcome/skip codes, statuses) stay verbatim.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.datetime_utils import utc_isoformat, utcnow
from models import (
    DecisionTrace,
    Incident,
    IncidentMember,
    KBDocument,
    RunbookExecution,
    WebhookEvent,
)

_TIMELINE_LIMIT = 40


def _fmt(ts: datetime | None) -> str:
    return utc_isoformat(ts) or "—"


def _duration(start: datetime | None, end: datetime | None) -> str:
    if start is None or end is None:
        return "进行中"
    minutes = int((end - start).total_seconds() // 60)
    hours, mins = divmod(max(0, minutes), 60)
    return f"{hours}h {mins}m" if hours else f"{mins}m"


def _event_line(event_row: Any, outcome_by_event: dict[int, str]) -> str:
    summary = ""
    analysis = event_row.ai_analysis if isinstance(event_row.ai_analysis, dict) else {}
    if analysis:
        summary = str(analysis.get("summary") or "").strip()
    delivered = outcome_by_event.get(int(event_row.id), "")
    delivery_note = f" · {delivered}" if delivered else ""
    duplicate_note = " · 重复" if bool(event_row.is_duplicate) else ""
    text = summary or f"{event_row.source} event"
    return (
        f"| {_fmt(event_row.timestamp)} | {event_row.source or '—'} | "
        f"{event_row.importance or '—'} | {text[:120]}{duplicate_note}{delivery_note} |"
    )


async def build_postmortem_markdown(session: AsyncSession, incident_id: int) -> str | None:
    """Render the incident as a Markdown postmortem draft; None if absent."""
    incident = await session.get(Incident, incident_id)
    if incident is None:
        return None

    member_rows = (
        await session.execute(
            select(
                WebhookEvent.id,
                WebhookEvent.timestamp,
                WebhookEvent.source,
                WebhookEvent.importance,
                WebhookEvent.is_duplicate,
                WebhookEvent.ai_analysis,
            )
            .join(IncidentMember, IncidentMember.event_id == WebhookEvent.id)
            .where(IncidentMember.incident_id == incident_id)
            .order_by(IncidentMember.event_timestamp.asc(), IncidentMember.id.asc())
            .limit(_TIMELINE_LIMIT)
        )
    ).all()

    event_ids = [int(row.id) for row in member_rows]
    outcome_by_event: dict[int, str] = {}
    if event_ids:
        trace_rows = (
            await session.execute(
                select(DecisionTrace.webhook_event_id, DecisionTrace.outcome, DecisionTrace.skip_code)
                .where(DecisionTrace.webhook_event_id.in_(event_ids))
                .order_by(DecisionTrace.id)
            )
        ).all()
        for event_id, outcome, skip_code in trace_rows:
            label = "forwarded" if outcome == "forwarded" else f"skipped ({skip_code})"
            outcome_by_event[int(event_id)] = label

    lines: list[str] = [
        f"# 复盘草稿：{incident.title}",
        "",
        f"- **事故:** #{incident.id}",
        f"- **状态:** {incident.status} / {incident.workflow_status}",
        f"- **来源:** {incident.source or '—'}",
        f"- **最高重要度:** {incident.top_importance or '—'}",
        f"- **开始:** {_fmt(incident.started_at)}",
        f"- **解决:** {_fmt(incident.resolved_at)}",
        f"- **持续时长:** {_duration(incident.started_at, incident.resolved_at)}",
        f"- **事故内告警数:** {incident.alert_count}",
        f"- **负责人:** {incident.assignee or '未指派'}",
    ]
    if incident.acknowledged_at is not None:
        lines.append(f"- **认领时间:** {_fmt(incident.acknowledged_at)}")
    if incident.escalated_at is not None:
        lines.append(f"- **升级（SLA 超时）:** {_fmt(incident.escalated_at)}")

    summary = incident.summary_analysis if isinstance(incident.summary_analysis, dict) else {}
    confirmed = incident.resolution_record if isinstance(incident.resolution_record, dict) else {}
    if confirmed.get("owner"):
        lines.append(f"- **处置负责人:** {str(confirmed['owner']).strip()}")
    for heading, value in (
        ("概要", summary.get("summary")),
        ("根因分类", confirmed.get("root_cause_category")),
        ("根因", confirmed.get("root_cause") or summary.get("root_cause")),
        ("影响", confirmed.get("impact") or summary.get("impact")),
        ("处置过程", confirmed.get("resolution")),
        ("恢复证据", confirmed.get("recovery_evidence")),
    ):
        text_value = str(value or "").strip()
        if text_value:
            lines += ["", f"## {heading}", "", text_value]
    association = str(confirmed.get("change_association") or "").strip()
    if association:
        related_change_id = confirmed.get("related_change_id")
        change_text = association
        if isinstance(related_change_id, int):
            change_text = f"{association}（变更 #{related_change_id}）"
        lines += ["", "## 变更关联", "", change_text]

    lines += ["", "## 时间线", ""]
    if member_rows:
        lines += ["| 时间（UTC） | 来源 | 重要度 | 发生了什么 |", "| --- | --- | --- | --- |"]
        lines += [_event_line(row, outcome_by_event) for row in member_rows]
        if incident.alert_count > len(member_rows):
            lines.append("")
            lines.append(f"_仅显示前 {len(member_rows)} 条，共 {incident.alert_count} 条成员告警。_")
    else:
        lines.append("_没有记录到成员告警。_")

    # Workflow milestones appended to the timeline as bullet points.
    milestones: list[tuple[datetime | None, str]] = [
        (incident.escalated_at, "已发送 SLA 超时升级通知"),
        (incident.acknowledged_at, "已认领"),
        (incident.resolved_at, "已解决"),
    ]
    milestone_lines = [f"- {_fmt(ts)} — {label}" for ts, label in milestones if ts is not None]
    if milestone_lines:
        lines += ["", "### 关键节点", ""] + milestone_lines

    runbook_executions = list(
        (
            await session.execute(
                select(RunbookExecution)
                .where(RunbookExecution.incident_id == incident_id)
                .order_by(RunbookExecution.started_at, RunbookExecution.id)
                .limit(10)
            )
        )
        .scalars()
        .all()
    )
    if runbook_executions:
        lines += ["", "## Runbook 执行", ""]
        for execution in runbook_executions:
            lines += [
                f"### {execution.title}",
                "",
                f"- **状态:** {execution.status}",
                f"- **执行人:** {execution.actor}",
                f"- **开始:** {_fmt(execution.started_at)}",
                f"- **完成:** {_fmt(execution.completed_at)}",
                f"- **有效性:** {execution.effectiveness or '未评价'}",
            ]
            steps = execution.steps if isinstance(execution.steps, list) else []
            if steps:
                lines.append("")
                for step in steps:
                    if not isinstance(step, dict):
                        continue
                    marker = "x" if bool(step.get("completed")) else " "
                    text = str(step.get("text") or "未命名手工步骤").strip()
                    lines.append(f"- [{marker}] {text}")
            if execution.notes:
                lines += ["", f"> {execution.notes.strip()}"]
            lines.append("")

    confirmed_follow_ups = confirmed.get("follow_ups")
    has_confirmed_follow_ups = isinstance(confirmed_follow_ups, list)
    raw_recommendations = confirmed_follow_ups if has_confirmed_follow_ups else summary.get("recommendations")
    recommendation_items = raw_recommendations if isinstance(raw_recommendations, list) else []
    recommendations = [str(r).strip() for r in recommendation_items if str(r).strip()]
    lines += ["", "## 后续事项", ""]
    if recommendations:
        lines += [f"- [ ] {r}" for r in recommendations]
    elif has_confirmed_follow_ups:
        lines.append("_未记录后续事项。_")
    else:
        lines.append("- [ ] _待补充后续事项。_")

    kb_ref = (
        await session.execute(
            select(KBDocument.title, KBDocument.status)
            .where(KBDocument.source_ref == f"incident:{incident_id}", KBDocument.chunk_index == 0)
            .limit(1)
        )
    ).first()
    if kb_ref is not None:
        status_zh = {"draft": "草稿", "published": "已发布"}.get(str(kb_ref.status), str(kb_ref.status))
        lines += ["", f"_知识库：“{kb_ref.title}”（{status_zh}）。_"]

    lines += ["", f"_由 WebhookWise 生成于 {utc_isoformat(utcnow())}_"]
    return "\n".join(lines).rstrip() + "\n"
