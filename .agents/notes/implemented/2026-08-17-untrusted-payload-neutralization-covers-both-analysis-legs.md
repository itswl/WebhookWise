---
title: Untrusted-payload neutralization covers both analysis legs
status: implemented
date: 2026-08-17
scope: services
---

## Decision

The fence-defang mechanism born in `deep_analysis_trigger` moved to
`services/analysis/prompt_safety.neutralize_untrusted_text` and now also runs
on the primary per-alert analysis leg: `_build_user_prompt` neutralizes the
payload YAML, the identity YAML, and the source string before `.format()`.
The default user-prompt template gained a Chinese `## 安全边界` section, and
`AI_SYSTEM_PROMPT`'s default (previously a literal `"...专家..."` stub) now
states that alert data is data, not instructions. KB context is interpolated
as written — it is operator-published content, not attacker input.

## Why

The primary leg's verdict sets `importance`, which drives forwarding and
silencing — so an unneutralized payload was a crafted alert steering its own
routing. Deep analysis had the defense; the leg with the larger blast radius
did not. Sharing one function (with an identity-assert test) instead of
copying it keeps the two legs from drifting apart again.

The system prompt carries the boundary sentence because operators can swap
the user-prompt template: code-side neutralization survives a swap, and the
system prompt is the anchor that survives it too.

## Consequences

- Changing the default prompts changes the recorded prompt version; the
  analysis cache misses once per alert shape after deploy. Accepted.
- A payload that legitimately contains ``` renders with zero-width-space
  breaks inside the fence run. Information is preserved; cosmetics are not.
- KB content is trusted by publication review. If unreviewed text ever gets
  a path into KB cards, this decision must be revisited.
