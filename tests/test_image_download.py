"""本文件验证图片网站下载的分批、坏数据隔离、ZIP分类和报告。"""

from pathlib import Path
from threading import Event
import zipfile

import cv2
import numpy as np
from openpyxl import Workbook
import pytest

from src.image_download import (
    ImageDownloadOrchestrator, ImageDownloadRequest, ProductIdSummary,
    create_product_template, extract_and_classify_archive, extract_product_ids,
)
from src.image_site_automation import EdgeImageSiteBackend


class FakeEdgeOptions:
    def __init__(self) -> None:
        self.arguments: list[str] = []
        self.experimental_options: dict[str, object] = {}

    def add_argument(self, value: str) -> None:
        self.arguments.append(value)

    def add_experimental_option(self, name: str, value: object) -> None:
        self.experimental_options[name] = value


class FakeLoginElement:
    def __init__(self) -> None:
        self.values: list[str] = []
        self.clicked = False

    def clear(self) -> None:
        self.values.clear()

    def send_keys(self, value: str) -> None:
        self.values.append(value)

    def click(self) -> None:
        self.clicked = True


def _product(index: int) -> str:
    return f"P{index:024d}"


def _tiff_bytes() -> bytes:
    image = np.full((12, 16), 128, dtype=np.uint8)
    ok, encoded = cv2.imencode(".tif", image)
    assert ok
    return encoded.tobytes()


class FakeBackend:
    def __init__(self, bad_product: str = "", bad_code: str = "") -> None:
        self.bad_product = bad_product
        self.bad_code = bad_code
        self.calls: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
        self.closed = False

    def download_batch(
        self, product_ids, image_codes, quality, skip_rework,
        template_path, download_dir, stop_event,
    ) -> Path:
        products, codes = tuple(product_ids), tuple(image_codes)
        self.calls.append((products, codes))
        if self.bad_product in products and (not self.bad_code or self.bad_code in codes):
            raise RuntimeError("one image unavailable")
        archive = download_dir / f"result_{len(self.calls)}.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            for product in products:
                for code in codes:
                    bundle.writestr(f"{product}20260826{code}.tif", _tiff_bytes())
        return archive

    def close(self) -> None:
        self.closed = True


class OmitOnceBackend(FakeBackend):
    def __init__(self, omitted_product: str, omitted_code: str) -> None:
        super().__init__(); self.omitted_product = omitted_product; self.omitted_code = omitted_code

    def download_batch(self, product_ids, image_codes, quality, skip_rework, template_path, download_dir, stop_event):
        products, codes = tuple(product_ids), tuple(image_codes)
        self.calls.append((products, codes))
        archive = download_dir / f"result_{len(self.calls)}.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            for product in products:
                for code in codes:
                    if len(self.calls) == 1 and product == self.omitted_product and code == self.omitted_code:
                        continue
                    bundle.writestr(f"{product}20260826{code}.tif", _tiff_bytes())
        return archive


def _request(tmp_path: Path, codes=("DA", "DE"), batch_size=80) -> ImageDownloadRequest:
    workbook = tmp_path / "source.xlsx"
    Workbook().save(workbook)
    return ImageDownloadRequest(workbook, tmp_path / "output", tuple(codes), "origin", batch_size=batch_size)


def test_product_template_has_ignored_first_row(tmp_path: Path) -> None:
    products = [_product(1), _product(2)]
    path = create_product_template(tmp_path / "template.xlsx", products)
    from openpyxl import load_workbook
    book = load_workbook(path, read_only=True)
    values = [row[0] for row in book.active.iter_rows(values_only=True)]
    book.close()
    assert values[1:] == products


def test_extract_product_ids_filters_and_keeps_production_order(tmp_path: Path) -> None:
    valid_a, valid_b = _product(1), _product(2)
    path = tmp_path / "mes.xlsx"
    book = Workbook(); sheet = book.active; sheet.title = "MS0310all"
    sheet.append(["Ident No.", "Test Date", "Line", "ST", "SI", "FU", "WP", "Result.Force"])
    sheet.append([valid_b, "2026-08-26 09:00:00", 3003, 10, 1, 1, 6, 1])
    sheet.append([valid_a, "2026-08-26 08:00:00", 3003, 10, 1, 1, 6, 1])
    sheet.append([valid_a, "2026-08-26 10:00:00", 3003, 10, 1, 1, 6, 1])
    sheet.append(["SHORT", "2026-08-26 11:00:00", 3003, 10, 1, 1, 6, 1])
    book.save(path); book.close()
    result = extract_product_ids(path)
    assert result.valid_ids == (valid_a, valid_b)
    assert result.invalid_ids == ("SHORT",)
    assert result.duplicate_count == 1


