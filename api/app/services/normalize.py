"""Text normalization shared across matching code: the sync fingerprint
(`app.services.sync`) and auto-categorization rules (`app.services.rules`, #51).

A separate module rather than living in either caller, since `sync` and `rules`
import from each other (`sync._post_process` calls `rules.apply_rules`) and a
helper defined in one would make the other's import a cycle.
"""

import re


def normalize_text(text: str) -> str:
    """Collapse runs of whitespace and uppercase, so two values that differ only
    by spacing or case compare equal -- a bank description against itself across
    syncs, or a bank description against a rule's pattern.
    """
    return re.sub(r"\s+", " ", text).strip().upper()
