"""Math helper utilities — pure functions with type annotations."""


def clamp(value: float, min_val: float, max_val: float) -> float:
    """Clamp a value within the inclusive range [min_val, max_val].

    Args:
        value: The input value to clamp.
        min_val: The lower bound of the range.
        max_val: The upper bound of the range.

    Returns:
        The clamped value, guaranteed to be between min_val and max_val.

    Raises:
        ValueError: If min_val > max_val.
    """
    if min_val > max_val:
        raise ValueError(f"min_val ({min_val}) must not exceed max_val ({max_val})")
    if value < min_val:
        return min_val
    if value > max_val:
        return max_val
    return value


def factorial(n: int) -> int:
    """Compute the factorial of a non-negative integer n (n!).

    Args:
        n: A non-negative integer.

    Returns:
        The factorial of n.

    Raises:
        ValueError: If n is negative.
    """
    if n < 0:
        raise ValueError(f"n must be non-negative, got {n}")
    result = 1
    for i in range(2, n + 1):
        result *= i
    return result


def fibonacci(n: int) -> int:
    """Return the n-th Fibonacci number (0-indexed: F(0)=0, F(1)=1).

    Args:
        n: A non-negative integer index.

    Returns:
        The n-th Fibonacci number.

    Raises:
        ValueError: If n is negative.
    """
    if n < 0:
        raise ValueError(f"n must be non-negative, got {n}")
    if n == 0:
        return 0
    a, b = 0, 1
    for _ in range(2, n + 1):
        a, b = b, a + b
    return b


if __name__ == "__main__":
    # Quick smoke tests
    print("Testing math_helpers...")

    # clamp
    assert clamp(5, 0, 10) == 5
    assert clamp(-5, 0, 10) == 0
    assert clamp(15, 0, 10) == 10
    assert clamp(3.14, 1.0, 5.0) == 3.14
    try:
        clamp(0, 10, 5)
        assert False, "Should have raised ValueError"
    except ValueError:
        pass
    print("  clamp: OK")

    # factorial
    assert factorial(0) == 1
    assert factorial(1) == 1
    assert factorial(5) == 120
    assert factorial(10) == 3628800
    try:
        factorial(-1)
        assert False, "Should have raised ValueError"
    except ValueError:
        pass
    print("  factorial: OK")

    # fibonacci
    assert fibonacci(0) == 0
    assert fibonacci(1) == 1
    assert fibonacci(2) == 1
    assert fibonacci(10) == 55
    assert fibonacci(20) == 6765
    try:
        fibonacci(-1)
        assert False, "Should have raised ValueError"
    except ValueError:
        pass
    print("  fibonacci: OK")

    print("All tests passed!")
