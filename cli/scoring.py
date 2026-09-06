"""Parse and validate LLM responses for complexity scoring."""

import json
import re
from typing import Dict, Any


class InvalidResponseError(Exception):
    """Raised when LLM response cannot be parsed."""


def parse_complexity_response(response_text: str) -> Dict[str, Any]:
    """
    Parse LLM response and extract complexity score and explanation.

    Expected format: {"complexity": <int 1..10>, "explanation": "<string>"}

    Args:
        response_text: Raw response text from LLM

    Returns:
        Dict with 'complexity' (int) and 'explanation' (str) keys

    Raises:
        InvalidResponseError: If response cannot be parsed or validated
    """
    # Try to extract JSON from response (may have extra text)
    response_text = response_text.strip()

    # Look for JSON object ("complexity" in single mode, "scope" in dimensions)
    json_match = re.search(r"\{[^{}]*\"(?:complexity|scope)\"[^{}]*\}", response_text, re.DOTALL)
    if json_match:
        response_text = json_match.group(0)

    try:
        data = json.loads(response_text)
    except json.JSONDecodeError as e:
        raise InvalidResponseError(f"Failed to parse JSON: {e}")

    # Validate structure
    if not isinstance(data, dict):
        raise InvalidResponseError("Response is not a JSON object")

    # Dimension-scoring responses (sun-security fork, wide scale) carry five
    # sub-scores instead of a single number — sum them into `complexity` so
    # every caller downstream (llm retry loop, labeling, CSV) is unaffected.
    from .constants import DIMENSION_KEYS, get_dimension_cap, get_max_score, use_dimension_scoring

    if use_dimension_scoring() and "complexity" not in data:
        missing = [k for k in DIMENSION_KEYS if k not in data]
        if missing:
            raise InvalidResponseError(f"Missing dimension keys in response: {missing}")
        cap = get_dimension_cap()
        dims = {}
        for k in DIMENSION_KEYS:
            try:
                dims[k] = max(1, min(cap, int(data[k])))
            except (ValueError, TypeError):
                raise InvalidResponseError(f"Invalid {k} value: {data[k]}")
        # Raw sums span [n_dims, cap*n_dims] (all-1s .. all-cap); rescale
        # linearly onto [1, max_score] so a genuinely trivial PR scores 1.
        max_score = get_max_score()
        raw = sum(dims.values())
        min_sum, max_sum = len(DIMENSION_KEYS), cap * len(DIMENSION_KEYS)
        total = round((raw - min_sum) * (max_score - 1) / (max_sum - min_sum)) + 1
        total = max(1, min(max_score, total))
        breakdown = ", ".join(f"{k} {v}" for k, v in dims.items())
        rationale = re.sub(r"\s+", " ", str(data.get("explanation", "")).strip())
        return {
            "complexity": total,
            "explanation": f"[{breakdown}] {rationale}".strip(),
            "dimensions": dims,
        }

    if "complexity" not in data:
        raise InvalidResponseError("Missing 'complexity' key in response")

    if "explanation" not in data:
        raise InvalidResponseError("Missing 'explanation' key in response")

    # Extract and validate complexity
    try:
        complexity = int(data["complexity"])
    except (ValueError, TypeError):
        raise InvalidResponseError(f"Invalid complexity value: {data['complexity']}")

    # Clamp to valid range (upper bound configurable via COMPLEXITY_MAX_SCORE —
    # sun-security fork; upstream hardcodes 10).
    from .constants import get_max_score

    complexity = max(1, min(get_max_score(), complexity))

    # Extract and sanitize explanation
    explanation = str(data.get("explanation", "")).strip()
    # Remove newlines (replace with spaces)
    explanation = re.sub(r"\s+", " ", explanation)

    return {
        "complexity": complexity,
        "explanation": explanation,
    }
