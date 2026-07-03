"""Input validation utilities — email and URL validators."""

import re

# Email regex: follows RFC 5322 simplified — local@domain.tld
_EMAIL_RE = re.compile(
    r"^[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+"
    r"@"
    r"(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+"
    r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$"
)

# URL regex: scheme://host[:port][/path]
_URL_RE = re.compile(
    r"^https?://"
    r"(?:(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,})"  # hostname
    r"(?::\d{1,5})?"  # optional port
    r"(?:/[^\s]*)?$"  # optional path
)


def validate_email(email: str) -> bool:
    """Check whether a string is a valid-looking email address.

    Uses a simplified RFC 5322 pattern — local-part@domain.tld.

    Args:
        email: The string to validate.

    Returns:
        True if the string looks like a valid email address, False otherwise.

    Example:
        >>> validate_email("user@example.com")
        True
        >>> validate_email("not-an-email")
        False
    """
    if not isinstance(email, str) or not email:
        return False
    return bool(_EMAIL_RE.match(email))


def validate_url(url: str) -> bool:
    """Check whether a string is a valid-looking HTTP(S) URL.

    Pattern: scheme://hostname[:port][/path]. The hostname must include
    at least one dot and a TLD of at least 2 characters.

    Args:
        url: The string to validate.

    Returns:
        True if the string looks like a valid URL, False otherwise.

    Example:
        >>> validate_url("https://example.com")
        True
        >>> validate_url("not-a-url")
        False
    """
    if not isinstance(url, str) or not url:
        return False
    return bool(_URL_RE.match(url))


if __name__ == "__main__":
    # Quick smoke tests
    print("Testing validator...")

    # validate_email
    assert validate_email("user@example.com") is True
    assert validate_email("a.b+c@sub.domain.co.uk") is True
    assert validate_email("user@xn--fsq.com") is True
    assert validate_email("") is False
    assert validate_email("@missing-local.com") is False
    assert validate_email("missing-at-sign.com") is False
    assert validate_email("missing@domain") is False
    assert validate_email(123) is False  # type: ignore[arg-type]
    print("  validate_email: OK")

    # validate_url
    assert validate_url("https://example.com") is True
    assert validate_url("http://sub.example.org/path?q=1") is True
    assert validate_url("https://example.com:8080/api") is True
    assert validate_url("") is False
    assert validate_url("not-a-url") is False
    assert validate_url("ftp://example.com") is False
    assert validate_url("https://") is False
    assert validate_url(123) is False  # type: ignore[arg-type]
    print("  validate_url: OK")

    print("All tests passed!")
