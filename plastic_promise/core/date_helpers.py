"""Date utility helpers built on datetime.date.

Provides simple date math: days between two dates, weekend detection,
and next business day calculation.
"""

from __future__ import annotations

import datetime


def days_between(date1: datetime.date, date2: datetime.date) -> int:
    """Return the absolute number of calendar days between *date1* and *date2*.

    The result is always non-negative regardless of argument order.
    """
    return abs((date2 - date1).days)


def is_weekend(date: datetime.date) -> bool:
    """Return ``True`` when *date* falls on a Saturday or Sunday."""
    return date.weekday() >= 5  # Monday=0 … Sunday=6


def get_next_business_day(date: datetime.date) -> datetime.date:
    """Return the earliest business day strictly after *date*.

    If *date* itself is a business day (Mon–Fri) the result is the next
    calendar day.  If *date* falls on a weekend the function advances to
    the following Monday.
    """
    one_day = datetime.timedelta(days=1)
    next_day = date + one_day
    while is_weekend(next_day):
        next_day += one_day
    return next_day


if __name__ == "__main__":
    # -- quick smoke tests ---------------------------------------------------
    d1 = datetime.date(2026, 1, 1)
    d2 = datetime.date(2026, 1, 10)

    assert days_between(d1, d2) == 9
    assert days_between(d2, d1) == 9, "order independence"

    mon = datetime.date(2026, 6, 29)  # Monday
    fri = datetime.date(2026, 7, 3)  # Friday
    sat = datetime.date(2026, 7, 4)  # Saturday
    sun = datetime.date(2026, 7, 5)  # Sunday

    assert not is_weekend(mon)
    assert not is_weekend(fri)
    assert is_weekend(sat)
    assert is_weekend(sun)

    assert get_next_business_day(mon) == datetime.date(2026, 6, 30)  # Tue
    assert get_next_business_day(fri) == datetime.date(2026, 7, 6)  # Mon
    assert get_next_business_day(sat) == datetime.date(2026, 7, 6)  # Mon
    assert get_next_business_day(sun) == datetime.date(2026, 7, 6)  # Mon

    print("All tests passed.")
