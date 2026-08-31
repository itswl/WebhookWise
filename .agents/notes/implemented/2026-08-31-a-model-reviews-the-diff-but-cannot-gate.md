---
title: A model reviews the diff in shadow, and cannot gate anything
status: implemented
date: 2026-08-31
scope: whole
---

## Decision

`.github/workflows/ai-review.yml` + `scripts/ci/ai_review.sh`: every
non-draft PR gets one sticky advisory comment from a model reviewer scoped to
correctness, ingress security, contract drift, and accidental translation of
behavioral Chinese strings. It is not in ci.yml's test job, `continue-on-error`
is set, and the job skips (with a notice) when `AI_REVIEW_API_KEY` is absent —
so it cannot block a merge, a fork PR, or a keyless clone. Style and coverage
are explicitly out of its scope: ruff, mypy and the gate own those.

## Why

The deterministic gate catches what it was told to catch; the class it cannot
hold is "this change is mechanically clean and behaviorally wrong". Anthropic's
account of its own SDLC (claude.com/blog/how-anthropic-secures-its-ai-native-
software-development-lifecycle) runs narrow-scope model reviewers next to the
deterministic tools and reports the share of PRs receiving substantive review
comments rising 16%→54% — with new reviewers starting in shadow until sampled
trust is earned. Same ladder this repository already uses for its own risky
automations (mode ladder, shadow-run), applied to the review surface.

This does NOT reopen
[2026-08-31-multi-agent-formation-and-model-reviewer](../rejected/2026-08-31-multi-agent-formation-and-model-reviewer.md):
that note rejects a model in the REMEDIATION approval chain, where a wrong
"looks fine" executes a command. This reviewer sits in no approval chain — its
worst failure is a wrong comment a person reads.

## Consequences

Advisory means ignorable: the comment stream must be sampled (do its findings
hold up?) or it is noise with an API bill. Promotion to anything gating waits
on that sampled hit rate — there is deliberately no timeline. The workflow is
inert until someone with repo admin sets `AI_REVIEW_API_KEY`; the reviewer
reads the diff of a public repository, so the key is the only secret involved
and PR content is already world-readable. Diff is capped at 55KB — a mega-PR
gets a partial review, which the comment states.
