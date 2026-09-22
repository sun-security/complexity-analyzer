"""Diff preprocessing: redaction, filtering, chunking, and stats."""

import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

from .constants import DEFAULT_HUNKS_PER_FILE, DEFAULT_MAX_TOKENS

# File filtering patterns
IGNORE_EXT_RE = re.compile(
    r"(?:\.(?:png|jpg|jpeg|gif|webp|ico|pdf|zip|gz|bz2|xz|mp4|mov|mp3|wav|ogg|wasm|min\.js|map|lock|md|mdx|mdc|mdoc|markdown|rst)|\.lock\.ya?ml|package-lock\.json|pnpm-lock\.yaml)$",
    re.IGNORECASE,
)
IGNORE_PATH_RE = re.compile(
    r"(?:^|/)(?:vendor|node_modules|dist|build|coverage)(?:/|$)",
    re.IGNORECASE,
)

# Redaction patterns - enhanced for better secret detection
SECRET_PATTERNS = [
    # Quoted secrets (original pattern)
    re.compile(r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*['\"][A-Za-z0-9_\-.]{8,}['\"]"),
    # Unquoted secrets (longer minimum to reduce false positives)
    re.compile(r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*[A-Za-z0-9_\-.]{16,}"),
    # Bearer tokens
    re.compile(r"Bearer\s+[A-Za-z0-9_\-.]{20,}", re.IGNORECASE),
    # AWS keys
    re.compile(r"AKIA[0-9A-Z]{16}"),
    # OpenAI keys
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    # GitHub tokens
    re.compile(r"ghp_[A-Za-z0-9]{36}"),
    # GitHub OAuth tokens
    re.compile(r"gho_[A-Za-z0-9]{36}"),
    # GitHub App tokens
    re.compile(r"ghu_[A-Za-z0-9]{36}"),
    # GitHub refresh tokens
    re.compile(r"ghr_[A-Za-z0-9]{36}"),
]
# Keep original pattern for backward compatibility
SECRET_RE = SECRET_PATTERNS[0]
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def redact(text: str) -> str:
    """Redact secrets and emails from diff text."""
    # Apply all secret patterns
    for pattern in SECRET_PATTERNS:
        text = pattern.sub("[REDACTED_SECRET]", text)
    text = EMAIL_RE.sub("[REDACTED_EMAIL]", text)
    return text


def parse_diff_sections(
    diff_text: str, hunks_per_file: int = DEFAULT_HUNKS_PER_FILE
) -> Dict[str, List[str]]:
    """
    Parse diff into sections per file, limiting hunks per file.

    Args:
        diff_text: Raw unified diff text
        hunks_per_file: Maximum hunks to include per file

    Returns:
        Dict mapping filename -> list of lines
    """
    files: Dict[str, List[str]] = {}
    current_file: Optional[str] = None
    current_lines: List[str] = []
    hunk_count = 0

    for line in diff_text.splitlines():
        if line.startswith("diff --git"):
            # Save previous file
            if current_file is not None and current_lines:
                files[current_file] = current_lines

            # Parse new file
            parts = line.strip().split()
            if len(parts) >= 4:
                b_path = parts[-1]
                filename = b_path[2:] if b_path.startswith("b/") else b_path
                current_file = filename
                current_lines = [line]
                hunk_count = 0
            else:
                current_file = None
                current_lines = []
                hunk_count = 0
        elif line.startswith("@@ "):
            if current_file is not None:
                if hunk_count < hunks_per_file:
                    current_lines.append(line)
                    hunk_count += 1
                # Skip hunks beyond limit
        else:
            if current_file is not None:
                if hunk_count > 0:
                    # We're inside a hunk
                    current_lines.append(line)
                else:
                    # Header lines before first hunk
                    if line.startswith(("index ", "--- ", "+++ ")):
                        current_lines.append(line)

    # Save last file
    if current_file is not None and current_lines:
        files[current_file] = current_lines

    return files


# Comment stripping (sun-security fork, PLT-3619): full-line comment and
# blank-line changes carry no implementation complexity, so they are removed
# from the scored diff. Detection is lexical (by extension), deliberately
# conservative: only lines that are ENTIRELY a comment are dropped — a code
# line with a trailing comment counts as code. Block-comment bodies are only
# recognized by convention (leading `*` / `<!-- ... -->` on one line); an
# unmarked interior line of a block comment passes through as code, which just
# means the LLM sees slightly more than necessary.
_HASH_COMMENT_EXTS = frozenset(
    "py pyi sh bash zsh rb yml yaml toml tf tfvars mk cmake nix r pl pm ps1".split()
)
_SLASH_COMMENT_EXTS = frozenset(
    "js jsx ts tsx mjs cjs go java kt kts c h cc cpp cxx hpp cs php rs swift scala m mm groovy dart proto".split()
)
_DASH_COMMENT_EXTS = frozenset("sql lua hs".split())
_BLOCK_COMMENT_EXTS = _SLASH_COMMENT_EXTS | frozenset("css scss less".split())
_MARKUP_COMMENT_EXTS = frozenset("html htm xml vue svelte".split())


def is_comment_line(content: str, ext: str) -> bool:
    """True if a changed line's content (marker stripped) is comment-only."""
    content = content.strip()
    if ext in _HASH_COMMENT_EXTS:
        return content.startswith("#")
    if ext in _DASH_COMMENT_EXTS:
        return content.startswith("--")
    if ext in _MARKUP_COMMENT_EXTS:
        return content.startswith("<!--") and content.endswith("-->")
    if ext in _BLOCK_COMMENT_EXTS:
        if ext in _SLASH_COMMENT_EXTS and content.startswith("//"):
            return True
        if content.startswith("/*") and content.endswith("*/"):
            return True
        # Doc-comment body/closer convention (` * text`, `*/`). Requires the
        # space after `*` so dereferences like `*ptr = x` stay code.
        return content in ("*", "*/") or content.startswith("* ")
    return False


def strip_comment_changes(lines: List[str], ext: str) -> Tuple[List[str], int, int]:
    """
    Drop comment-only and blank +/- lines from one file's diff lines, then drop
    hunks left with no changes at all.

    Args:
        lines: One file's diff lines as produced by parse_diff_sections
        ext: File extension (lowercase, no dot)

    Returns:
        Tuple of (filtered_lines, additions_kept, deletions_kept)
    """
    header: List[str] = []
    hunks: List[List[str]] = []
    current: Optional[List[str]] = None
    for line in lines:
        if line.startswith("@@ "):
            current = [line]
            hunks.append(current)
        elif current is None:
            header.append(line)
        else:
            current.append(line)

    additions = deletions = 0
    out = header[:]
    for hunk in hunks:
        kept = [hunk[0]]
        changed = False
        for line in hunk[1:]:
            if line.startswith(("+", "-")):
                content = line[1:].strip()
                if not content or is_comment_line(content, ext):
                    continue
                changed = True
                if line[0] == "+":
                    additions += 1
                else:
                    deletions += 1
            kept.append(line)
        if changed:
            out.extend(kept)
    return out, additions, deletions


# Test exclusion (sun-security fork, PLT-3782): test files are stripped from
# the scored diff and the stats, the same way markdown and lockfiles are.
#
# The `testing` dimension asks for the effort a change DEMANDS to validate,
# explicitly "not whether tests are present in the diff". Showing the model
# the delivered test code contaminates exactly that judgement, and the lines
# inflate `scope` on top: a one-line fix arriving with a hundred table cases
# read as a large, hard change. Stripped, `testing` judges the production
# change on its own terms, which is what the rubric asks for.
#
# Detection is by path convention and deliberately conservative: a helper that
# merely supports tests but does not live under a test path counts as code,
# which just means the model sees slightly more than necessary.
TEST_PATH_RE = re.compile(
    r"(?:^|/)(?:tests?|__tests__|__mocks__|__snapshots__|testdata)/"
    r"|_test\.[a-z0-9]+$"
    r"|\.(?:test|spec)\.[a-z0-9]+$"
    r"|(?:^|/)conftest\.py$"
    r"|(?:^|/)test_[^/]+\.py$",
    re.IGNORECASE,
)


def is_test_path(path: str) -> bool:
    """True if a file is test code, judged by path convention alone."""
    return bool(TEST_PATH_RE.search(path))


def cap_hunks(lines: List[str], hunks_per_file: int) -> List[str]:
    """Keep the file header plus at most hunks_per_file hunks."""
    out: List[str] = []
    count = 0
    for line in lines:
        if line.startswith("@@ "):
            count += 1
            if count > hunks_per_file:
                break
        out.append(line)
    return out


def filter_file(path: str) -> bool:
    """Check if file should be included (not ignored)."""
    if IGNORE_PATH_RE.search(path):
        return False
    if IGNORE_EXT_RE.search(path):
        return False
    return True


def ext_from_filename(filename: str) -> str:
    """Extract file extension."""
    base = os.path.basename(filename)
    _, ext = os.path.splitext(base)
    return ext.lstrip(".").lower()


def build_stats(meta: Dict[str, Any], filenames: List[str]) -> Dict[str, Any]:
    """
    Build statistics from PR metadata and selected filenames.

    Args:
        meta: PR metadata dict from GitHub API
        filenames: List of selected filenames

    Returns:
        Stats dict with additions, deletions, changedFiles, byExt, byLang, fileCount
    """
    additions = meta.get("additions", 0)
    deletions = meta.get("deletions", 0)
    changed_files = meta.get("changed_files") or meta.get("changedFiles")
    if not changed_files:
        files_list = meta.get("files") or []
        changed_files = len(files_list)

    # Count by extension
    by_ext: Dict[str, int] = {}
    for fn in filenames:
        ext = ext_from_filename(fn)
        by_ext[ext] = by_ext.get(ext, 0) + 1

    # Map extensions to languages
    lang_map = {
        "ts": "TypeScript",
        "tsx": "TypeScript",
        "js": "JavaScript",
        "jsx": "JavaScript",
        "py": "Python",
        "rb": "Ruby",
        "go": "Go",
        "java": "Java",
        "kt": "Kotlin",
        "cs": "CSharp",
        "php": "PHP",
        "rs": "Rust",
        "swift": "Swift",
        "m": "Objective-C",
        "scala": "Scala",
        "sh": "Shell",
        "yml": "YAML",
        "yaml": "YAML",
        "json": "JSON",
        "sql": "SQL",
    }

    by_lang: Dict[str, int] = {}
    for ext, count in by_ext.items():
        lang = lang_map.get(ext or "", "Other")
        by_lang[lang] = by_lang.get(lang, 0) + count

    return {
        "additions": additions,
        "deletions": deletions,
        "changedFiles": changed_files,
        "byExt": by_ext,
        "byLang": by_lang,
        "fileCount": len(filenames),
    }


def truncate_to_token_limit(text: str, max_tokens: int) -> Tuple[str, int]:
    """
    Truncate text to at most max_tokens using tiktoken.

    Args:
        text: Text to truncate
        max_tokens: Maximum token count

    Returns:
        Tuple of (truncated_text, token_count)
    """
    if max_tokens <= 0:
        return "", 0

    try:
        import tiktoken

        enc = tiktoken.get_encoding("cl100k_base")
        tokens = enc.encode(text, disallowed_special=())
        if len(tokens) <= max_tokens:
            return text, len(tokens)
        # Decode only the first max_tokens
        truncated = enc.decode(tokens[:max_tokens])
        return truncated, max_tokens
    except ImportError:
        # Fallback: approximate by characters (rough estimate: 4 chars per token)
        char_limit = max_tokens * 4
        if len(text) <= char_limit:
            return text, len(text) // 4
        return text[:char_limit], max_tokens


def make_prompt_input(
    url: str, title: str, stats: Dict[str, Any], files: List[str], diff_excerpt: str
) -> str:
    """
    Format prompt input with PR context and diff.

    Args:
        url: PR URL
        title: PR title
        stats: Stats dict
        files: List of changed files
        diff_excerpt: Truncated diff text

    Returns:
        Formatted prompt input string
    """
    header = (
        f"PR: {url}\n"
        f"Title: {title}\n"
        f"Stats: additions={stats.get('additions')} deletions={stats.get('deletions')} "
        f"changedFiles={stats.get('changedFiles')} filesTop={len(files)}\n"
        f"Files: {', '.join(files[:10])}\n"
        f"--- DIFF START ---\n"
    )
    return header + (diff_excerpt or "") + "\n--- DIFF END ---"


def _select_files(
    sections: Dict[str, List[str]],
    hunks_per_file: int,
    exclude_tests: bool,
) -> Tuple[List[str], List[str], int, int]:
    """
    Pick the scoreable files out of parsed diff sections.

    Args:
        sections: Per-file diff lines from parse_diff_sections
        hunks_per_file: Maximum hunks to keep per file in the excerpt
        exclude_tests: Drop files on a test path (see TEST_PATH_RE)

    Returns:
        Tuple of (selected_files, excerpt_lines, additions, deletions)
    """
    selected_files: List[str] = []
    excerpt_lines: List[str] = []
    additions = deletions = 0
    for fn, lines in sections.items():
        if not filter_file(fn):
            continue
        if exclude_tests and is_test_path(fn):
            continue
        lines, adds, dels = strip_comment_changes(lines, ext_from_filename(fn))
        if adds + dels == 0:
            # Comment/whitespace-only change — nothing scoreable in this file.
            continue
        selected_files.append(fn)
        additions += adds
        deletions += dels
        excerpt_lines.extend(cap_hunks(lines, hunks_per_file))
    return selected_files, excerpt_lines, additions, deletions


def process_diff(
    diff_text: str,
    meta: Dict[str, Any],
    max_tokens: int = DEFAULT_MAX_TOKENS,
    hunks_per_file: int = DEFAULT_HUNKS_PER_FILE,
) -> Tuple[str, Dict[str, Any], List[str]]:
    """
    Process diff: redact, filter, chunk, truncate, and build stats.

    Args:
        diff_text: Raw diff text
        meta: PR metadata
        max_tokens: Maximum tokens for diff excerpt
        hunks_per_file: Maximum hunks per file

    Returns:
        Tuple of (formatted_diff_excerpt, stats_dict, selected_files_list)
    """
    # Redact secrets
    redacted_diff = redact(diff_text)

    # Parse WITHOUT the per-file hunk cap: comment-only hunks are stripped
    # first so they can't crowd real ones out of the capped excerpt, and stats
    # count the whole filtered change rather than just the excerpted hunks.
    sections = parse_diff_sections(redacted_diff, hunks_per_file=sys.maxsize)

    # Filter files, strip comment/blank-only changes, cap hunks
    selected_files, excerpt_lines, additions, deletions = _select_files(
        sections, hunks_per_file, exclude_tests=True
    )

    # A PR that is nothing but tests would otherwise arrive at the caller as
    # "no scoreable files" and be short-circuited to 1 as a docs-only change,
    # which is both the wrong score and the wrong reason. Score its tests
    # instead; only a PR with nothing left at all falls through empty.
    if not selected_files:
        selected_files, excerpt_lines, additions, deletions = _select_files(
            sections, hunks_per_file, exclude_tests=False
        )

    # Combine and truncate
    excerpt = "\n".join(excerpt_lines)
    truncated, _tok = truncate_to_token_limit(excerpt, max_tokens)

    # Stats reflect only scoreable content: markdown/lockfiles (filter_file),
    # test files (is_test_path) and comment/blank changes
    # (strip_comment_changes) no longer inflate the counts the LLM sees.
    stats = build_stats(
        {
            **meta,
            "additions": additions,
            "deletions": deletions,
            "changed_files": len(selected_files),
        },
        selected_files,
    )

    return truncated, stats, selected_files
