"""Tests for preprocess module."""

from cli.preprocess import redact, filter_file, parse_diff_sections, build_stats


def test_redact_secrets():
    """Test secret redaction."""
    text = 'api_key = "secret123456"'
    result = redact(text)
    assert "[REDACTED_SECRET]" in result
    assert "secret123456" not in result


def test_redact_emails():
    """Test email redaction."""
    text = "Contact: user@example.com"
    result = redact(text)
    assert "[REDACTED_EMAIL]" in result
    assert "user@example.com" not in result


def test_filter_file():
    """Test file filtering."""
    assert filter_file("src/main.py") is True
    assert filter_file("node_modules/package.js") is False
    assert filter_file("dist/bundle.js") is False
    assert filter_file("package-lock.json") is False
    assert filter_file("test.png") is False
    assert filter_file("README.md") is False
    assert filter_file("docs/guide.mdx") is False
    assert filter_file(".cursor/rules/style.mdc") is False


def test_filter_file_lock_yaml():
    """Generated *.lock.yml / *.lock.yaml files should be filtered out."""
    # gh-aw and similar tools produce these as compiled lockfiles
    assert filter_file(".github/workflows/ks-drift-detector.lock.yml") is False
    assert filter_file(".github/workflows/some.lock.yaml") is False
    # But regular .yml / .yaml files should still pass through
    assert filter_file(".github/workflows/build.yml") is True
    assert filter_file("config/app.yaml") is True


def test_parse_diff_sections():
    """Test diff parsing."""
    diff = """diff --git a/file.py b/file.py
index 123..456
--- a/file.py
+++ b/file.py
@@ -1,3 +1,3 @@
 line1
-line2
+line2_modified
 line3
"""
    sections = parse_diff_sections(diff)
    assert "file.py" in sections
    assert len(sections["file.py"]) > 0


def test_build_stats():
    """Test stats building."""
    meta = {
        "additions": 10,
        "deletions": 5,
        "changed_files": 2,
    }
    files = ["file1.py", "file2.ts"]
    stats = build_stats(meta, files)
    assert stats["additions"] == 10
    assert stats["deletions"] == 5
    assert stats["changedFiles"] == 2
    assert stats["fileCount"] == 2
    assert "byExt" in stats
    assert "byLang" in stats


class TestCommentStripping:
    """PLT-3619: comment/blank-only changes are excluded from scoring."""

    def test_is_comment_line_by_language(self):
        from cli.preprocess import is_comment_line

        assert is_comment_line("# a comment", "py") is True
        assert is_comment_line("x = 1  # trailing", "py") is False
        assert is_comment_line("// note", "ts") is True
        assert is_comment_line("/* one-liner */", "go") is True
        assert is_comment_line("* doc body", "java") is True
        assert is_comment_line("*/", "java") is True
        assert is_comment_line("*ptr = x;", "c") is False
        assert is_comment_line("-- drop me", "sql") is True
        assert is_comment_line("<!-- note -->", "html") is True
        assert is_comment_line("# yaml comment", "yaml") is True
        # Unknown extensions are never treated as comments
        assert is_comment_line("# maybe", "unknownext") is False

    def test_strip_comment_changes_drops_comment_only_hunk(self):
        from cli.preprocess import strip_comment_changes

        lines = [
            "diff --git a/f.py b/f.py",
            "--- a/f.py",
            "+++ b/f.py",
            "@@ -1,2 +1,2 @@",
            "-# old comment",
            "+# new comment",
            "@@ -10,2 +10,2 @@",
            "-x = 1",
            "+x = 2",
        ]
        out, adds, dels = strip_comment_changes(lines, "py")
        assert adds == 1 and dels == 1
        # comment-only hunk removed entirely, code hunk kept
        assert sum(1 for line in out if line.startswith("@@ ")) == 1
        assert "+x = 2" in out
        assert "+# new comment" not in out

    def test_strip_keeps_code_with_trailing_comment(self):
        from cli.preprocess import strip_comment_changes

        lines = ["@@ -1,1 +1,1 @@", "+x = compute()  # important"]
        out, adds, dels = strip_comment_changes(lines, "py")
        assert adds == 1
        assert "+x = compute()  # important" in out

    def test_process_diff_drops_comment_only_file(self):
        from cli.preprocess import process_diff

        diff = (
            "diff --git a/notes.py b/notes.py\n"
            "--- a/notes.py\n"
            "+++ b/notes.py\n"
            "@@ -1,1 +1,1 @@\n"
            "-# old\n"
            "+# new\n"
            "diff --git a/app.py b/app.py\n"
            "--- a/app.py\n"
            "+++ b/app.py\n"
            "@@ -1,1 +1,1 @@\n"
            "-x = 1\n"
            "+x = 2\n"
        )
        meta = {"additions": 2, "deletions": 2, "changed_files": 2}
        excerpt, stats, files = process_diff(diff, meta)
        assert files == ["app.py"]
        # stats reflect the filtered diff, not the raw meta counts
        assert stats["additions"] == 1
        assert stats["deletions"] == 1
        assert stats["changedFiles"] == 1
        assert "notes.py" not in excerpt

    def test_process_diff_all_docs_returns_no_files(self):
        from cli.preprocess import process_diff

        diff = (
            "diff --git a/README.md b/README.md\n"
            "--- a/README.md\n"
            "+++ b/README.md\n"
            "@@ -1,1 +1,2 @@\n"
            " # Title\n"
            "+More docs.\n"
        )
        meta = {"additions": 1, "deletions": 0, "changed_files": 1}
        excerpt, stats, files = process_diff(diff, meta)
        assert files == []
        assert excerpt == ""
        assert stats["additions"] == 0

    def test_process_diff_hunk_cap_applies_after_stripping(self):
        from cli.preprocess import process_diff

        # Three hunks: comment-only, code, code. With hunks_per_file=2 the
        # comment-only hunk must not consume a slot.
        diff = (
            "diff --git a/app.py b/app.py\n"
            "--- a/app.py\n"
            "+++ b/app.py\n"
            "@@ -1,1 +1,1 @@\n"
            "+# comment\n"
            "@@ -10,1 +10,1 @@\n"
            "+a = 1\n"
            "@@ -20,1 +20,1 @@\n"
            "+b = 2\n"
        )
        meta = {"additions": 3, "deletions": 0, "changed_files": 1}
        excerpt, stats, files = process_diff(diff, meta, hunks_per_file=2)
        assert "+a = 1" in excerpt
        assert "+b = 2" in excerpt
        assert "+# comment" not in excerpt
        # stats count ALL kept hunks, not just the excerpted ones
        assert stats["additions"] == 2
