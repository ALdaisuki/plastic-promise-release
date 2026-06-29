"""Retry decorator with exponential backoff."""

import functools
import time
import warnings
from collections.abc import Callable
from typing import Any, TypeVar

F = TypeVar("F", bound=Callable[..., Any])


def retry(
    max_attempts: int = 3,
    backoff_factor: float = 2.0,
) -> Callable[[F], F]:
    """Decorator: retry a function on failure with exponential backoff.

    On each retry, waits (backoff_factor ** (attempt - 1)) seconds before
    retrying. Emits a warning on each failure.

    Args:
        max_attempts: Maximum number of attempts including the first call.
        backoff_factor: Multiplier for exponential backoff delay.

    Returns:
        A decorated version of the function with retry behavior.

    Raises:
        ValueError: If max_attempts < 1 or backoff_factor < 0.

    Example:
        >>> @retry(max_attempts=3, backoff_factor=2)
        ... def flaky():
        ...     ...
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    if backoff_factor < 0:
        raise ValueError("backoff_factor must be >= 0")

    def decorator(func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exception: Exception | None = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_exception = e
                    if attempt < max_attempts:
                        delay = backoff_factor ** (attempt - 1)
                        warnings.warn(
                            f"{func.__name__} failed (attempt {attempt}/{max_attempts}): {e}. "
                            f"Retrying in {delay:.1f}s..."
                        )
                        time.sleep(delay)
            # All attempts exhausted
            raise last_exception  # type: ignore[misc]

        return wrapper  # type: ignore[return-value]

    return decorator


if __name__ == "__main__":
    # Quick smoke tests
    print("Testing retry...")

    # Test 1: Successful on first attempt
    call_count_ok: list[int] = [0]

    @retry(max_attempts=3)
    def always_works() -> str:
        call_count_ok[0] += 1
        return "ok"

    assert always_works() == "ok"
    assert call_count_ok[0] == 1
    print("  always_works: OK")

    # Test 2: Succeeds on retry
    call_count_retry: list[int] = [0]

    @retry(max_attempts=3, backoff_factor=0)  # factor=0 means no delay (backoff 0**n = 0)
    def works_on_third() -> str:
        call_count_retry[0] += 1
        if call_count_retry[0] < 3:
            raise ValueError("not yet")
        return "finally"

    assert works_on_third() == "finally"
    assert call_count_retry[0] == 3
    print("  works_on_third: OK")

    # Test 3: Exhausts all attempts
    call_count_fail: list[int] = [0]

    @retry(max_attempts=2, backoff_factor=0)
    def never_works() -> str:
        call_count_fail[0] += 1
        raise RuntimeError("always fails")

    try:
        never_works()
        assert False, "Should have raised RuntimeError"
    except RuntimeError:
        pass
    assert call_count_fail[0] == 2
    print("  never_works: OK")

    # Test 4: Invalid parameters
    try:
        retry(max_attempts=0)
        assert False
    except ValueError:
        pass
    try:
        retry(backoff_factor=-1)
        assert False
    except ValueError:
        pass
    try:
        retry(max_attempts=0)
        assert False
    except ValueError:
        pass
    print("  invalid_params: OK")

    print("All tests passed!")
