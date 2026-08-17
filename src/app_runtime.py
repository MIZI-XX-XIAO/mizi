"""本文件管理应用版本、用户可写目录、滚动日志和诊断元数据。"""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256
from logging import Logger, getLogger
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
import json
import logging
import os
import platform
import sys
import tempfile
import uuid


APP_NAME = "MEA多工站缺陷规律分析"
APP_SLUG = "MEA5SDefectAnalysis"
APP_VERSION = "2.2.0"
ALGORITHM_VERSION = "3.0"


def user_data_dir() -> Path:
    """返回无需管理员权限即可写入的应用数据目录。"""
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    path = base / APP_SLUG
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        path = Path(tempfile.gettempdir()) / APP_SLUG
        path.mkdir(parents=True, exist_ok=True)
    return path


def configure_logging() -> tuple[Logger, Path]:
    log_dir = user_data_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "application.log"
    logger = getLogger(APP_SLUG)
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = RotatingFileHandler(
            log_path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter(
            "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
        ))
        logger.addHandler(handler)
    logger.info("application_start version=%s python=%s", APP_VERSION, sys.version.split()[0])
    return logger, log_path


def new_error_id() -> str:
    return datetime.now().strftime("%Y%m%d") + "-" + uuid.uuid4().hex[:8].upper()


def file_fingerprint(path: Path) -> dict[str, Any]:
    """以流式方式计算输入文件摘要，避免大文件额外占用内存。"""
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    stat = path.stat()
    return {
        "path": str(path),
        "bytes": stat.st_size,
        "modified_at": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds"),
        "sha256": digest.hexdigest(),
    }


def runtime_metadata() -> dict[str, Any]:
    return {
        "application_version": APP_VERSION,
        "algorithm_version": ALGORITHM_VERSION,
        "python": sys.version,
        "platform": platform.platform(),
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
