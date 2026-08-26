"""本文件编排公司图片网站批量下载、坏数据隔离、解压分类和结果报告。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from threading import Event
from typing import Callable, Protocol, Sequence
import csv
import hashlib
import json
import shutil
import zipfile

import cv2
import numpy as np
from openpyxl import Workbook

from src.station_sources import parse_image_filename


ALL_IMAGE_CODES = (
    "DA", "DB", "DC", "DE", "DX", "DY",
    "EA", "EB", "EC", "EE", "EX", "EY",
    "FA", "FB", "FC", "FE", "FX", "FY",
    "GA", "GB", "GC", "GE", "GX", "GY",
)
PRIMARY_IMAGE_CODES = ("DA", "DE", "EA", "EE", "FA", "FE", "GA", "GE")
CODE_SCOPE = {"D": "5S", "E": "5X", "F": "7S", "G": "7X"}
MIN_FREE_BYTES = 2 * 1024 * 1024 * 1024


ProgressCallback = Callable[[int, str], None]
LogCallback = Callable[[str], None]


@dataclass(frozen=True)
class ProductIdSummary:
    valid_ids: tuple[str, ...]
    invalid_ids: tuple[str, ...]
    duplicate_count: int


@dataclass(frozen=True)
class ImageDownloadRequest:
    workbook_path: Path
    output_root: Path
    image_codes: tuple[str, ...]
    quality: str
    skip_rework: bool = True
    username: str = ""
    password: str = ""
    batch_size: int = 80
    retry_manifest: Path | None = None

    def validate(self) -> None:
        if not self.workbook_path.is_file():
            raise FileNotFoundError(f"MES工作簿不存在：{self.workbook_path}")
        if not self.image_codes:
            raise ValueError("请至少选择一个图片代码")
        unknown = sorted(set(self.image_codes) - set(ALL_IMAGE_CODES))
        if unknown:
            raise ValueError("未知图片代码：" + "、".join(unknown))
        if self.quality not in {"origin", "resize"}:
            raise ValueError("必须选择原图或压缩图")
        if not 10 <= self.batch_size <= 100:
            raise ValueError("每批产品号必须在10到100之间")


@dataclass
class ImageDownloadIssue:
    product_id: str
    image_code: str
    stage: str
    category: str
    message: str
    retry_count: int = 0
    status: str = "failed"


@dataclass
class ImageDownloadResult:
    status: str
    output_dir: Path
    image_roots: dict[str, Path]
    product_count: int
    completed_item_count: int
    issues: list[ImageDownloadIssue]
    manifest_path: Path
    summary_path: Path


class ImageSiteBackend(Protocol):
    def download_batch(
        self,
        product_ids: Sequence[str],
        image_codes: Sequence[str],
        quality: str,
        skip_rework: bool,
        template_path: Path,
        download_dir: Path,
        stop_event: Event,
    ) -> Path:
        """操作网站并返回下载完成的ZIP路径。"""

    def close(self) -> None:
        """关闭浏览器和驱动。"""


def extract_product_ids(workbook_path: Path) -> ProductIdSummary:
    """从MES工作簿的工站事件中按生产时间提取25位Ident No.。"""
    from src.station_sources import load_station_catalog
    from src.station_workbook import load_station_workbook

    project_root = Path(__file__).resolve().parents[1]
    catalog = load_station_catalog(project_root / "config" / "stations.yaml")
    workbook = load_station_workbook(workbook_path, catalog)
    events = workbook.events.copy()
    if "test_date" in events:
        events = events.sort_values("test_date", na_position="last", kind="stable")
    raw_values = [str(value or "").strip().upper() for value in events["dmc_raw"]]
    valid: list[str] = []
    invalid: list[str] = []
    seen: set[str] = set()
    duplicates = 0
    for value in raw_values:
        if len(value) != 25:
            if value and value not in invalid:
                invalid.append(value)
            continue
        if value in seen:
            duplicates += 1
            continue
        seen.add(value)
        valid.append(value)
    return ProductIdSummary(tuple(valid), tuple(invalid), duplicates)


def create_product_template(path: Path, product_ids: Sequence[str]) -> Path:
    """生成图片网站可读取的Sheet1产品号模板。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Sheet1"
    sheet.append(["第一行无效，产品编号从第二行开始"])
    for product_id in product_ids:
        sheet.append([product_id])
    workbook.save(path)
    workbook.close()
    return path


def _safe_member_name(name: str) -> str:
    normalized = name.replace("\\", "/")
    if normalized.startswith("/") or ".." in Path(normalized).parts:
        raise ValueError(f"ZIP包含不安全路径：{name}")
    return Path(normalized).name


