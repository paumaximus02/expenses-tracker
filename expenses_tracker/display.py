from __future__ import annotations

import re


def format_merchant(name: str) -> str:
    cleaned = re.sub(r"\s+Date\s+\d{2}/\d{2}/\d{4}.*$", "", name, flags=re.I)
    cleaned = re.sub(r"\s+To view transactions.*$", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip(" .")
