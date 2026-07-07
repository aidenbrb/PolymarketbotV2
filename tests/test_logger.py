import logging

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
