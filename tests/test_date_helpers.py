"""Parametrized pytest tests for utils/date_helpers.py."""

import datetime

import pytest

from utils.date_helpers import days_between, get_next_business_day, is_weekend


# ---------------------------------------------------------------------------
# days_between
# ---------------------------------------------------------------------------

DAYS_BETWEEN_CASES = [
    # (desc, date1, date2, expected)
    # --- basic ---
    ("one day apart", datetime.date(2026, 1, 1), datetime.date(2026, 1, 2), 1),
    ("five days apart", datetime.date(2026, 1, 1), datetime.date(2026, 1, 6), 5),
    ("same day", datetime.date(2026, 6, 29), datetime.date(2026, 6, 29), 0),
    ("reversed order", datetime.date(2026, 6, 29), datetime.date(2026, 6, 25), 4),
    # --- cross-year ---
    ("cross year: Dec 31 → Jan 1", datetime.date(2026, 12, 31), datetime.date(2027, 1, 1), 1),
    ("cross year: Jan 1 → Dec 31", datetime.date(2027, 1, 1), datetime.date(2026, 12, 31), 1),
    ("cross year: full year span", datetime.date(2026, 1, 1), datetime.date(2027, 2, 1), 396),
    # --- leap year: 2024 (leap), 2025 (non-leap) ---
    ("leap year: Feb 28 → Feb 29 (2024)", datetime.date(2024, 2, 28), datetime.date(2024, 2, 29), 1),
    ("leap year: Feb 28 → Mar 1 (2024)", datetime.date(2024, 2, 28), datetime.date(2024, 3, 1), 2),
    ("non-leap year: Feb 28 → Mar 1 (2025)", datetime.date(2025, 2, 28), datetime.date(2025, 3, 1), 1),
    ("leap year: full leap span", datetime.date(2024, 1, 1), datetime.date(2025, 1, 1), 366),
    ("non-leap year: full span", datetime.date(2025, 1, 1), datetime.date(2026, 1, 1), 365),
    # --- edge: date bounds ---
    ("min date diff", datetime.date(1, 1, 1), datetime.date(1, 1, 3), 2),
    ("max date diff", datetime.date(9999, 12, 29), datetime.date(9999, 12, 31), 2),
]


@pytest.mark.parametrize("desc, date1, date2, expected", DAYS_BETWEEN_CASES)
def test_days_between(desc, date1, date2, expected):
    result = days_between(date1, date2)
    assert result == expected, f"{desc}: expected {expected}, got {result}"


# ---------------------------------------------------------------------------
# is_weekend
# ---------------------------------------------------------------------------

IS_WEEKEND_CASES = [
    # (desc, date, expected)
    ("Saturday", datetime.date(2026, 6, 27), True),
    ("Sunday", datetime.date(2026, 6, 28), True),
    ("Monday", datetime.date(2026, 6, 29), False),
    ("Tuesday", datetime.date(2026, 6, 30), False),
    ("Wednesday", datetime.date(2026, 7, 1), False),
    ("Thursday", datetime.date(2026, 7, 2), False),
    ("Friday", datetime.date(2026, 7, 3), False),
    # --- cross-year weekend ---
    ("Saturday Dec 31 2022", datetime.date(2022, 12, 31), True),
    ("Sunday Jan 1 2023", datetime.date(2023, 1, 1), True),
    ("Monday Jan 2 2023", datetime.date(2023, 1, 2), False),
    # --- leap year weekend ---
    ("Friday Feb 28 2020", datetime.date(2020, 2, 28), False),
    ("Saturday Feb 29 2020 (leap day)", datetime.date(2020, 2, 29), True),
    ("Sunday Mar 1 2020", datetime.date(2020, 3, 1), True),
]


@pytest.mark.parametrize("desc, d, expected", IS_WEEKEND_CASES)
def test_is_weekend(desc, d, expected):
    result = is_weekend(d)
    assert result is expected, f"{desc}: expected {expected}, got {result}"


