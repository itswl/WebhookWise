---
title: The version line restarts at 0.1.x, and 5.0.0 is renumbered away
status: implemented
date: 2026-08-31
scope: whole
---

## Decision

The project version moves 5.0.0 → 0.1.1. The v5.0.0 tag and GitHub Release are
replaced by v0.1.1; the 2026-08-19 recreation release keeps its CHANGELOG
content renumbered as 0.1.0 with a one-line provenance note; pre-recreation
entries keep their original 1.x–3.8.0 numbers. The sweep is the version
contract (`tests/runtime/test_release_version_contract.py`: pyproject,
core/version.py, Dockerfile ARG, compose ×5, k8s configmap/kustomization/
README/4 manifests, both .env examples) plus the regenerated `build/openapi`
and the issue-template placeholder. `.pre-commit-config.yaml`'s `rev: v5.0.0`
is a HOOK REPOSITORY revision, not this project's version — it must never be
swept.

## Why

The maintainer's call (2026-08-31). The 08-19 choice of 5.0.0 optimized for
CHANGELOG continuity across the recreation, but a public repository whose
checkable history starts at a single release overstates itself at 5.0.0: a
reader can verify one release, and the number claims five majors. 0.1.x
matches what is actually published — and matches the sibling hookstack's
v0.1.0 line, so the family reads consistently.

## Consequences

Anything that ever saw 5.0.0 sees a downgrade: Docker Hub and GHCR keep their
immutable 5.0.0 (and sha-pinned) images until someone with registry access
deletes them, and semver-max logic would still pick 5.0.0 over 0.1.1 there.
The CHANGELOG is no longer numerically descending across the recreation
boundary — chronological order holds, and the 0.1.1 preamble carries the
explanation. Deployments pinning `ghcr.io/itswl/webhookwise:5.0.0` keep
working against the frozen old image and silently stop receiving updates;
the k8s manifests in-repo now pin 0.1.1.
