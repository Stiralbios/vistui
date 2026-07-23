import inspect
import logging
import sys

from backend.settings import AppSettings
from loguru import logger


class InterceptHandler(logging.Handler):
    """
    Default handler from examples in loguru documentation.
    See https://loguru.readthedocs.io/en/stable/overview.html#entirely-compatible-with-standard-logging
    """

    def emit(self, record: logging.LogRecord):
        level: str | int
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        frame, depth = inspect.currentframe(), 0
        while frame and (depth == 0 or frame.f_code.co_filename == logging.__file__):
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def configure_loggers():
    """Configure loguru and intercept uvicorn/sqlalchemy logs."""
    settings = AppSettings()
    logger.remove()

    override_logger_names = (
        name
        for name in logging.root.manager.loggerDict
        if name.startswith("uvicorn.") or name.startswith("sqlalchemy.")
    )
    intercept_handler = InterceptHandler()
    for logger_name in override_logger_names:
        logger_to_override = logging.getLogger(logger_name)
        logger_to_override.handlers = []
        logger_to_override.propagate = False
        logger_to_override.handlers = [intercept_handler]

    logger.configure(handlers=[{"sink": sys.stderr, "level": settings.LOG_LEVEL, "colorize": True}])
