import logging
import logging.handlers

from polymarket_bot import config
from polymarket_bot import logger as logger_module


def test_setup_logging_never_attaches_a_file_handler_under_pytest():
    """Regression test: loggers are module-level singletons configured once
    on first import, so without this guard every test run permanently mixes
    synthetic test log data into the real production logs/bot.log with no
    way to tell it apart later (discovered 2026-07-06 -- roughly a third of
    the real file turned out to be test-run noise). "pytest" is always in
    sys.modules during a test session, so setup_logging() must never add a
    real FileHandler while it's running."""
    root = logging.getLogger("polymarket_bot")
    saved_handlers = list(root.handlers)
    saved_configured = logger_module._CONFIGURED
    try:
        root.handlers = []
        logger_module._CONFIGURED = False

        logger_module.setup_logging()

        assert not any(isinstance(h, logging.FileHandler) for h in root.handlers)
        assert any(isinstance(h, logging.StreamHandler) for h in root.handlers)
    finally:
        for handler in root.handlers:
            if handler not in saved_handlers:
                handler.close()
        root.handlers = saved_handlers
        logger_module._CONFIGURED = saved_configured


def test_build_file_handler_is_rotating_with_configured_size_and_backup_count(tmp_path, monkeypatch):
    # Must never touch the real project's logs/bot.log from a test run --
    # same real-contamination risk the pytest-guard test above exists for.
    monkeypatch.setattr(logger_module.config, "LOGS_DIR", tmp_path)
    settings = config.Settings(log_max_bytes=123456, log_backup_count=4)

    handler = logger_module._build_file_handler(settings)
    try:
        assert isinstance(handler, logging.handlers.RotatingFileHandler)
        assert handler.maxBytes == 123456
        assert handler.backupCount == 4
    finally:
        handler.close()
