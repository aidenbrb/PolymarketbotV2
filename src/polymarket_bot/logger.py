"""Console + file logging configuration."""

from __future__ import annotations

import logging
import logging.handlers
import sys

from . import config

_CONFIGURED = False


def setup_logging(log_level: str | None = None) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return

    config.ensure_data_dirs()
    settings = config.load_settings()
    level = getattr(logging, (log_level or settings.log_level).upper(), logging.INFO)

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger("polymarket_bot")
    root.setLevel(level)
    root.propagate = False

    if root.handlers:
        _CONFIGURED = True
        return

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    root.addHandler(console_handler)

    # Never attach the real file handler under pytest -- loggers are
    # module-level singletons configured once on first import, which for a
    # test run means the very first test module collected. Without this
    # guard, every test that exercises a logged code path (which is most of
    # them) writes real entries into the actual production logs/bot.log,
    # permanently mixing synthetic test data (fake slugs like "m1", fake
    # exceptions like "boom"/"network error") into real trading history with
    # no way to tell them apart later. "pytest" in sys.modules is true for
    # the whole test session (pytest imports itself before collecting any
    # test module), so this never fires in a real `live-start` process.
    if "pytest" not in sys.modules:
        file_handler = _build_file_handler(settings)
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    _CONFIGURED = True


def _build_file_handler(settings: config.Settings) -> logging.handlers.RotatingFileHandler:
    # Rotating, not a plain FileHandler -- a confirmed real incident left a
    # crashed process's diagnostic trail effectively undiagnosable;
    # rotation keeps the file bounded regardless of run length. See
    # live/RUNBOOK.md's most recent section.
    return logging.handlers.RotatingFileHandler(
        config.LOGS_DIR / "bot.log",
        maxBytes=settings.log_max_bytes,
        backupCount=settings.log_backup_count,
        encoding="utf-8",
    )


def get_logger(name: str) -> logging.Logger:
    setup_logging()
    return logging.getLogger(f"polymarket_bot.{name}")
