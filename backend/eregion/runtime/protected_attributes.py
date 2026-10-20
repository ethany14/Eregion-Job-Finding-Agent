"""Protected-attribute wording shared by interview question and requirement-target checks."""
from __future__ import annotations

import re

# Technical terms that contain a protected word but say nothing about a person. Anything else that
# uses the word, including "age group" or "your race", is still treated as a protected attribute.
_TECHNICAL_TERMS = re.compile(
    r"\b(?:race[\s-]+conditions?|data[\s-]+races?|race[\s-]+free|max[\s-]+age|cache[\s-]+age)\b", re.I)


def mentions_protected(pattern: re.Pattern, text: str) -> bool:
    return bool(pattern.search(_TECHNICAL_TERMS.sub(" ", text)))
