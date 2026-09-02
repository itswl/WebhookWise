"""Neutralization for untrusted text interpolated into chat-card markdown.

Feishu ``lark_md``/``markdown`` elements, WeCom markdown and DingTalk markdown
all render a few constructs that let a value masquerade as something else:
``<at id=all></at>`` pages the whole chat, ``<font color=...>`` recolours a
line, ``<a href=...>`` / ``[label](url)`` hide a destination behind a label,
and ``**`` / ``~~`` / backticks can close the card's own bold label and open a
fake one. Alert bodies and model output both reach these cards verbatim, so a
crafted payload — or a prompt-injected analysis — could otherwise page the
whole chat or dress a phishing link as a runbook link.

Only interpolated VALUES go through :func:`escape_lark_md`. The card copy
itself (section titles, the ``**`` around them, list dashes, the SLA card's own
``<at id="all">``) is markup and is never escaped.
"""

from __future__ import annotations

import re

# Same-width look-alikes rather than deletion: the text stays readable and the
# callers' character budgets (400/600/800) still mean what they say. Callers
# truncate first, then escape — only the zero-width space adds a character.
_ANGLE_BRACKETS = str.maketrans({"<": "＜", ">": "＞"})
_BOLD_RUN = re.compile(r"\*{2,}")
_STRIKE_RUN = re.compile(r"~{2,}")
_BACKTICK_RUN = re.compile(r"`+")
# U+200B between "]" and "(" — invisible, but the link syntax no longer parses.
_LINK_JOINT = "]​("


def escape_lark_md(text: str) -> str:
    """Return ``text`` with mention, link and emphasis markup made inert.

    - ``<`` / ``>`` become full-width, so ``<at>``, ``<font>``, ``<a>`` and the
      WeCom ``<@userid>`` mention never form a tag;
    - ``](`` gets a zero-width space, so ``[label](url)`` cannot form a link;
    - runs of ``**``, ``~~`` and backticks become full-width look-alikes, so the
      value can neither close the template's bold nor open strike/code.

    A single ``*`` or ``~`` is left alone: it is ordinary prose far more often
    than markup, and on its own it cannot break out of the surrounding label.
    """
    if not text:
        return ""
    escaped = text.translate(_ANGLE_BRACKETS).replace("](", _LINK_JOINT)
    escaped = _BOLD_RUN.sub(lambda match: "＊" * len(match.group()), escaped)
    escaped = _STRIKE_RUN.sub(lambda match: "～" * len(match.group()), escaped)
    return _BACKTICK_RUN.sub(lambda match: "ˋ" * len(match.group()), escaped)
