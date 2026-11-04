"""アプリのログ（14日分ローテーション）。"""
from __future__ import annotations

import logging
import sys
from logging.handlers import TimedRotatingFileHandler

from app.config import get_config

_done = False


def setup_logging(name: str) -> None:
    global _done
    if _done:
        return
    _done = True
    cfg = get_config()
    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    if cfg.app_env != "test":
        try:
            cfg.logs_dir.mkdir(parents=True, exist_ok=True)
            fh = TimedRotatingFileHandler(cfg.logs_dir / f"{name}.log", when="midnight", backupCount=14,
                                          encoding="utf-8")
            fh.setFormatter(fmt)
            root.addHandler(fh)
        except OSError:
            pass
