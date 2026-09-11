from __future__ import annotations

import csv
import json
import logging
import os
import re
import shutil
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}
VIEW_CODES = tuple("ABCEXYV")
STATE_VERSION = 2


def natural_key(value: Path | str) -> list[object]:
    name = value.name if isinstance(value, Path) else value
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", name)]


def scan_images(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    try:
        items = [p for p in folder.iterdir() if p.is_file() and p.suffix.casefold() in SUPPORTED_EXTENSIONS]
    except OSError:
        return []
    return sorted(items, key=natural_key)


def parse_product_view(path: Path) -> tuple[str, str] | None:
    stem = path.stem
    if len(stem) < 2 or stem[-1].upper() not in VIEW_CODES:
        return None
    return stem[:-1], stem[-1].upper()


@dataclass(frozen=True)
class Category:
    name: str
    destination: str


@dataclass
class ProductGroup:
    product_id: str
    review_image: str
    members: list[str]
    view_map: dict[str, list[str]]

    @property
    def duplicate_views(self) -> list[str]:
        return sorted(view for view, paths in self.view_map.items() if len(paths) > 1)

    def view_for(self, path: str) -> str:
        for view, paths in self.view_map.items():
            if path in paths:
                return view
        return ""

    @classmethod
    def from_dict(cls, data: dict) -> "ProductGroup":
        return cls(data["product_id"], data["review_image"], list(data.get("members", [])), {str(k): list(v) for k, v in data.get("view_map", {}).items()})


@dataclass
class ScanResult:
    groups: list[ProductGroup]
    image_count: int
    product_count: int
    missing_review_count: int
    ignored: list[str]
    view_counts: dict[str, int]
    duplicate_view_count: int


def scan_product_groups(folder: Path, review_view: str = "A") -> ScanResult:
    review_view = review_view.upper()
    raw: dict[str, dict] = {}
    ignored: list[str] = []
    view_counts = {view: 0 for view in VIEW_CODES}
    images = scan_images(folder)
    for path in images:
        parsed = parse_product_view(path)
        if parsed is None:
            ignored.append(str(path.resolve()))
            continue
        product_id, view = parsed
        bucket = raw.setdefault(product_id.casefold(), {"product_id": product_id, "views": {}})
        bucket["views"].setdefault(view, []).append(str(path.resolve()))
        view_counts[view] += 1
    groups: list[ProductGroup] = []
    missing = duplicates = 0
    for bucket in raw.values():
        views = {view: sorted(paths, key=natural_key) for view, paths in bucket["views"].items()}
        duplicates += sum(1 for paths in views.values() if len(paths) > 1)
        if review_view not in views:
            missing += 1
            continue
        members = sorted((path for paths in views.values() for path in paths), key=natural_key)
        groups.append(ProductGroup(bucket["product_id"], views[review_view][0], members, views))
    groups.sort(key=lambda item: natural_key(item.product_id))
    return ScanResult(groups, len(images), len(raw), missing, ignored, view_counts, duplicates)


@dataclass
class FileMove:
    original: str
    destination: str
    view: str
    name: str


@dataclass
class GroupMoveRecord:
    product_id: str
    category: str
    review_image: str
    review_view: str
    files: list[FileMove]
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    @classmethod
    def from_dict(cls, data: dict) -> "GroupMoveRecord":
        return cls(data["product_id"], data["category"], data["review_image"], data.get("review_view", ""), [FileMove(**item) for item in data.get("files", [])], data.get("timestamp", ""))


@dataclass
class SessionState:
    source: str
    categories: list[Category]
    pending: list[ProductGroup]
    review_view: str = "A"
    deferred: list[ProductGroup] = field(default_factory=list)
    history: list[GroupMoveRecord] = field(default_factory=list)
    total: int = 0
    current_index: int = 0
    ignored_count: int = 0
    missing_review_count: int = 0
    duplicate_view_count: int = 0
    failure_count: int = 0
    version: int = STATE_VERSION
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "SessionState":
        version = int(data.get("version", 1))
        categories = [Category(**item) for item in data.get("categories", [])]
        if version == 1:
            def legacy(path: str) -> ProductGroup:
                return ProductGroup(Path(path).stem, path, [path], {"": [path]})
            history = []
            for item in data.get("history", []):
                move = FileMove(item["original"], item["destination"], "", item.get("final_name", Path(item["destination"]).name))
                history.append(GroupMoveRecord(Path(item["original"]).stem, item["category"], item["original"], "", [move], item.get("timestamp", "")))
            return cls(data["source"], categories, [legacy(x) for x in data.get("pending", [])], "", [legacy(x) for x in data.get("deferred", [])], history, int(data.get("total", 0)), int(data.get("current_index", 0)))
        if version != STATE_VERSION:
            raise ValueError("任务状态版本不兼容")
        return cls(
            data["source"], categories, [ProductGroup.from_dict(x) for x in data.get("pending", [])], data.get("review_view", "A"),
            [ProductGroup.from_dict(x) for x in data.get("deferred", [])], [GroupMoveRecord.from_dict(x) for x in data.get("history", [])],
            int(data.get("total", 0)), int(data.get("current_index", 0)), int(data.get("ignored_count", 0)),
            int(data.get("missing_review_count", 0)), int(data.get("duplicate_view_count", 0)), int(data.get("failure_count", 0)), STATE_VERSION,
            data.get("session_id", uuid.uuid4().hex[:10]),
        )


def app_data_dir() -> Path:
    base = os.getenv("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    result = Path(base) / "ImageReviewClassifier"
    result.mkdir(parents=True, exist_ok=True)
    return result


def configure_logging() -> Path:
    target = app_data_dir() / "logs"
    target.mkdir(parents=True, exist_ok=True)
    log_file = target / "application.log"
    logging.basicConfig(filename=log_file, level=logging.INFO, encoding="utf-8", format="%(asctime)s %(levelname)s %(name)s %(message)s")
    return log_file


class StateStore:
    def __init__(self, path: Path | None = None):
        self.path = path or app_data_dir() / "active_session.json"

    def save(self, state: SessionState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(state.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, self.path)

    def load(self) -> SessionState | None:
        return SessionState.from_dict(json.loads(self.path.read_text(encoding="utf-8"))) if self.path.exists() else None

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


AUDIT_HEADERS = [
    "操作时间", "任务编号", "产品编号", "目检视角", "目检图片", "当前文件视角", "图片名称",
    "原始完整路径", "原始来源文件夹", "分类名称", "移动后完整路径", "动作类型", "操作结果", "失败原因",
]


@contextmanager
def _exclusive_log_lock(lock_path: Path, timeout: float = 3.0):
    deadline = time.monotonic() + timeout
    descriptor = None
    while descriptor is None:
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                if time.time() - lock_path.stat().st_mtime > 30:
                    lock_path.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise TimeoutError(f"记录文件正在被其他任务使用：{lock_path.parent}")
            time.sleep(0.05)
    try:
        yield
    finally:
        os.close(descriptor)
        lock_path.unlink(missing_ok=True)


class CategoryAuditLog:
    def __init__(self, destination: Path):
        self.destination = destination
        self.path = destination / "分类移动记录.csv"
        self.lock_path = destination / ".分类移动记录.lock"

    def write_rows(self, rows: list[list[str]]) -> None:
        if not rows:
            return
        self.destination.mkdir(parents=True, exist_ok=True)
        with _exclusive_log_lock(self.lock_path):
            new_file = not self.path.exists()
            with self.path.open("a", newline="", encoding="utf-8-sig") as handle:
                writer = csv.writer(handle)
                if new_file:
                    writer.writerow(AUDIT_HEADERS)
                writer.writerows(rows)
                handle.flush()
                os.fsync(handle.fileno())

    def rows_for(self, state: SessionState, group: ProductGroup, category: Category, moves: list[FileMove], action: str, result: str, error: str = "") -> list[list[str]]:
        now = datetime.now().isoformat(timespec="seconds")
        return [[
            now, state.session_id, group.product_id, state.review_view, Path(group.review_image).name,
            move.view, move.name, move.original, str(Path(move.original).parent), category.name,
            move.original if action in ("撤销", "回滚") else move.destination, action, result, error,
        ] for move in moves]


class GroupConflictError(RuntimeError):
    def __init__(self, title: str, paths: list[Path]):
        self.title, self.paths = title, paths
        preview = "\n".join(str(path) for path in paths[:6])
        if len(paths) > 6:
            preview += f"\n……另有 {len(paths) - 6} 项"
        super().__init__(f"{title}\n{preview}")


class GroupMoveError(RuntimeError):
    pass


def _view_map_from_moves(moves: list[FileMove], originals: bool) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for move in moves:
        result.setdefault(move.view, []).append(move.original if originals else move.destination)
    return result


class ReviewEngine:
    def __init__(self, state: SessionState, store: StateStore):
        self.state, self.store = state, store
        self.reconcile()

    @classmethod
    def create(cls, source: Path, categories: Iterable[Category], review_view: str, store: StateStore) -> "ReviewEngine":
        scan = scan_product_groups(source, review_view)
        state = SessionState(
            source=str(source.resolve()), categories=list(categories), pending=scan.groups, review_view=review_view.upper(),
            total=len(scan.groups), ignored_count=len(scan.ignored), missing_review_count=scan.missing_review_count,
            duplicate_view_count=scan.duplicate_view_count,
        )
        engine = cls(state, store)
        engine.save()
        return engine

    def reconcile(self) -> None:
        def valid(group: ProductGroup) -> bool:
            return bool(group.members) and all(Path(path).is_file() for path in group.members)
        self.state.pending = [group for group in self.state.pending if valid(group)]
        self.state.deferred = [group for group in self.state.deferred if valid(group)]
        self.state.current_index = min(max(0, self.state.current_index), max(0, len(self.state.pending) - 1))

    def save(self) -> None:
        self.store.save(self.state)

    @property
    def current(self) -> ProductGroup | None:
        return self.state.pending[self.state.current_index] if self.state.pending else None

    @property
    def completed_count(self) -> int:
        return len(self.state.history)

    @property
    def completed_image_count(self) -> int:
        return sum(len(record.files) for record in self.state.history)

    def navigate(self, delta: int) -> None:
        if self.state.pending:
            self.state.current_index = (self.state.current_index + delta) % len(self.state.pending)
            self.save()

    def defer_current(self) -> ProductGroup | None:
        group = self.current
        if group is None:
            return None
        value = self.state.pending.pop(self.state.current_index)
        if not any(item.product_id.casefold() == value.product_id.casefold() for item in self.state.deferred):
            self.state.deferred.append(value)
        self.state.current_index %= max(1, len(self.state.pending))
        self.save()
        return group

    def restore_deferred(self) -> bool:
        valid = [group for group in self.state.deferred if all(Path(path).is_file() for path in group.members)]
        if not valid:
            self.state.deferred.clear()
            self.save()
            return False
        existing = {group.product_id.casefold() for group in self.state.pending}
        self.state.pending.extend(group for group in valid if group.product_id.casefold() not in existing)
        self.state.deferred.clear()
        self.state.current_index = 0
        self.save()
        return True

    @staticmethod
    def _ensure_writable(directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / f".image-review-write-{uuid.uuid4().hex}.tmp"
        try:
            probe.touch(exist_ok=False)
        finally:
            probe.unlink(missing_ok=True)

    def _preflight_move(self, group: ProductGroup, destination: Path) -> list[tuple[Path, Path, str]]:
        missing = [Path(path) for path in group.members if not Path(path).is_file()]
        if missing:
            raise GroupConflictError("以下源文件不存在，整组未移动：", missing)
        self._ensure_writable(destination)
        plan = [(Path(path), destination / Path(path).name, group.view_for(path)) for path in group.members]
        conflicts = [target for _, target, _ in plan if target.exists()]
        if conflicts:
            raise GroupConflictError("目标目录存在同名文件，整组未移动：", conflicts)
        return plan

    def classify(self, category_index: int) -> GroupMoveRecord:
        group = self.current
        if group is None:
            raise RuntimeError("没有待处理产品")
        category = self.state.categories[category_index]
        target_dir = Path(category.destination)
        plan = self._preflight_move(group, target_dir)
        moved: list[FileMove] = []
        audit = CategoryAuditLog(target_dir)
        try:
            for source, target, view in plan:
                original = str(source.resolve())
                shutil.move(str(source), str(target))
                moved.append(FileMove(original, str(target.resolve()), view, source.name))
            audit.write_rows(audit.rows_for(self.state, group, category, moved, "整组移动", "成功"))
        except Exception as exc:
            rollback_errors: list[str] = []
            for move in reversed(moved):
                try:
                    if Path(move.destination).exists() and not Path(move.original).exists():
                        shutil.move(move.destination, move.original)
                except Exception as rollback_exc:
                    rollback_errors.append(f"{move.name}: {rollback_exc}")
            try:
                audit.write_rows(audit.rows_for(self.state, group, category, moved, "回滚", "失败" if rollback_errors else "成功", str(exc)))
            except Exception:
                logging.exception("Could not write rollback audit")
            logging.exception("Group move failed for %s", group.product_id)
            self.state.failure_count += 1
            self.save()
            detail = f"整组移动失败，已回滚：{exc}"
            if rollback_errors:
                detail += "\n部分文件回滚失败：\n" + "\n".join(rollback_errors)
            raise GroupMoveError(detail) from exc

        record = GroupMoveRecord(group.product_id, category.name, group.review_image, self.state.review_view, moved)
        self.state.history.append(record)
        self.state.pending.pop(self.state.current_index)
        self.state.deferred = [item for item in self.state.deferred if item.product_id.casefold() != group.product_id.casefold()]
        self.state.current_index %= max(1, len(self.state.pending))
        self.save()
        return record

    def undo(self) -> GroupMoveRecord:
        if not self.state.history:
            raise RuntimeError("没有可撤销的产品")
        record = self.state.history[-1]
        missing = [Path(move.destination) for move in record.files if not Path(move.destination).is_file()]
        conflicts = [Path(move.original) for move in record.files if Path(move.original).exists()]
        if missing:
            raise GroupConflictError("分类目录中的文件已不存在，无法整组撤销：", missing)
        if conflicts:
            raise GroupConflictError("原位置存在同名文件，整组未撤销：", conflicts)
        fallback = str(Path(record.files[0].destination).parent)
        category = next((item for item in self.state.categories if item.name == record.category), Category(record.category, fallback))
        audit = CategoryAuditLog(Path(category.destination))
        restored: list[FileMove] = []
        group = ProductGroup(record.product_id, record.review_image, [move.original for move in record.files], _view_map_from_moves(record.files, True))
        try:
            for move in reversed(record.files):
                shutil.move(move.destination, move.original)
                restored.append(move)
            audit.write_rows(audit.rows_for(self.state, group, category, record.files, "撤销", "成功"))
        except Exception as exc:
            rollback_errors: list[str] = []
            for move in restored:
                try:
                    if Path(move.original).exists() and not Path(move.destination).exists():
                        shutil.move(move.original, move.destination)
                except Exception as rollback_exc:
                    rollback_errors.append(f"{move.name}: {rollback_exc}")
            logging.exception("Group undo failed for %s", record.product_id)
            self.state.failure_count += 1
            self.save()
            detail = f"整组撤销失败，已恢复撤销前状态：{exc}"
            if rollback_errors:
                detail += "\n部分文件恢复失败：\n" + "\n".join(rollback_errors)
            raise GroupMoveError(detail) from exc
        self.state.history.pop()
        self.state.pending.insert(min(self.state.current_index, len(self.state.pending)), group)
        self.save()
        return record

    def category_summary(self) -> dict[str, tuple[int, int]]:
        result = {}
        for category in self.state.categories:
            records = [record for record in self.state.history if record.category == category.name]
            result[category.name] = (len(records), sum(len(record.files) for record in records))
        return result