def test_82_products_are_split_into_80_and_2(tmp_path: Path) -> None:
    products = tuple(_product(index) for index in range(82))
    backend = FakeBackend()
    result = ImageDownloadOrchestrator(_request(tmp_path), backend).run(ProductIdSummary(products, (), 0))
    assert result.status == "complete"
    assert [len(call[0]) for call in backend.calls] == [80, 2]
    assert result.completed_item_count == 164
    assert backend.closed


def test_bad_product_and_code_are_isolated_while_good_images_survive(tmp_path: Path) -> None:
    products = tuple(_product(index) for index in range(8))
    bad = products[3]
    backend = FakeBackend(bad, "DE")
    result = ImageDownloadOrchestrator(
        _request(tmp_path, batch_size=10), backend,
    ).run(ProductIdSummary(products, (), 0))
    assert result.status == "partial"
    assert (result.output_dir / "images" / "5S" / f"{bad}20260826DA.tif").is_file()
    assert not (result.output_dir / "images" / "5S" / f"{bad}20260826DE.tif").exists()
    assert any(issue.product_id == bad and issue.image_code == "DE" for issue in result.issues)
    for product in products:
        assert (result.output_dir / "images" / "5S" / f"{product}20260826DA.tif").is_file()


def test_corrupt_tiff_is_quarantined(tmp_path: Path) -> None:
    product = _product(1)
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(f"{product}20260826DA.tif", b"not-a-tiff")
    outcome = extract_and_classify_archive(archive, tmp_path, [product], ["DA"])
    assert not outcome.found_items
    assert outcome.issues[0].category == "corrupt_tiff"
    assert any((tmp_path / "_quarantine").iterdir())


def test_resize_suffix_is_normalized_for_existing_image_index(tmp_path: Path) -> None:
    product = _product(1)
    archive = tmp_path / "resize.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(f"{product}20260826DA_resize.tif", _tiff_bytes())
    outcome = extract_and_classify_archive(archive, tmp_path, [product], ["DA"])
    assert outcome.found_items == {(product, "DA")}
    assert (tmp_path / "images" / "5S" / f"{product}20260826DA.tif").is_file()


def test_missing_image_in_successful_zip_is_retried_individually(tmp_path: Path) -> None:
    products = tuple(_product(index) for index in range(4))
    backend = OmitOnceBackend(products[2], "DE")
    result = ImageDownloadOrchestrator(
        _request(tmp_path, batch_size=10), backend,
    ).run(ProductIdSummary(products, (), 0))
    assert result.status == "complete"
    assert backend.calls[-1] == ((products[2],), ("DE",))
    assert result.completed_item_count == 8


def test_retry_manifest_only_downloads_failed_item(tmp_path: Path) -> None:
    products = tuple(_product(index) for index in range(4))
    bad = products[1]
    first = ImageDownloadOrchestrator(
        _request(tmp_path, batch_size=10), FakeBackend(bad, "DE"),
    ).run(ProductIdSummary(products, (), 0))
    retry_request = ImageDownloadRequest(
        first.manifest_path.parent.parent / "source.xlsx",
        first.output_dir, ("DA", "DE"), "origin", batch_size=10,
        retry_manifest=first.manifest_path,
    )
    backend = FakeBackend()
    retried = ImageDownloadOrchestrator(retry_request, backend).run(ProductIdSummary(products, (), 0))
    assert retried.status == "complete"
    assert backend.calls == [((bad,), ("DE",))]
    assert retried.product_count == 4
    assert retried.completed_item_count == 8


