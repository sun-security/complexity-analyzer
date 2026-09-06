"""Centralized constants for the CLI."""

# Timeouts
DEFAULT_TIMEOUT = 120.0

# API sleep/delay
DEFAULT_SLEEP_SECONDS = 0.7

# Token display
TOKEN_VISIBLE_CHARS = 4

# LLM defaults
DEFAULT_MODEL = "gpt-5.2"
DEFAULT_MAX_TOKENS = 50000
DEFAULT_HUNKS_PER_FILE = 2

# Rate limits
MAX_RATE_LIMIT_WAIT = 3600  # 1 hour
RATE_LIMIT_CACHE_SECONDS = 30

# CSV
CSV_BATCH_SIZE = 10  # Write every N rows

# Retry settings
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_DELAY = 1.0

# GitHub API
GITHUB_API_VERSION = "2022-11-28"
GITHUB_API_BASE_URL = "https://api.github.com"
GITHUB_PER_PAGE = 100  # Max items per page for GitHub API


# --- sun-security fork addition -------------------------------------------
# Upper bound of the complexity scale. Upstream hardcodes 1-10; we score on a
# wider scale (1-100 in CI) for finer-grained analytics. Configured via env so
# the flag doesn't have to thread through every CLI/analyzer signature.
DEFAULT_MAX_SCORE = 10


def get_max_score() -> int:
    """COMPLEXITY_MAX_SCORE env override for the scale's upper bound (2-1000)."""
    import os

    raw = os.environ.get("COMPLEXITY_MAX_SCORE", "")
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_SCORE
    return value if 2 <= value <= 1000 else DEFAULT_MAX_SCORE


# Dimension scoring (sun-security fork, round 3): when the scale is wider than
# the upstream 1-10, single-number scoring collapses onto landmarks (multiples
# of 5, band centers — observed: 403 PRs scored exactly 65). A wide scale
# therefore switches to FIVE per-dimension sub-scores (scope, logic,
# integration, testing, risk), each 1..max/5, summed by the caller — sums of
# small judgments spread naturally across the range.
DIMENSION_KEYS = ("scope", "logic", "integration", "testing", "risk")


def use_dimension_scoring() -> bool:
    """Wide scale (max score > default) implies dimension scoring."""
    return get_max_score() > DEFAULT_MAX_SCORE


def get_dimension_cap() -> int:
    """Per-dimension upper bound: max score split evenly across dimensions."""
    return max(2, get_max_score() // len(DIMENSION_KEYS))