def _valid_tiff(data: bytes) -> bool:
    if len(data) < 64 or data[:4] not in {b"II*\x00", b"MM\x00*"}:
        return False
    decoded = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    return decoded is not None and decoded.size > 0


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass
class _ArchiveOutcome:
    found_items: set[tuple[str, str]] = field(default_factory=set)
    issues: list[ImageDownloadIssue] = field(default_factory=list)
    extracted_count: int = 0


def extract_and_classify_archive(
    archive: Path,
    output_dir: Path,
    expected_products: Sequence[str],
    expected_codes: Sequence[str],
) -> _ArchiveOutcome:
    """校验ZIP和TIFF，并把图片安全分类到四个分析范围。"""
    expected_product_set = set(expected_products)
    expected_code_set = set(expected_codes)
    outcome = _ArchiveOutcome()
    quarantine = output_dir / "_quarantine"
    quarantine.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(archive) as bundle:
            broken = bundle.testzip()
            if broken:
                raise zipfile.BadZipFile(f"ZIP成员损坏：{broken}")
            for info in bundle.infolist():
                if info.is_dir():
                    continue
                filename = _safe_member_name(info.filename)
                data = bundle.read(info)
                suffix = Path(filename).suffix.lower()
                logical_filename = filename
                if Path(filename).stem.lower().endswith("_resize"):
                    logical_filename = Path(filename).stem[:-7] + Path(filename).suffix
                parsed = parse_image_filename(Path(logical_filename))
                if suffix not in {".tif", ".tiff"} or parsed is None:
                    target = quarantine / filename
                    target.write_bytes(data)
                    outcome.issues.append(ImageDownloadIssue(
                        "", "", "extract", "unrecognized_filename",
                        f"文件名无法识别：{filename}", status="quarantined",
                    ))
                    continue
                product_id, code = parsed.dmc_raw.upper(), parsed.image_code.upper()
                if product_id not in expected_product_set or code not in expected_code_set:
                    target = quarantine / filename
                    target.write_bytes(data)
                    outcome.issues.append(ImageDownloadIssue(
                        product_id, code, "extract", "unexpected_image",
                        f"ZIP包含未请求的图片：{filename}", status="quarantined",
                    ))
                    continue
                if not _valid_tiff(data):
                    target = quarantine / filename
                    target.write_bytes(data)
                    outcome.issues.append(ImageDownloadIssue(
                        product_id, code, "validate", "corrupt_tiff",
                        f"TIFF无法解码：{filename}", status="retryable",
                    ))
                    continue
                scope = CODE_SCOPE[code[0]]
                target_dir = output_dir / "images" / scope
                target_dir.mkdir(parents=True, exist_ok=True)
                # 压缩图的网站文件名带 _resize；分类时去掉该标记，以复用现有现场命名解析。
                target = target_dir / logical_filename
                if target.exists():
                    if _digest(target.read_bytes()) == _digest(data):
                        outcome.found_items.add((product_id, code))
                        continue
                    conflict = quarantine / f"duplicate_{filename}"
                    conflict.write_bytes(data)
                    outcome.issues.append(ImageDownloadIssue(
                        product_id, code, "extract", "duplicate_image",
                        f"同名图片内容不同：{filename}", status="quarantined",
                    ))
                    continue
                target.write_bytes(data)
                outcome.found_items.add((product_id, code))
                outcome.extracted_count += 1
    except zipfile.BadZipFile as exc:
        raise ValueError(f"下载文件不是有效ZIP：{archive.name}；{exc}") from exc
    return outcome


