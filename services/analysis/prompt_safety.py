"""Neutralization for attacker-controllable text interpolated into AI prompts.

Shared by BOTH analysis legs. Deep analysis had this from the start; the
primary per-alert analysis did not, even though its output sets ``importance``
— which drives forwarding and silencing decisions — so a crafted alert body
could plausibly steer its own routing. Whatever goes into a prompt as data
must pass through here first.
"""

from __future__ import annotations


def neutralize_untrusted_text(text: str) -> str:
    """Defang fence/delimiter sequences in attacker-controllable text.

    Alert payload values (and an optional user question) are untrusted: a value
    containing a ``` fence or a heading marker could otherwise break out of its
    fenced code block and inject text the model treats as instructions. We break
    backtick runs with a zero-width space so the surrounding fence cannot be
    closed early. Applied to the serialized JSON/YAML string, this does not
    change the structure the model reads for legitimate (backtick-free)
    payloads.
    """
    # Zero-width space breaks a literal ``` run without removing information.
    return text.replace("```", "`​`​`")