def test_product_limit_and_credentials_are_not_persisted(tmp_path: Path) -> None:
    request = _request(tmp_path)
    secret_request = ImageDownloadRequest(
        request.workbook_path, request.output_root, request.image_codes, request.quality,
        username="ldap-user", password="super-secret-password", batch_size=80,
    )
    result = ImageDownloadOrchestrator(secret_request, FakeBackend()).run(
        ProductIdSummary((_product(1),), (), 0)
    )
    manifest = result.manifest_path.read_text(encoding="utf-8")
    assert "ldap-user" not in manifest
    assert "super-secret-password" not in manifest

    too_large = ImageDownloadRequest(
        request.workbook_path, request.output_root, request.image_codes, request.quality,
        batch_size=101,
    )
    with pytest.raises(ValueError, match="10到100"):
        too_large.validate()
    backend = EdgeImageSiteBackend(tmp_path)
    with pytest.raises(ValueError, match="最多接受100"):
        backend.download_batch(
            [_product(index) for index in range(101)], ["DA"], "origin", True,
            tmp_path / "template.xlsx", tmp_path, Event(),
        )


def test_edge_options_use_dedicated_persistent_profile(tmp_path: Path) -> None:
    profile_dir = tmp_path / "edge-image-profile"
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    backend = EdgeImageSiteBackend(tmp_path, profile_dir=profile_dir)

    options = backend._build_options(FakeEdgeOptions, download_dir)

    assert "--inprivate" not in options.arguments
    assert f"--user-data-dir={profile_dir.resolve()}" in options.arguments
    assert "--profile-directory=Default" in options.arguments
    assert profile_dir.is_dir()
    assert options.experimental_options["prefs"]["download.default_directory"] == str(
        download_dir.resolve()
    )


def test_default_edge_profile_is_stable_and_application_owned(tmp_path: Path, monkeypatch) -> None:
    app_data = tmp_path / "MEA5SDefectAnalysis"
    monkeypatch.setattr("src.image_site_automation.user_data_dir", lambda: app_data)

    first = EdgeImageSiteBackend(tmp_path)
    second = EdgeImageSiteBackend(tmp_path)

    assert first.profile_dir == app_data / "edge-image-profile"
    assert second.profile_dir == first.profile_dir


def test_wia_login_is_reported_as_manual_and_times_out_clearly(tmp_path: Path, monkeypatch) -> None:
    messages: list[str] = []
    backend = EdgeImageSiteBackend(
        tmp_path, password="temporary-secret", log=messages.append,
        login_wait_seconds=1, profile_dir=tmp_path / "profile",
    )
    backend._page_ready = lambda: False
    backend._current_url = lambda: "https://stfs.bosch.com/adfs/ls/wia?wa=wsignin1.0"
    ticks = iter((0.0, 0.0, 2.0))
    monkeypatch.setattr("src.image_site_automation.time.monotonic", lambda: next(ticks))
    monkeypatch.setattr("src.image_site_automation.time.sleep", lambda _seconds: None)

    with pytest.raises(TimeoutError, match="Windows集成认证超时"):
        backend._complete_login()

    assert any("系统窗口" in message and "手动完成" in message for message in messages)
    assert backend.password == ""


def test_web_ldap_form_is_filled_and_login_completion_is_detected(tmp_path: Path, monkeypatch) -> None:
    messages: list[str] = []
    username = FakeLoginElement()
    password = FakeLoginElement()
    button = FakeLoginElement()
    backend = EdgeImageSiteBackend(
        tmp_path, username="ldap-user", password="temporary-secret",
        log=messages.append, profile_dir=tmp_path / "profile",
    )
    ready = iter((False, True))
    elements = iter((username, password, button))
    backend._page_ready = lambda: next(ready)
    backend._current_url = lambda: "https://fuel-cell.apac.bosch.com/login"
    backend._find_first = lambda _selectors: next(elements)
    backend._By = type(
        "FakeBy", (), {"ID": "id", "NAME": "name", "CSS_SELECTOR": "css", "XPATH": "xpath"},
    )
    monkeypatch.setattr("src.image_site_automation.time.sleep", lambda _seconds: None)

    backend._complete_login()

    assert username.values == ["ldap-user"]
    assert password.values == ["temporary-secret"]
    assert button.clicked
    assert any("网页LDAP登录表单" in message for message in messages)
    assert messages[-1] == "图片网站登录成功"
