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

    def test_wide_scale_loads_dimensions_prompt(self, monkeypatch):
        from cli.analyze import load_prompt

        monkeypatch.setenv("COMPLEXITY_MAX_SCORE", "100")
        text = load_prompt()
        assert "FIVE independent" in text
        assert "1–20 integer scale" in text
        assert '"scope"' in text and '"risk"' in text
        assert "{{DIM_CAP}}" not in text and "{{MAX_SCORE}}" not in text

    def test_default_prompt_untouched_at_default_scale(self, monkeypatch):
        from cli.analyze import load_prompt

        monkeypatch.delenv("COMPLEXITY_MAX_SCORE", raising=False)
        text = load_prompt()
        assert "<int 1..10>" in text


class TestMaxScoreRubricAndLabeler:
    """Fork round 2: the rubric bands must scale too, and the labeler bound."""

    def test_dimensions_parse_and_rescale(self, monkeypatch):
        monkeypatch.setenv("COMPLEXITY_MAX_SCORE", "100")
        r = parse_complexity_response(
            '{"scope": 8, "logic": 15, "integration": 6, "testing": 11, "risk": 9, "explanation": "x"}'
        )
        # raw 49 → rescaled round((49-5)*99/95)+1 = 47
        assert r["complexity"] == 47
        assert r["dimensions"] == {
            "scope": 8,
            "logic": 15,
            "integration": 6,
            "testing": 11,
            "risk": 9,
        }
        assert r["explanation"].startswith("[scope 8, logic 15,")

    def test_dimensions_bounds(self, monkeypatch):
        monkeypatch.setenv("COMPLEXITY_MAX_SCORE", "100")
        lo = parse_complexity_response(
            '{"scope":1,"logic":1,"integration":1,"testing":1,"risk":1,"explanation":"x"}'
        )
        hi = parse_complexity_response(
            '{"scope":20,"logic":25,"integration":20,"testing":20,"risk":20,"explanation":"x"}'
        )
        assert lo["complexity"] == 1
        assert hi["complexity"] == 100  # 25 clamps to cap 20

    def test_dimensions_missing_key_rejected(self, monkeypatch):
        monkeypatch.setenv("COMPLEXITY_MAX_SCORE", "100")
        import pytest

        with pytest.raises(InvalidResponseError):
            parse_complexity_response(
                '{"scope":5,"logic":5,"testing":5,"risk":5,"explanation":"x"}'
            )

    def test_single_mode_unaffected(self, monkeypatch):
        monkeypatch.delenv("COMPLEXITY_MAX_SCORE", raising=False)
        r = parse_complexity_response('{"complexity": 7, "explanation": "x"}')
        assert r["complexity"] == 7

    def test_labeler_accepts_wide_scores(self, monkeypatch):
        from cli import github

        monkeypatch.setenv("COMPLEXITY_MAX_SCORE", "100")
        # The bound check happens before any network call; complexity=85 must
        # pass validation (a network error afterwards would be a different
        # exception than ValueError).
        try:
            github.update_complexity_label("o", "r", 1, 85, token="x", timeout=0.001)
        except ValueError as e:
            raise AssertionError(f"85 rejected under max=100: {e}")
        except Exception:
            pass  # network failure expected — validation passed

    def test_labeler_still_rejects_over_max(self, monkeypatch):
        from cli import github

        monkeypatch.setenv("COMPLEXITY_MAX_SCORE", "100")
        try:
            github.update_complexity_label("o", "r", 1, 150, token="x", timeout=0.001)
            raise AssertionError("150 accepted")
        except ValueError:
            pass


class TestRiskExposure:
    """PLT-3619: the risk dimension is surfaced, rescaled to the total's scale."""

    def test_risk_rescaled_to_max_score(self, monkeypatch):
        monkeypatch.setenv("COMPLEXITY_MAX_SCORE", "100")
        r = parse_complexity_response(
            '{"scope": 8, "logic": 15, "integration": 6, "testing": 11, "risk": 9, "explanation": "x"}'
        )
        # risk raw 9 on 1-20 → round((9-1)*99/19)+1 = 43 on 1-100
        assert r["risk"] == 43

    def test_risk_bounds(self, monkeypatch):
        monkeypatch.setenv("COMPLEXITY_MAX_SCORE", "100")
        lo = parse_complexity_response(
            '{"scope":1,"logic":1,"integration":1,"testing":1,"risk":1,"explanation":"x"}'
        )
        hi = parse_complexity_response(
            '{"scope":1,"logic":1,"integration":1,"testing":1,"risk":20,"explanation":"x"}'
        )
        assert lo["risk"] == 1
        assert hi["risk"] == 100

    def test_single_score_mode_has_no_risk(self, monkeypatch):
        monkeypatch.delenv("COMPLEXITY_MAX_SCORE", raising=False)
        r = parse_complexity_response('{"complexity": 5, "explanation": "x"}')
        assert "risk" not in r
