"""Tests for the Verdict Pydantic model (LLM response parsing)."""
import pytest
from pydantic import ValidationError

from app.models import Verdict


def test_valid_bid_verdict():
    v = Verdict.model_validate(
        {"recommend": "bid", "score": 8, "concerns": [], "notes": "Clean glass."}
    )
    assert v.recommend == "bid"
    assert v.score == 8
    assert v.concerns == []


def test_valid_skip_verdict():
    v = Verdict.model_validate(
        {"recommend": "skip", "score": 2, "concerns": ["haze mentioned"], "notes": "Skip."}
    )
    assert v.recommend == "skip"
    assert len(v.concerns) == 1


def test_maybe_with_defaults():
    v = Verdict.model_validate({"recommend": "maybe", "score": 5})
    assert v.concerns == []
    assert v.notes == ""


def test_score_boundary_zero():
    v = Verdict.model_validate({"recommend": "skip", "score": 0})
    assert v.score == 0


def test_score_boundary_ten():
    v = Verdict.model_validate({"recommend": "bid", "score": 10})
    assert v.score == 10


def test_invalid_recommend_raises():
    with pytest.raises(ValidationError):
        Verdict.model_validate({"recommend": "buy", "score": 8})


def test_score_above_max_raises():
    with pytest.raises(ValidationError):
        Verdict.model_validate({"recommend": "bid", "score": 11})


def test_score_below_min_raises():
    with pytest.raises(ValidationError):
        Verdict.model_validate({"recommend": "bid", "score": -1})


def test_multiple_concerns():
    v = Verdict.model_validate(
        {
            "recommend": "skip",
            "score": 1,
            "concerns": ["fungus mentioned", "stiff focus ring", "as-is sale"],
            "notes": "Multiple defects noted.",
        }
    )
    assert len(v.concerns) == 3
