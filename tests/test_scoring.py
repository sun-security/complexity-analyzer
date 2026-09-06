"""Tests for scoring module."""

import pytest
from cli.scoring import parse_complexity_response, InvalidResponseError


def test_parse_valid_response():
    """Test parsing valid JSON response."""
    response = '{"complexity": 5, "explanation": "Test explanation"}'
    result = parse_complexity_response(response)
    assert result["complexity"] == 5
    assert result["explanation"] == "Test explanation"


def test_parse_response_with_extra_text():
    """Test parsing response with extra text."""
    response = 'Some text {"complexity": 7, "explanation": "Complex"} more text'
    result = parse_complexity_response(response)
    assert result["complexity"] == 7
    assert result["explanation"] == "Complex"


def test_parse_response_clamps_range():
    """Test that complexity is clamped to 1-10."""
    response = '{"complexity": 15, "explanation": "Test"}'
    result = parse_complexity_response(response)
    assert result["complexity"] == 10

    response = '{"complexity": -5, "explanation": "Test"}'
    result = parse_complexity_response(response)
    assert result["complexity"] == 1


def test_parse_response_sanitizes_newlines():
    """Test that newlines in explanation are replaced."""
    response = '{"complexity": 5, "explanation": "Line1\\nLine2"}'
    result = parse_complexity_response(response)
    assert "\n" not in result["explanation"]


def test_parse_invalid_json():
    """Test parsing invalid JSON."""
    with pytest.raises(InvalidResponseError):
        parse_complexity_response("not json")


def test_parse_missing_keys():
    """Test parsing response with missing keys."""
    with pytest.raises(InvalidResponseError):
        parse_complexity_response('{"complexity": 5}')
    with pytest.raises(InvalidResponseError):
        parse_complexity_response('{"explanation": "test"}')


class TestMaxScoreOverride:
    """sun-security fork: COMPLEXITY_MAX_SCORE widens the clamp + prompt scale."""

    def test_clamp_uses_env_max_score(self, monkeypatch):
        monkeypatch.setenv("COMPLEXITY_MAX_SCORE", "100")
        result = parse_complexity_response('{"complexity": 73, "explanation": "x"}')
        assert result["complexity"] == 73

    def test_clamp_still_caps_at_env_max(self, monkeypatch):
        monkeypatch.setenv("COMPLEXITY_MAX_SCORE", "100")
        result = parse_complexity_response('{"complexity": 400, "explanation": "x"}')
        assert result["complexity"] == 100

    def test_default_clamp_without_env(self, monkeypatch):
        monkeypatch.delenv("COMPLEXITY_MAX_SCORE", raising=False)
        result = parse_complexity_response('{"complexity": 73, "explanation": "x"}')
        assert result["complexity"] == 10

    def test_invalid_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("COMPLEXITY_MAX_SCORE", "banana")
        result = parse_complexity_response('{"complexity": 73, "explanation": "x"}')
        assert result["complexity"] == 10

    def test_default_prompt_rewritten(self, monkeypatch):
        from cli.analyze import load_prompt

        monkeypatch.setenv("COMPLEXITY_MAX_SCORE", "100")
        text = load_prompt()
        assert "1–100 integer scale" in text
        assert "<int 1..100>" in text
        assert "between 1 and 100 inclusive" in text
        assert "1..10>" not in text

    def test_default_prompt_untouched_at_default_scale(self, monkeypatch):
        from cli.analyze import load_prompt

        monkeypatch.delenv("COMPLEXITY_MAX_SCORE", raising=False)
        text = load_prompt()
        assert "<int 1..10>" in text
