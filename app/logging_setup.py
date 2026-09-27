"""Configuration du module ``logging`` : console + fichier rotatif ``logs/trading_bot.log``."""

from __future__ import annotations

import logging
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_TOKEN_IN_URL = re.compile(r"(token=)[^&\s\"']+")


class RedactTokens(logging.Filter):
    """Masque le jeton du dashboard passé dans l'URL du WebSocket (journaux uvicorn)."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if "token=" in message:
            record.msg, record.args = _TOKEN_IN_URL.sub(r"\g<1>***", message), None
        return True


def configure_logging(level: str = "INFO", log_dir: Path | None = Path("logs")) -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(log_dir / "trading_bot.log", maxBytes=5_000_000,
                                            backupCount=5, encoding="utf-8"))
    for handler in handlers:
        handler.addFilter(RedactTokens())
    logging.basicConfig(level=level, format=LOG_FORMAT, handlers=handlers, force=True)
    for noisy in ("httpx", "httpcore", "anthropic", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