class ImageDownloadOrchestrator:
    def __init__(
        self,
        request: ImageDownloadRequest,
        backend: ImageSiteBackend,
        stop_event: Event | None = None,
        log: LogCallback | None = None,
        progress: ProgressCallback | None = None,
    ) -> None:
        self.request = request
        self.backend = backend
        self.stop_event = stop_event or Event()
        self.log = log or (lambda _message: None)
        self.progress = progress or (lambda _value, _message: None)
        self.issues: list[ImageDownloadIssue] = []
        self.completed_items: set[tuple[str, str]] = set()
        self._operation_index = 0
        self.output_dir = request.output_root
        self.temp_dir = self.output_dir / "_temporary_batches"
        self.manifest_path = self.output_dir / "image_download_manifest.json"
        self._all_product_ids: list[str] = []

    def _check_cancelled(self) -> None:
        if self.stop_event.is_set():
            raise InterruptedError("图片下载已取消")

    def _check_disk(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(self.output_dir).free < MIN_FREE_BYTES:
            raise OSError("图片保存目录剩余空间不足2 GB")

    def _record_manifest(self, product_ids: Sequence[str]) -> None:
        payload = {
            "version": 1,
            "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "workbook": str(self.request.workbook_path.resolve()),
            "quality": self.request.quality,
            "skip_rework": self.request.skip_rework,
            "image_codes": list(self.request.image_codes),
            "batch_size": self.request.batch_size,
            "product_ids": list(product_ids),
            "completed_items": [list(item) for item in sorted(self.completed_items)],
            "failed_items": [
                {"product_id": issue.product_id, "image_code": issue.image_code,
                 "category": issue.category, "message": issue.message}
                for issue in self.issues if issue.status == "failed" and issue.product_id
            ],
        }
        self.manifest_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
        )

    def _checkpoint(self) -> None:
        if self._all_product_ids:
            self._record_manifest(self._all_product_ids)

    def _attempt_download(
        self, product_ids: Sequence[str], image_codes: Sequence[str], retry_count: int,
    ) -> Path:
        self._check_cancelled()
        self._check_disk()
        self._operation_index += 1
        label = f"batch_{self._operation_index:04d}"
        batch_dir = self.temp_dir / label
        batch_dir.mkdir(parents=True, exist_ok=True)
        template = create_product_template(batch_dir / "products.xlsx", product_ids)
        self.log(
            f"{label}：{len(product_ids)}个产品，代码 {'+'.join(image_codes)}"
            + (f"，第{retry_count}次重试" if retry_count else "")
        )
        return self.backend.download_batch(
            product_ids, image_codes, self.request.quality, self.request.skip_rework,
            template, batch_dir, self.stop_event,
        )

    def _download_or_isolate(
        self, product_ids: Sequence[str], image_codes: Sequence[str], verify_retry: int = 0,
    ) -> None:
        self._check_cancelled()
        requested = {
            (product_id, code) for product_id in product_ids for code in image_codes
        } - self.completed_items
        if not requested:
            return
        active_products = [item for item in product_ids if any((item, c) in requested for c in image_codes)]
        active_codes = [code for code in image_codes if any((p, code) in requested for p in active_products)]
        last_error: Exception | None = None
        archive: Path | None = None
        for retry in range(3):
            try:
                archive = self._attempt_download(active_products, active_codes, retry)
                if not archive.is_file() or archive.stat().st_size == 0:
                    raise FileNotFoundError("网站未生成ZIP下载文件")
                break
            except InterruptedError:
                raise
            except Exception as exc:
                last_error = exc
                self.log(f"下载失败：{exc}")
        if archive is None:
            if len(active_products) > 1:
                middle = len(active_products) // 2
                self.log(f"自动拆分产品：{len(active_products)} → {middle}+{len(active_products)-middle}")
                self._download_or_isolate(active_products[:middle], active_codes, verify_retry)
                self._download_or_isolate(active_products[middle:], active_codes, verify_retry)
                return
            if len(active_codes) > 1:
                middle = len(active_codes) // 2
                self.log(f"隔离产品 {active_products[0]} 的图片代码")
                self._download_or_isolate(active_products, active_codes[:middle], verify_retry)
                self._download_or_isolate(active_products, active_codes[middle:], verify_retry)
                return
            self.issues.append(ImageDownloadIssue(
                active_products[0], active_codes[0], "download", "website_unavailable",
                str(last_error or "网站未返回文件"), retry_count=2, status="failed",
            ))
            self._checkpoint()
            return

        try:
            outcome = extract_and_classify_archive(
                archive, self.output_dir, active_products, active_codes,
            )
        except Exception as exc:
            self.log(f"ZIP校验失败，开始隔离：{exc}")
            if len(active_products) > 1:
                middle = len(active_products) // 2
                self._download_or_isolate(active_products[:middle], active_codes, verify_retry)
                self._download_or_isolate(active_products[middle:], active_codes, verify_retry)
            elif len(active_codes) > 1:
                middle = len(active_codes) // 2
                self._download_or_isolate(active_products, active_codes[:middle], verify_retry)
                self._download_or_isolate(active_products, active_codes[middle:], verify_retry)
            else:
                self.issues.append(ImageDownloadIssue(
                    active_products[0], active_codes[0], "extract", "invalid_zip",
                    str(exc), retry_count=2, status="failed",
                ))
                self._checkpoint()
            return

        self.completed_items.update(outcome.found_items)
        self.issues.extend(outcome.issues)
        self._checkpoint()
        missing = requested - outcome.found_items
        try:
            archive.unlink()
        except OSError:
            pass
        # A successful ZIP may still omit or corrupt one image. Retry those items alone.
        retryable = {
            (issue.product_id, issue.image_code)
            for issue in outcome.issues if issue.status == "retryable"
        }
        for product_id, code in sorted(missing | retryable):
            if (product_id, code) in self.completed_items:
                continue
            if verify_retry < 1:
                self._download_or_isolate([product_id], [code], verify_retry + 1)
            if (product_id, code) in self.completed_items:
                for issue in self.issues:
                    if issue.product_id == product_id and issue.image_code == code and issue.status == "retryable":
                        issue.status = "recovered"
                continue
            already_failed = any(
                issue.product_id == product_id and issue.image_code == code and issue.status == "failed"
                for issue in self.issues
            )
            if not already_failed:
                self.issues.append(ImageDownloadIssue(
                    product_id, code, "verify", "missing_image",
                    "ZIP中未找到可用的所选图片", retry_count=verify_retry + 1, status="failed",
                ))
        self._checkpoint()

    def _write_reports(self, product_ids: Sequence[str]) -> Path:
        issue_path = self.output_dir / "image_download_issues.csv"
        columns = ["product_id", "image_code", "stage", "category", "message", "retry_count", "status"]
        with issue_path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            for issue in self.issues:
                writer.writerow(asdict(issue))
        summary_path = self.output_dir / "image_download_summary.xlsx"
        workbook = Workbook()
        summary = workbook.active
        summary.title = "Summary"
        summary.append(["项目", "数量"])
        summary.append(["产品号", len(product_ids)])
        summary.append(["已下载产品-代码项", len(self.completed_items)])
        summary.append(["异常项", len([i for i in self.issues if i.status == "failed"])])
        detail = workbook.create_sheet("Issues")
        detail.append(columns)
        for issue in self.issues:
            detail.append([getattr(issue, column) for column in columns])
        workbook.save(summary_path)
        workbook.close()
        return summary_path

    def run(self, product_summary: ProductIdSummary | None = None) -> ImageDownloadResult:
        self.request.validate()
        summary = product_summary or extract_product_ids(self.request.workbook_path)
        all_product_ids = list(summary.valid_ids)
        self._all_product_ids = list(all_product_ids)
        product_ids = list(all_product_ids)
        if self.request.retry_manifest:
            payload = json.loads(self.request.retry_manifest.read_text(encoding="utf-8"))
            failed = payload.get("failed_items", [])
            retry_ids = {str(item.get("product_id", "")) for item in failed}
            product_ids = [item for item in product_ids if item in retry_ids]
            self.completed_items.update(tuple(item) for item in payload.get("completed_items", []))
        if not product_ids:
            raise ValueError("MES工作簿中没有可下载的25位Ident No.")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        for scope in CODE_SCOPE.values():
            (self.output_dir / "images" / scope).mkdir(parents=True, exist_ok=True)
        self._checkpoint()
        self.log(
            f"有效产品号 {len(product_ids)} 个，无效 {len(summary.invalid_ids)} 个，"
            f"重复记录 {summary.duplicate_count} 条"
        )
        total_batches = (len(product_ids) + self.request.batch_size - 1) // self.request.batch_size
        try:
            for index in range(0, len(product_ids), self.request.batch_size):
                self._check_cancelled()
                batch_number = index // self.request.batch_size + 1
                self.progress(
                    int((batch_number - 1) / max(1, total_batches) * 90),
                    f"正在处理第 {batch_number}/{total_batches} 批",
                )
                batch = product_ids[index:index + self.request.batch_size]
                self._download_or_isolate(batch, self.request.image_codes)
                self._record_manifest(all_product_ids)
            self.progress(94, "正在生成下载报告")
            for invalid in summary.invalid_ids:
                self.issues.append(ImageDownloadIssue(
                    invalid, "", "input", "invalid_product_id", "产品号不是25位", status="failed",
                ))
            self._record_manifest(all_product_ids)
            summary_path = self._write_reports(all_product_ids)
            failed_count = len([issue for issue in self.issues if issue.status == "failed"])
            status = "partial" if failed_count else "complete"
            self.progress(100, "图片下载完成" if status == "complete" else "图片下载部分完成")
            roots = {
                scope: self.output_dir / "images" / scope
                for scope in CODE_SCOPE.values()
                if any((self.output_dir / "images" / scope).iterdir())
            }
            return ImageDownloadResult(
                status, self.output_dir, roots, len(all_product_ids), len(self.completed_items),
                self.issues, self.manifest_path, summary_path,
            )
        finally:
            self.backend.close()


def default_image_download_dir(output_root: Path) -> Path:
    return output_root / f"image_download_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
