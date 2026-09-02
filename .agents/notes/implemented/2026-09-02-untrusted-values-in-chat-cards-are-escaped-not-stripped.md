---
title: Untrusted values in chat cards are escaped, not stripped
status: implemented
date: 2026-09-02
scope: services
---

## Decision

Every payload-derived or model-derived value that is interpolated into a
Feishu `lark_md` element, or into the DingTalk/WeCom markdown body, passes
through `services/notifications/markdown_safety.escape_lark_md` first. The
function neutralizes only the constructs that change what a card *does* —
`<`/`>` (mentions, `<font>`, `<a>`), the `](` that closes a link, and runs of
`**` / `~~` / backticks that could close the template's own formatting — by
replacing them with visually identical full-width characters or a zero-width
space. Single `*` and `~` in prose are left alone. The template's own labels
and markup are never escaped.

## Why

An audit on 2026-09-02 found zero escaping in `services/notifications`: an
alert body such as `[点击确认](https://evil.example)` or `<at id=all></at>`
rendered as a live link or an @all mention in the operator's card, and a
prompt-injected analysis could do the same through the model's output. The
prompt-safety neutralizer covers only what goes *into* the model, not what the
model or the payload puts *onto* the card.

Stripping the characters was rejected because it changes the alert text
(`a<b` is a real comparison, `x**2` a real expression) and a reader comparing
the card with the source would see a different alert. Escaping with
full-width look-alikes keeps the text legible and byte-for-byte recognisable
while the markdown parser no longer sees an instruction. Backslash escapes
were rejected because `lark_md` does not honour them consistently.

## Consequences

- Recovery cards, verdict lines and the 400/600/800-character truncations are
  byte-identical to before for text without markup; tests pin that.
- A card can no longer carry a clickable link taken from alert text. Links the
  product itself adds (the dashboard link on incident cards, KB entries) are
  template markup and still work.
- New card builders must call the same helper; the test module
  `tests/notifications/test_markdown_safety.py` is where a new element type
  gets its injection case.
