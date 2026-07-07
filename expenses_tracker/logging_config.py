"""Application logging setup (env-configurable without redeploy)."""
from __future__ import annotations

import logging

from expenses_tracker.config import Settings

_EMAIL_QUERY_LOGGERS = (
    "expenses_tracker.sync",
    "expenses_tracker.email_import",
)


def configure_logging(settings: Settings) -> None:
    """Apply logging levels from environment-backed settings."""
    if not settings.email_query_debug:
        return
    level = logging.DEBUG
    for name in _EMAIL_QUERY_LOGGERS:
        logging.getLogger(name).setLevel(level)
    logging.getLogger().info(
        "EMAIL_QUERY_DEBUG is enabled; verbose email query import logging is on "
        "(loggers: %s)",
        ", ".join(_EMAIL_QUERY_LOGGERS),
    )
