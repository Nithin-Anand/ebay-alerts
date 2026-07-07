"""Tests for saving searches back to YAML (used by the web UI)."""
import pytest

from app.config_loader import load_searches, save_searches
from app.models import LlmConfig, Search


def _search(**overrides) -> Search:
    base = dict(id="test-search", name="Test", query="test query")
    return Search.model_validate({**base, **overrides})


def test_roundtrip_preserves_all_fields(tmp_path):
    original = _search(
        poll_interval_seconds=120,
        sort="ending_soonest",
        limit=20,
        enabled=False,
        filters={
            "condition": ["USED"],
            "buying_options": ["AUCTION"],
            "price_min": "30.50",
            "price_max": "150",
            "sellers": ["someuser"],
        },
        llm={
            "enabled": True,
            "criteria": "Line one.\nLine two with detail.\n",
            "min_score_to_notify": 6,
            "skip_verdicts": ["skip", "maybe"],
            "model": "llava",
        },
        notification={"pushover_priority": 1, "pushover_sound": "siren"},
    )
    path = tmp_path / "searches.yaml"
    save_searches(path, [original])
    reloaded = load_searches(path)

    assert len(reloaded) == 1
    assert reloaded[0].model_dump() == original.model_dump()


def test_explicit_none_delivery_country_survives(tmp_path):
    """delivery_country defaults to GB but can be explicitly nulled —
    saving must not silently restore the default."""
    original = _search(filters={"delivery_country": None})
    path = tmp_path / "searches.yaml"
    save_searches(path, [original])
    assert load_searches(path)[0].filters.delivery_country is None


def test_default_fields_are_omitted_from_file(tmp_path):
    path = tmp_path / "searches.yaml"
    save_searches(path, [_search()])
    text = path.read_text()
    assert "poll_interval_seconds" not in text  # default 600
    assert "enabled" not in text               # default true
    assert "llm" not in text                   # not configured


def test_multiline_criteria_saved_as_literal_block(tmp_path):
    original = _search(llm={"criteria": "First line.\nSecond line.\n"})
    path = tmp_path / "searches.yaml"
    save_searches(path, [original])
    assert "criteria: |" in path.read_text()


def test_saved_file_has_managed_header(tmp_path):
    path = tmp_path / "searches.yaml"
    save_searches(path, [_search()])
    assert path.read_text().startswith("# eBay Alerts")


def test_invalid_id_rejected():
    with pytest.raises(Exception):
        _search(id="Has Spaces And Caps")
