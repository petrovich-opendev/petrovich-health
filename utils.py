"""Small shared utilities."""
from __future__ import annotations

import json
import re


def parse_json_response(raw: str) -> dict | None:
    """Extract a JSON object from an LLM response, handling markdown wrappers.

    Tries direct json.loads first. On failure, falls back to the first
    {...} substring (DOTALL). Returns None if no valid JSON is found.
    """
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return None
