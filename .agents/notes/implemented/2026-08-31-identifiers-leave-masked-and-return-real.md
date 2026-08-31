---
title: Estate identifiers leave for the model as tokens and come back real
status: implemented
date: 2026-08-31
scope: services
---

## Decision

`services/analysis/pseudonymizer.py` swaps configured identifier classes (IPv4,
hostname suffixes, exact estate terms) for stable `anon-*` tokens in AI-bound
prompts and swaps the model's answer back. Masking is string-level over the
fully assembled prompt — one interception point covers payload, identity, KB
context, correction prior and evidence pack. The sync path round-trips in
process; the gateway round-trip persists the map on the DeepAnalysis row and
the poller unmasks the report, then clears the map. Off by default.

## Why

Absorbed from OpenSRE's reversible-masking intake. Credential redaction is
one-way and stays that way, but estate names — internal hostnames, addresses,
the org's names for things — were leaving for external providers verbatim,
which is the class of leak the estate scrub exists to prevent. Blunt redaction
would also cost the model referential integrity; a stable token per identifier
keeps "the same host failed twice" reasoning intact while the provider learns
nothing.

## Consequences

With masking on, correction-prior lookups and KB retrieval see masked text
wherever a configured term appears, so recall on exactly those terms drops —
the operator who turns this on chooses that trade. String-level masking can
hit lookalikes (a version string shaped like an IP); the swap is reversible,
so the cost is cosmetic. The map on the DeepAnalysis row holds real names —
the same names the row's payload already holds — and is cleared on first use.
