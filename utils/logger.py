"""Simple logging wrapper with console and file output support."""

import logging
import sys
from pathlib import Path


def setup_logger(name: str, level: int = logging.INFO) -> logging.Logger:
    """Create and configure a logger with console and file output.

    Writes log files to a 'logs/' directory relative to the project root,
    with the filename derived from the logger name.

    Args:
        name: The logger name, also used for the log filename (name.log).
        level: The logging level (default: logging.INFO).

    Returns:
        A fully configured logging.Logger instance.

    Example:
        >>> logger = setup_logger("myapp", logging.DEBUG)
        >>> logger.info("Hello, world!")
    """
    logger = logging.getLogger(name)
    logger.setLevel(level)

    # Avoid duplicate handlers if called multiple times
    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler (stdout)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # File handler (logs/<name>.log)
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    file_handler = logging.FileHandler(log_dir / f"{name}.log", encoding="utf-8")
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger


if __name__ == "__main__":
    # Quick smoke test
    logger = setup_logger("test_logger", logging.DEBUG)
    logger.debug("This is a debug message.")
    logger.info("This is an info message.")
    logger.warning("This is a warning.")
    logger.error("This is an error.")

    # Verify log file was created
    log_path = Path("logs") / "test_logger.log"
    assert log_path.exists(), "Log file was not created"
    content = log_path.read_text(encoding="utf-8")
    assert "This is an info message." in content
    print("  logger: OK — console and file output both working")
