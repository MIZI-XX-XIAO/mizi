"""本文件使用Edge和Selenium操作公司图片批量下载页面。"""

from __future__ import annotations

from pathlib import Path
from threading import Event
from typing import Callable, Sequence
import os
import shutil
import sys
import time


SITE_URL = "https://fuel-cell.apac.bosch.com/customize/download"


class EdgeImageSiteBackend:
    """通过真实网页完成认证、Excel上传、选项配置和ZIP下载。"""

    def __init__(
        self,
        project_root: Path,
        username: str = "",
        password: str = "",
        log: Callable[[str], None] | None = None,
        login_wait_seconds: int = 300,
        download_wait_seconds: int = 3600,
    ) -> None:
        self.project_root = project_root.resolve()
        self.username = username
        self.password = password
        self.log = log or (lambda _message: None)
        self.login_wait_seconds = login_wait_seconds
        self.download_wait_seconds = download_wait_seconds
        self.driver = None
        self._By = None
        self._WebDriverWait = None

    def _load_selenium(self):
        try:
            from selenium import webdriver
            from selenium.webdriver.common.by import By
            from selenium.webdriver.edge.options import Options
            from selenium.webdriver.edge.service import Service
            from selenium.webdriver.support.ui import WebDriverWait
        except ModuleNotFoundError as exc:
            raise RuntimeError("图片下载组件缺少 Selenium，请安装软件完整依赖包") from exc
        self._By = By
        self._WebDriverWait = WebDriverWait
        return webdriver, Options, Service

    def _driver_candidates(self) -> tuple[Path, ...]:
        frozen_root = Path(getattr(sys, "_MEIPASS", self.project_root))
        return (
            self.project_root / "image_downloader" / "msedgedriver.exe",
            self.project_root.parent / "image_downloader" / "msedgedriver.exe",
            frozen_root / "image_downloader" / "msedgedriver.exe",
        )

    def start(self, initial_download_dir: Path) -> None:
        if self.driver is not None:
            return
        webdriver, Options, Service = self._load_selenium()
        initial_download_dir.mkdir(parents=True, exist_ok=True)
        options = Options()
        options.add_argument("--inprivate")
        options.add_argument("--disable-notifications")
        options.add_argument("--disable-popup-blocking")
        options.add_experimental_option("prefs", {
            "download.default_directory": str(initial_download_dir.resolve()),
            "download.prompt_for_download": False,
            "download.directory_upgrade": True,
            "safebrowsing.enabled": True,
        })
        driver_path = next((path for path in self._driver_candidates() if path.is_file()), None)
        if driver_path is None:
            on_path = shutil.which("msedgedriver")
            driver_path = Path(on_path) if on_path else None
        try:
            service = Service(executable_path=str(driver_path)) if driver_path else Service()
            self.driver = webdriver.Edge(service=service, options=options)
        except Exception as exc:
            hint = (
                "请将与公司Edge主版本一致的msedgedriver.exe放入"
                " image_downloader 目录，或允许Selenium Manager获取驱动。"
            )
            raise RuntimeError(f"无法启动Microsoft Edge：{exc}\n{hint}") from exc
        self.driver.set_page_load_timeout(120)
        self.driver.get(SITE_URL)
        self._complete_login()

    def _find_first(self, selectors: Sequence[tuple[str, str]]):
        for by, value in selectors:
            try:
                elements = self.driver.find_elements(by, value)
                match = next((item for item in elements if item.is_displayed()), None)
                if match is not None:
                    return match
            except Exception:
                continue
        return None

    def _page_ready(self) -> bool:
        try:
            return bool(self.driver.find_elements(
                self._By.CSS_SELECTOR, ".batch-download-main, .batch-download-tabs"
            ))
        except Exception:
            return False

    def _complete_login(self) -> None:
        deadline = time.monotonic() + self.login_wait_seconds
        credentials_used = False
        self.log("等待图片网站登录；如出现Microsoft窗口，请在Edge中完成登录")
        while time.monotonic() < deadline:
            if self._page_ready():
                self.log("图片网站登录成功")
                return
            if not credentials_used:
                username = self._find_first((
                    (self._By.ID, "login_username"), (self._By.NAME, "login_username"),
                    (self._By.CSS_SELECTOR, "input[autocomplete='username']"),
                ))
                password = self._find_first((
                    (self._By.ID, "login_password"), (self._By.NAME, "login_password"),
                    (self._By.CSS_SELECTOR, "input[type='password']"),
                ))
                if username is not None and password is not None:
                    if not self.username or not self.password:
                        raise RuntimeError("网站显示LDAP登录，但本次任务未提供LDAP账号密码")
                    username.clear(); username.send_keys(self.username)
                    password.clear(); password.send_keys(self.password)
                    login_button = self._find_first((
                        (self._By.XPATH, "//button[contains(.,'登录') or contains(.,'Login')]"),
                        (self._By.CSS_SELECTOR, ".el-drawer button.el-button--primary"),
                    ))
                    if login_button is None:
                        raise RuntimeError("已找到LDAP输入框，但未找到登录按钮")
                    login_button.click()
                    credentials_used = True
                    self.log("已提交LDAP登录，等待页面授权")
            time.sleep(1)
        self.password = ""
        raise TimeoutError("等待图片网站登录超时，请确认公司网络、权限和登录状态")

    def _set_download_directory(self, download_dir: Path) -> None:
        download_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.driver.execute_cdp_cmd("Page.setDownloadBehavior", {
                "behavior": "allow", "downloadPath": str(download_dir.resolve()),
            })
        except Exception as exc:
            raise RuntimeError(f"无法设置Edge下载目录：{exc}") from exc

    def _wait_visible(self, by: str, selector: str, timeout: int = 30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            element = self._find_first(((by, selector),))
            if element is not None:
                return element
            time.sleep(0.25)
        raise TimeoutError(f"网页控件等待超时：{selector}")

    def _wait_present(self, by: str, selector: str, timeout: int = 30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                elements = self.driver.find_elements(by, selector)
                if elements:
                    return elements[0]
            except Exception:
                pass
            time.sleep(0.25)
        raise TimeoutError(f"网页控件不存在：{selector}")

    def _click_xpath(self, xpath: str) -> None:
        element = self._wait_visible(self._By.XPATH, xpath)
        self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", element)
        try:
            element.click()
        except Exception:
            self.driver.execute_script("arguments[0].click();", element)

    def _set_checkbox(self, label: str, checked: bool, container: str) -> None:
        xpath = (
            f"//div[contains(@class,'{container}')]//label[contains(@class,'el-checkbox')]"
            f"[.//span[contains(@class,'el-checkbox__label') and normalize-space(.)='{label}']]"
        )
        element = self._wait_visible(self._By.XPATH, xpath)
        current = "is-checked" in (element.get_attribute("class") or "")
        if current != checked:
            self.driver.execute_script("arguments[0].click();", element)

    def _configure_import_page(
        self, template_path: Path, image_codes: Sequence[str], quality: str, skip_rework: bool,
    ) -> None:
        self.driver.get(SITE_URL)
        if not self._page_ready():
            self._complete_login()
        self._click_xpath(
            "//div[contains(@class,'batch-download-tabs')]//div[contains(@class,'el-tabs__item')]"
            "[contains(.,'导入产品号列表批量下载')]"
        )
        upload = self._wait_present(
            self._By.CSS_SELECTOR,
            ".import-execl-for-download-excel input[type=file]",
        )
        self.driver.execute_script(
            "arguments[0].style.display='block'; arguments[0].style.visibility='visible';", upload,
        )
        upload.send_keys(str(template_path.resolve()))
        self._wait_visible(
            self._By.XPATH,
            "//div[contains(@class,'import-execl-for-download-options')]//*[contains(.,'一共')]",
            timeout=30,
        )
        selected = set(image_codes)
        for code in (
            "DA", "DB", "DC", "DE", "DX", "DY", "EA", "EB", "EC", "EE", "EX", "EY",
            "FA", "FB", "FC", "FE", "FX", "FY", "GA", "GB", "GC", "GE", "GX", "GY",
        ):
            self._set_checkbox(code, code in selected, "import-execl-for-download-options")
        self._set_checkbox("去除7层返工站", skip_rework, "import-execl-for-download-options")
        quality_label = "只选择原图" if quality == "origin" else "只选择压缩图"
        self._click_xpath(
            "//div[contains(@class,'import-execl-for-download-options')]"
            f"//label[contains(@class,'el-radio')][.//span[contains(.,'{quality_label}')]]"
        )

    def _wait_for_zip(self, download_dir: Path, before: set[Path], stop_event: Event) -> Path:
        deadline = time.monotonic() + self.download_wait_seconds
        stable_size = -1
        stable_polls = 0
        while time.monotonic() < deadline:
            if stop_event.is_set():
                raise InterruptedError("图片下载已取消")
            error = self._find_first(((self._By.CSS_SELECTOR, ".el-message--error, .el-message-box"),))
            if error is not None:
                text = error.text.strip() or "网站报告下载错误"
                raise RuntimeError(text)
            partials = list(download_dir.glob("*.crdownload"))
            candidates = [
                path for path in download_dir.glob("*.zip")
                if path.resolve() not in before and path.is_file()
            ]
            if candidates and not partials:
                newest = max(candidates, key=lambda path: path.stat().st_mtime)
                size = newest.stat().st_size
                stable_polls = stable_polls + 1 if size == stable_size else 0
                stable_size = size
                if size > 0 and stable_polls >= 2:
                    return newest
            time.sleep(1)
        raise TimeoutError("等待网站ZIP下载完成超时")

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
        if len(product_ids) > 100:
            raise ValueError("网站单批最多接受100个产品号")
        self.start(download_dir)
        self._set_download_directory(download_dir)
        before = {path.resolve() for path in download_dir.glob("*.zip")}
        self._configure_import_page(template_path, image_codes, quality, skip_rework)
        self._click_xpath(
            "//div[contains(@class,'import-execl-for-download-options')]"
            "//button[contains(@class,'el-button--primary') and contains(.,'开始下载')]"
        )
        return self._wait_for_zip(download_dir, before, stop_event)

    def close(self) -> None:
        self.password = ""
        if self.driver is not None:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None
