"""Read-both-ends window tests (Three-Librarians D6 alignment)."""
from plastic_promise.core.reranker import _both_ends_window, _BOTH_ENDS_JOINER


def test_short_text_passes_verbatim():
    text = "short rule text"
    assert _both_ends_window(text, 1200) == text


def test_long_text_keeps_head_and_tail_within_budget():
    head_line = "HEAD-MARKER turn order begins here"
    tail_line = "TAIL-MARKER the buried conclusion lives here"
    filler = "x" * 3000
    text = head_line + filler + tail_line
    windowed = _both_ends_window(text, 1200)
    assert len(windowed) <= 1200
    assert windowed.startswith("HEAD-MARKER")
    assert windowed.rstrip().endswith("the buried conclusion lives here")
    assert _BOTH_ENDS_JOINER in windowed  # elision is explicit, never silent


def test_window_is_deterministic_and_monotone_budget():
    text = "y" * 5000 + "END"
    first = _both_ends_window(text, 800)
    second = _both_ends_window(text, 800)
    assert first == second
    bigger = _both_ends_window(text, 1600)
    assert len(first) <= 800 and len(bigger) <= 1600


def test_zero_budget_returns_empty_and_exact_fit_passes():
    assert _both_ends_window("anything", 0) == ""
    exact = "z" * 1200
    assert _both_ends_window(exact, 1200) == exact