# ---------------------------------------------------------------------------
# get_next_business_day
# ---------------------------------------------------------------------------

NEXT_BIZ_DAY_CASES = [
    # (desc, input_date, expected_date)
    # Behaviour: returns the *earliest* business day *strictly after* input.
    ("Monday → Tuesday", datetime.date(2026, 6, 29), datetime.date(2026, 6, 30)),
    ("Tuesday → Wednesday", datetime.date(2026, 6, 30), datetime.date(2026, 7, 1)),
    ("Wednesday → Thursday", datetime.date(2026, 7, 1), datetime.date(2026, 7, 2)),
    ("Thursday → Friday", datetime.date(2026, 7, 2), datetime.date(2026, 7, 3)),
    ("Friday → Monday", datetime.date(2026, 7, 3), datetime.date(2026, 7, 6)),
    ("Saturday → Monday", datetime.date(2026, 6, 27), datetime.date(2026, 6, 29)),
    ("Sunday → Monday", datetime.date(2026, 6, 28), datetime.date(2026, 6, 29)),
    # --- cross-year weekend ---
    ("Saturday Dec 31 2022 → Mon Jan 2 2023", datetime.date(2022, 12, 31), datetime.date(2023, 1, 2)),
    ("Sunday Jan 1 2023 → Mon Jan 2 2023", datetime.date(2023, 1, 1), datetime.date(2023, 1, 2)),
    ("Friday Dec 30 2022 → Mon Jan 2 2023", datetime.date(2022, 12, 30), datetime.date(2023, 1, 2)),
    # --- leap year weekend ---
    ("Saturday Feb 29 2020 → Mon Mar 2 2020", datetime.date(2020, 2, 29), datetime.date(2020, 3, 2)),
    ("Friday Feb 28 2020 → Mon Mar 2 2020", datetime.date(2020, 2, 28), datetime.date(2020, 3, 2)),
]


@pytest.mark.parametrize("desc, input_date, expected_date", NEXT_BIZ_DAY_CASES)
def test_get_next_business_day(desc, input_date, expected_date):
    result = get_next_business_day(input_date)
    assert result == expected_date, f"{desc}: expected {expected_date}, got {result}"


# ---------------------------------------------------------------------------
# Input validation — TypeError on None / non-date
# ---------------------------------------------------------------------------

INVALID_INPUTS = [
    None,
    42,
    3.14,
    "2026-06-29",
    [],
    {},
    True,
    object(),
]


@pytest.mark.parametrize("bad_input", INVALID_INPUTS)
def test_days_between_rejects_bad_date1(bad_input):
    d = datetime.date(2026, 6, 29)
    with pytest.raises((TypeError, AttributeError)):
        days_between(bad_input, d)


@pytest.mark.parametrize("bad_input", INVALID_INPUTS)
def test_days_between_rejects_bad_date2(bad_input):
    d = datetime.date(2026, 6, 29)
    with pytest.raises((TypeError, AttributeError)):
        days_between(d, bad_input)


@pytest.mark.parametrize("bad_input", INVALID_INPUTS)
def test_is_weekend_rejects_bad_input(bad_input):
    with pytest.raises((TypeError, AttributeError)):
        is_weekend(bad_input)


@pytest.mark.parametrize("bad_input", INVALID_INPUTS)
def test_get_next_business_day_rejects_bad_input(bad_input):
    with pytest.raises((TypeError, AttributeError)):
        get_next_business_day(bad_input)


# ---------------------------------------------------------------------------
# Smoketest: no false positives for valid inputs
# ---------------------------------------------------------------------------

def test_all_functions_accept_date_instances():
    """Ensure none of the guards block legitimate datetime.date objects."""
    d1 = datetime.date(2026, 6, 29)
    d2 = datetime.date(2026, 7, 3)
    # Should not raise
    assert isinstance(days_between(d1, d2), int)
    assert isinstance(is_weekend(d1), bool)
    assert isinstance(get_next_business_day(d1), datetime.date)
