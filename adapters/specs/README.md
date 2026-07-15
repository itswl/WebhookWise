# Declarative webhook adapter specs

Onboard a **simple** new webhook source without writing Python: drop a
`<name>.yaml` file in this directory. Each spec is parsed and validated once at
process start and compiled into the same detector + normalizer a code adapter
registers, so a declarative source flows through the normal pipeline and
produces identical alert-identity / dedup keys.

Specs are **static process configuration** — read from disk at startup, never
from the database or Redis, never mutated at runtime. A change requires a
restart. A malformed spec fails loudly at startup (`DeclarativeSpecError`,
naming the file and field) rather than silently disabling the source.

For anything the mapping below can't express (multi-alert fan-out, computed
fields, conditional logic), write a code adapter in `adapters/simple_adapters.py`
instead.

## Format

```yaml
name: generic_json          # required — adapter id (also the default identity source)
priority: 0                 # optional — detect-order tie-break; see Precedence
aliases:                    # optional — extra source names that select this adapter
  - generic
  - simple_json

detect:                     # required — is this payload from this source?
  all:                      # combinator: "all" (AND) or "any" (OR)
    - key_exists: alert_name # a path must be present
    - key_equals:           # a path must equal a scalar value
        path: kind
        value: alert
    - key_prefix:           # a string path must start with a prefix
        path: source
        prefix: acme-

identity:                   # required — canonical identity used for dedup keying
  name: alert_name          # a single path...
  resource:                 # ...or an ordered list; first non-empty path wins
    - host
    - instance
  service: service
  fingerprint:
    - id
    - event_id
  severity: level           # run through the shared severity normalizer

output:                     # optional — populate canonical WebhookData fields
  Type: GenericAlert        # a literal classification tag
  RuleName: alert_name      # resolved from a payload path
  Level: level              # severity-normalized
  summary:
    - message
    - description
```

### Paths

Every `identity`/`output` value is a **path** (or a list of candidate paths,
first non-empty wins). Paths are dotted and support list indexing, so a nested
Prometheus-style field is reachable:

```
alerts.0.labels.alertname
```

Segments are case-sensitive; an all-digit segment indexes a list, anything else
is a mapping key. An absent path is skipped (treated as empty), not an error.

### `source` and `severity`

- `source` is not mapped: it defaults to the adapter `name`. (The identity's
  `source` is what dedup keys are scoped by.)
- `severity` (and `output.Level`) are passed through the same `normalize_level`
  used by the built-in adapters, so `crit`/`critical`/`P1`/`error` collapse to
  the canonical levels.

## Precedence

Built-in code adapters are checked first, so a code adapter **wins on a detect
tie**. Set `priority` above `0` only to intentionally pre-empt a code adapter;
higher priority is checked earlier. Keep `detect` specific enough not to collide
with existing sources (combine two conditions with `all`).

## Testing a new spec

After adding a spec, a quick local check that it loads and matches:

```bash
python -c "from adapters.ecosystem_adapters import initialize_adapters, normalize_webhook_event; \
initialize_adapters(); \
print(normalize_webhook_event({'alert_name':'x','level':'crit'}, 'unknown').source)"
```

`generic_json.yaml` in this directory is a working example (a flat
`{alert_name, level, host, service, id, message}` shape) and doubles as
reference documentation.
