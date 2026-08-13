from __future__ import annotations

import csv
import json
import logging
import os
import re
import shutil
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Literal


SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}
STATE_VERSION = 1


def natural_key(path: Path) -> list[object]:
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", path.name)]


def scan_images(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    try:
        items = [p for p in folder.iterdir() if p.is_file() and p.suffix.casefold() in SUPPORTED_EXTENSIONS]
    except OSError:
        return []
    return sorted(items, key=natural_key)


def app_data_dir() -> Path:
    base = os.getenv("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    result = Path(base) / "ImageReviewClassifier"
    result.mkdir(parents=True, exist_ok=True)
    return result


def configure_logging() -> Path:
    target = app_data_dir() / "logs"
    target.mkdir(parents=True, exist_ok=True)
    log_file = target / "application.log"
    logging.basicConfig(
        filename=log_file,
        level=logging.INFO,
        encoding="utf-8",
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    return log_file


@dataclass(frozen=True)
class Category:
    name: str
    destination: str


@dataclass
class MoveRecord:
    original: str
    destination: str
    category: str
    original_name: str
    final_name: str
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))


@dataclass
class SessionState:
    source: str
    categories: list[Category]
    pending: list[str]
    deferred: list[str] = field(default_factory=list)
    history: list[MoveRecord] = field(default_factory=list)
    total: int = 0
    current_index: int = 0
    version: int = STATE_VERSION
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])

    def to_dict(self) -> dict:
        return {
            **asdict(self),
            "categories": [asdict(x) for x in self.categories],
            "history": [asdict(x) for x in self.history],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SessionState":
        if data.get("version") != STATE_VERSION:
            raise ValueError("任务状态版本不兼容")
        return cls(
            source=data["source"],
            categories=[Category(**x) for x in data.get("categories", [])],
            pending=list(data.get("pending", [])),
            deferred=list(data.get("deferred", [])),
            history=[MoveRecord(**x) for x in data.get("history", [])],
            total=int(data.get("total", 0)),
            current_index=int(data.get("current_index", 0)),
            version=int(data["version"]),
            session_id=data.get("session_id", uuid.uuid4().hex[:10]),
        )


class StateStore:
    def __init__(self, path: Path | None = None):
        self.path = path or app_data_dir() / "active_session.json"

    def save(self, state: SessionState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(state.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, self.path)

    def load(self) -> SessionState | None:
        if not self.path.exists():
            return None
        return SessionState.from_dict(json.loads(self.path.read_text(encoding="utf-8")))

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


CSV_HEADERS = ["操作时间", "原始文件名", "原始完整路径", "人工分类", "实际目标路径", "动作类型", "操作结果", "重命名后文件名", "失败原因"]


class AuditLog:
    def __init__(self, source: Path, session_id: str):
        self.path = source / f"复核记录_{session_id}.csv"

    def write(
        self,
        *,
        original: Path,
        category: str,
        destination: Path | None,
        action: str,
        result: str,
        final_name: str = "",
        error: str = "",
    ) -> None:
        new_file = not self.path.exists()
        with self.path.open("a", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            if new_file:
                writer.writerow(CSV_HEADERS)
            writer.writerow([
                datetime.now().isoformat(timespec="seconds"), original.name, str(original), category,
                str(destination or ""), action, result, final_name, error,
            ])
            handle.flush()
            os.fsync(handle.fileno())


def unique_destination(destination: Path) -> Path:
    if not destination.exists():
        return destination
    counter = 1
    while True:
        candidate = destination.with_name(f"{destination.stem}_{counter}{destination.suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


class ReviewEngine:
    def __init__(self, state: SessionState, store: StateStore):
        self.state = state
        self.store = store
        self.audit = AuditLog(Path(state.source), state.session_id)
        self.reconcile()

    @classmethod
    def create(cls, source: Path, categories: Iterable[Category], store: StateStore) -> "ReviewEngine":
        images = scan_images(source)
        state = SessionState(
            source=str(source.resolve()), categories=list(categories), pending=[str(p.resolve()) for p in images], total=len(images)
        )
        engine = cls(state, store)
        engine.save()
        return engine

    def reconcile(self) -> None:
        self.state.pending = [p for p in self.state.pending if Path(p).is_file()]
        self.state.deferred = [p for p in self.state.deferred if Path(p).is_file()]
        if self.state.pending:
            self.state.current_index = min(max(0, self.state.current_index), len(self.state.pending) - 1)
        else:
            self.state.current_index = 0

    def save(self) -> None:
        self.store.save(self.state)

    @property
    def current(self) -> Path | None:
        if not self.state.pending:
            return None
        return Path(self.state.pending[self.state.current_index])

    @property
    def completed_count(self) -> int:
        return len(self.state.history)

    def navigate(self, delta: int) -> None:
        if self.state.pending:
            self.state.current_index = (self.state.current_index + delta) % len(self.state.pending)
            self.save()

    def defer_current(self) -> Path | None:
        current = self.current
        if current is None:
            return None
        value = self.state.pending.pop(self.state.current_index)
        if value not in self.state.deferred:
            self.state.deferred.append(value)
        if self.state.pending:
            self.state.current_index %= len(self.state.pending)
        else:
            self.state.current_index = 0
        self.audit.write(original=current, category="", destination=None, action="暂不处理", result="成功")
        self.save()
        return current

    def restore_deferred(self) -> bool:
        valid = [p for p in self.state.deferred if Path(p).is_file()]
        if not valid:
            self.state.deferred.clear()
            self.save()
            return False
        self.state.pending.extend(p for p in valid if p not in self.state.pending)
        self.state.deferred.clear()
        self.state.current_index = 0
        self.save()
        return True

    def classify(self, category_index: int, collision: Literal["cancel", "skip", "rename"] = "cancel") -> MoveRecord | None:
        current = self.current
        if current is None:
            raise RuntimeError("没有待处理图片")
        category = self.state.categories[category_index]
        target_dir = Path(category.destination)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / current.name
        if target.exists():
            if collision == "cancel":
                raise FileExistsError(str(target))
            if collision == "skip":
                self.audit.write(original=current, category=category.name, destination=target, action="跳过", result="同名跳过", error="目标文件已存在")
                value = self.state.pending.pop(self.state.current_index)
                if value not in self.state.deferred:
                    self.state.deferred.append(value)
                if self.state.pending:
                    self.state.current_index %= len(self.state.pending)
                else:
                    self.state.current_index = 0
                self.save()
                return None
            target = unique_destination(target)
        try:
            original = current.resolve()
            shutil.move(str(original), str(target))
            record = MoveRecord(str(original), str(target.resolve()), category.name, original.name, target.name)
            self.state.history.append(record)
            self.state.pending.pop(self.state.current_index)
            self.state.deferred = [p for p in self.state.deferred if p != str(original)]
            if self.state.pending:
                self.state.current_index %= len(self.state.pending)
            else:
                self.state.current_index = 0
            self.audit.write(original=original, category=category.name, destination=target.resolve(), action="移动", result="成功", final_name=target.name)
            self.save()
            return record
        except Exception as exc:
            logging.exception("Move failed: %s -> %s", current, target)
            self.audit.write(original=current, category=category.name, destination=target, action="移动", result="失败", error=str(exc))
            raise

    def undo(self, collision: Literal["cancel", "rename"] = "cancel") -> MoveRecord:
        if not self.state.history:
            raise RuntimeError("没有可撤销的移动")
        record = self.state.history[-1]
        moved = Path(record.destination)
        if not moved.exists():
            raise FileNotFoundError(f"目标文件已不存在：{moved}")
        original = Path(record.original)
        restore_to = original
        if restore_to.exists():
            if collision == "cancel":
                raise FileExistsError(str(restore_to))
            restore_to = unique_destination(restore_to)
        try:
            shutil.move(str(moved), str(restore_to))
            self.state.history.pop()
            restored = str(restore_to.resolve())
            if restored not in self.state.pending:
                self.state.pending.insert(min(self.state.current_index, len(self.state.pending)), restored)
            self.audit.write(original=original, category=record.category, destination=restore_to.resolve(), action="撤销", result="成功", final_name=restore_to.name)
            self.save()
            return record
        except Exception as exc:
            logging.exception("Undo failed: %s -> %s", moved, restore_to)
            self.audit.write(original=original, category=record.category, destination=restore_to, action="撤销", result="失败", error=str(exc))
            raise
