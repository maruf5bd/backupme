import logging
import sys


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"backupme.{name}")


def setup_logging(verbose: bool = False) -> None:
    """Configure output for all backupme.* loggers. Library code must never
    call this — only CLI entry points do."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
        force=True,
    )
