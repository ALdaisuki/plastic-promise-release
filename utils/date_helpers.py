"""Date helper utilities using the datetime module."""

import datetime


def days_between(date1: datetime.date, date2: datetime.date) -> int:
    """Return the absolute number of days between two dates.

    Args:
        date1: The first date.
        date2: The second date.

    Returns:
        A non-negative integer representing the number of days
        between date1 and date2 (inclusive of one endpoint).

    Example:
        >>> from datetime import date
        >>> days_between(date(2026, 1, 1), date(2026, 1, 5))
        4
    """
    return abs((date2 - date1).days)


def is_weekend(d: datetime.date) -> bool:
    """Check whether a date falls on a weekend (Saturday or Sunday).

    Args:
        d: The date to check.

    Returns:
        True if the date is Saturday (weekday 5) or Sunday (weekday 6).

    Example:
        >>> from datetime import date
        >>> is_weekend(date(2026, 6, 27))  # Saturday
        True
        >>> is_weekend(date(2026, 6, 29))  # Monday
        False
    """
    return d.weekday() >= 5


def get_next_business_day(d: datetime.date) -> datetime.date:
    """Return the next business day on or after the given date.

    If the date is a weekday (Mon-Fri), it is returned unchanged.
    If it is a weekend, the following Monday is returned.

    Args:
        d: The reference date.

    Returns:
        A date object representing the next business day.

    Example:
        >>> from datetime import date
        >>> get_next_business_day(date(2026, 6, 26))  # Friday
        datetime.date(2026, 6, 26)
        >>> get_next_business_day(date(2026, 6, 27))  # Saturday
        datetime.date(2026, 6, 29)
    """
    while d.weekday() >= 5:
        d += datetime.timedelta(days=1)
    return d


if __name__ == "__main__":
    from datetime import date

    print("Testing date_helpers...")

    # days_between
    assert days_between(date(2026, 1, 1), date(2026, 1, 5)) == 4
    assert days_between(date(2026, 1, 5), date(2026, 1, 1)) == 4
    assert days_between(date(2026, 6, 29), date(2026, 6, 29)) == 0
    print("  days_between: OK")

    # is_weekend
    assert is_weekend(date(2026, 6, 27)) is True   # Saturday
    assert is_weekend(date(2026, 6, 28)) is True   # Sunday
    assert is_weekend(date(2026, 6, 29)) is False  # Monday
    assert is_weekend(date(2026, 6, 30)) is False  # Tuesday
    print("  is_weekend: OK")

    # get_next_business_day
    assert get_next_business_day(date(2026, 6, 26)) == date(2026, 6, 26)  # Fri → Fri
    assert get_next_business_day(date(2026, 6, 27)) == date(2026, 6, 29)  # Sat → Mon
    assert get_next_business_day(date(2026, 6, 28)) == date(2026, 6, 29)  # Sun → Mon
    assert get_next_business_day(date(2026, 6, 29)) == date(2026, 6, 29)  # Mon → Mon
    print("  get_next_business_day: OK")

    print("All tests passed!")
