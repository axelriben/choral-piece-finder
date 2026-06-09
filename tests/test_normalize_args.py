"""Unit tests for dispatcher._normalize_args."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from dispatcher import _normalize_args


def test_unicode_escape_sequences():
    result = _normalize_args({"query": "V\\u00e5rnatt"})
    assert result["query"] == "Vårnatt", repr(result["query"])


def test_swedish_umlauts():
    result = _normalize_args({"composer": "St\\u00e4mning"})
    assert result["composer"] == "Stämning", repr(result["composer"])


def test_empty_string_stripped():
    result = _normalize_args({"composer": "Stenhammar", "query": "", "voicing": ""})
    assert "query" not in result
    assert "voicing" not in result
    assert result["composer"] == "Stenhammar"


def test_null_string_stripped():
    result = _normalize_args({"composer": "Alfvén", "num_voices": "null", "period": "None"})
    assert "num_voices" not in result
    assert "period" not in result
    assert result["composer"] == "Alfvén"


def test_none_value_stripped():
    result = _normalize_args({"composer": "Palestrina", "voicing": None})
    assert "voicing" not in result


def test_boolean_coercion():
    # Use a non-filter-boolean key so False is not stripped
    result = _normalize_args({"some_flag": "true"})
    assert result["some_flag"] is True

    result = _normalize_args({"some_flag": "false"})
    assert result["some_flag"] is False

    result = _normalize_args({"some_flag": "True"})
    assert result["some_flag"] is True


def test_integer_coercion():
    result = _normalize_args({"num_voices": "4"})
    assert result["num_voices"] == 4
    assert isinstance(result["num_voices"], int)


def test_negative_integer_coercion():
    result = _normalize_args({"year_from": "-500"})
    assert result["year_from"] == -500


def test_real_unicode_passthrough():
    # Already-decoded Unicode should pass through unchanged
    result = _normalize_args({"query": "Vårnatt"})
    assert result["query"] == "Vårnatt"


def test_nonempty_values_preserved():
    result = _normalize_args({"composer": "Stenhammar", "period": "Late Romantic", "num_voices": 4})
    assert result == {"composer": "Stenhammar", "period": "Late Romantic", "num_voices": 4}


def test_filter_boolean_false_stripped():
    # has_free_score=False should be removed (LLM placeholder)
    result = _normalize_args({"composer": "Stenhammar", "has_free_score": False})
    assert "has_free_score" not in result
    assert result["composer"] == "Stenhammar"


def test_filter_boolean_true_kept():
    # has_free_score=True is a real filter and must pass through
    result = _normalize_args({"composer": "Stenhammar", "has_free_score": True})
    assert result["has_free_score"] is True


def test_filter_boolean_string_false_stripped():
    # String "false" → bool False → stripped
    result = _normalize_args({"has_free_score": "false", "include_non_choral": "false"})
    assert "has_free_score" not in result
    assert "include_non_choral" not in result


def test_filter_boolean_include_non_choral_true_kept():
    result = _normalize_args({"include_non_choral": True})
    assert result["include_non_choral"] is True


def test_empty_input():
    assert _normalize_args({}) == {}


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
            passed += 1
        except Exception as exc:
            print(f"  FAIL  {fn.__name__}: {exc}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
