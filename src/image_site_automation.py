"""本文件使用Edge和Selenium操作公司图片批量下载页面。"""

from __future__ import annotations

from pathlib import Path
from threading import Event
from typing import Callable, Sequence
import re
import shutil
import sys
import time
from urllib.parse import urlsplit

from src.app_runtime import user_data_dir


SITE_URL = "https://fuel-cell.apac.bosch.com/customize/download"
EDGE_PROFILE_DIRNAME = "edge-image-profile"


class EdgeImageSiteBackend:
    """通过真实网页完成认证、DMC填写、选项配置和ZIP下载。"""

    def __init__(
        self,
        project_root: Path,
        username: str = "",
        password: str = "",
        log: Callable[[str], None] | None = None,
        login_wait_seconds: int = 300,
        download_wait_seconds: int = 3600,
        profile_dir: Path | None = None,
    ) -> None:
        self.project_root = project_root.resolve()
        self.username = username
        self.password = password
        self.log = log or (lambda _message: None)
        self.login_wait_seconds = login_wait_seconds
        self.download_wait_seconds = download_wait_seconds
        self.profile_dir = (profile_dir or user_data_dir() / EDGE_PROFILE_DIRNAME).resolve()
        self.driver = None
        self._By = None
        self._WebDriverWait = None
        self._TimeoutException = None
        self._active_download_pane = None

    def _load_selenium(self):
        try:
            from selenium import webdriver
            from selenium.webdriver.common.by import By
            from selenium.webdriver.edge.options import Options
            from selenium.webdriver.edge.service import Service
            from selenium.common.exceptions import TimeoutException
            from selenium.webdriver.support.ui import WebDriverWait
        except ModuleNotFoundError as exc:
            raise RuntimeError("图片下载组件缺少 Selenium，请安装软件完整依赖包") from exc
        self._By = By
        self._WebDriverWait = WebDriverWait
        self._TimeoutException = TimeoutException
        return webdriver, Options, Service

    def _build_options(self, options_class, initial_download_dir: Path):
        """创建使用应用专用持久配置的普通Edge选项。"""
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        options = options_class()
        options.add_argument(f"--user-data-dir={self.profile_dir}")
        options.add_argument("--profile-directory=Default")
        options.add_argument("--disable-notifications")
        options.add_argument("--disable-popup-blocking")
        options.add_experimental_option("prefs", {
            "download.default_directory": str(initial_download_dir.resolve()),
            "download.prompt_for_download": False,
            "download.directory_upgrade": True,
            "safebrowsing.enabled": True,
        })
        return options

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
        options = self._build_options(Options, initial_download_dir)
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
        self.log(f"使用软件专用Edge登录配置：{self.profile_dir}")
        try:
            self.driver.get(SITE_URL)
        except self._TimeoutException:
            # ADFS/WIA的系统级凭据框可能让导航一直处于未完成状态；
            # 浏览器仍可由用户操作，因此转入统一的登录等待流程。
            self.log("图片网站导航仍在等待认证，请在Edge中完成公司登录")
        self._complete_login()

    def _find_first(self, selectors: Sequence[tuple[str, str]]):
        return self._find_first_within(self.driver, selectors)

    @staticmethod
    def _find_first_within(root, selectors: Sequence[tuple[str, str]]):
        for by, value in selectors:
            try:
                elements = root.find_elements(by, value)
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

    def _current_url(self) -> str:
        try:
            return str(self.driver.current_url or "")
        except Exception:
            return ""

    def _safe_current_url(self) -> str:
        """返回不含查询参数和认证片段的当前地址，供任务日志诊断。"""
        current = self._current_url()
        try:
            parsed = urlsplit(current)
            return f"{parsed.scheme}://{parsed.netloc}{parsed.path}" if parsed.netloc else current
        except Exception:
            return current

    @staticmethod
    def _is_wia_url(url: str) -> bool:
        normalized = url.lower()
        return "/adfs/ls/wia" in normalized or (
            "stfs.bosch.com" in normalized and "/adfs/" in normalized
        )

    def _complete_login(self) -> None:
        deadline = time.monotonic() + self.login_wait_seconds
        credentials_used = False
        last_stage = ""
        self.log("等待图片网站登录；首次使用时请在Edge中完成公司认证")
        while time.monotonic() < deadline:
            if self._page_ready():
                self.log("图片网站登录成功")
                return
            current_url = self._current_url()
            if self._is_wia_url(current_url):
                if last_stage != "wia":
                    self.log(
                        "检测到ADFS/Windows集成认证。Windows Security是系统窗口，"
                        "请在Edge中手动完成；程序将在认证成功后自动继续"
                    )
                    last_stage = "wia"
                time.sleep(1)
                continue
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
                    if last_stage != "ldap":
                        self.log("检测到网页LDAP登录表单")
                        last_stage = "ldap"
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
                elif last_stage != "redirect":
                    self.log("等待公司认证页面跳转或用户完成登录")
                    last_stage = "redirect"
            time.sleep(1)
        self.password = ""
        if last_stage == "wia":
            raise TimeoutError(
                "等待ADFS/Windows集成认证超时。请确认公司网络和账号权限；"
                "若Windows Security反复弹出，说明公司策略未接受当前浏览器会话"
            )
        raise TimeoutError("等待图片网站登录超时，请确认公司网络、账号权限和登录状态")

    def _set_download_directory(self, download_dir: Path) -> None:
        download_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.driver.execute_cdp_cmd("Page.setDownloadBehavior", {
                "behavior": "allow", "downloadPath": str(download_dir.resolve()),
            })
        except Exception as exc:
            raise RuntimeError(f"无法设置Edge下载目录：{exc}") from exc

    def _wait_visible(self, by: str, selector: str, timeout: int = 30, root=None):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            element = self._find_first_within(root or self.driver, ((by, selector),))
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

    def _click_xpath(self, xpath: str, root=None) -> None:
        element = self._wait_visible(self._By.XPATH, xpath, root=root)
        self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", element)
        try:
            element.click()
        except Exception:
            self.driver.execute_script("arguments[0].click();", element)

    def _set_checkbox(self, label: str, checked: bool, root=None) -> None:
        xpath = (
            "//label[contains(@class,'el-checkbox')]"
            f"[.//span[contains(@class,'el-checkbox__label') and normalize-space(.)='{label}']]"
        )
        element = self._wait_visible(self._By.XPATH, xpath, root=root)
        current = "is-checked" in (element.get_attribute("class") or "")
        if current != checked:
            self.driver.execute_script("arguments[0].click();", element)

    @staticmethod
    def _recognized_count(text: str) -> int | None:
        match = re.search(r"(?:一共|共)\D{0,12}(\d+)\s*(?:个|条)?", text or "")
        return int(match.group(1)) if match else None

    def _wait_recognized_count(self, expected: int, timeout: int = 30) -> int:
        deadline = time.monotonic() + timeout
        last_count: int | None = None
        while time.monotonic() < deadline:
            try:
                elements = self.driver.find_elements(
                    self._By.XPATH,
                    "//*[(contains(normalize-space(.),'一共') or starts-with(normalize-space(.),'共'))"
                    " and string-length(normalize-space(.)) < 120]",
                )
                for element in elements:
                    if not element.is_displayed():
                        continue
                    count = self._recognized_count(element.text)
                    if count is not None:
                        last_count = count
                        if count == expected:
                            return count
            except Exception:
                pass
            time.sleep(0.25)
        if last_count is not None:
            raise RuntimeError(f"DMC解析数量不一致：提交{expected}个，网站识别{last_count}个")
        raise TimeoutError(f"网站未显示DMC识别数量；本批提交{expected}个")

    def _configure_direct_page(
        self, product_ids: Sequence[str], image_codes: Sequence[str], quality: str, skip_rework: bool,
    ) -> None:
        self.driver.get(SITE_URL)
        if not self._page_ready():
            self._complete_login()
        try:
            self._click_xpath(
                "//div[contains(@class,'batch-download-tabs')]//div[contains(@class,'el-tabs__item')]"
                "[(contains(.,'产品号') or contains(.,'DMC') or contains(.,'批量'))"
                " and not(contains(.,'导入')) and not(contains(.,'Excel'))]"
            )
        except TimeoutError as exc:
            raise RuntimeError("网站直接填写DMC入口不可用：未找到直接输入页签") from exc
        try:
            textarea = self._wait_visible(
                self._By.CSS_SELECTOR,
                ".batch-download-main textarea, .el-tabs__content textarea, textarea",
            )
        except TimeoutError as exc:
            raise RuntimeError("网站直接填写DMC入口不可用：未找到可见的产品号文本框") from exc
        pane = self.driver.execute_script(
            "return arguments[0].closest('.el-tab-pane') || "
            "arguments[0].closest('.batch-download-main');",
            textarea,
        ) or self.driver
        self._active_download_pane = pane
        payload = "\n".join(product_ids)
        textarea.clear()
        textarea.send_keys(payload)
        self.driver.execute_script(
            "arguments[0].dispatchEvent(new Event('input',{bubbles:true}));"
            "arguments[0].dispatchEvent(new Event('change',{bubbles:true}));"
            "arguments[0].blur();",
            textarea,
        )
        recognized = self._wait_recognized_count(len(product_ids))
        self.log(
            f"直接填写DMC：提交{len(product_ids)}个，网站识别{recognized}个；"
            f"页面 {self._safe_current_url()}"
        )
        selected = set(image_codes)
        for code in (
            "DA", "DB", "DC", "DE", "DX", "DY", "EA", "EB", "EC", "EE", "EX", "EY",
            "FA", "FB", "FC", "FE", "FX", "FY", "GA", "GB", "GC", "GE", "GX", "GY",
        ):
            self._set_checkbox(code, code in selected, root=pane)
        self._set_checkbox("去除7层返工站", skip_rework, root=pane)
        quality_label = "只选择原图" if quality == "origin" else "只选择压缩图"
        self._click_xpath(
            f".//label[contains(@class,'el-radio')][.//span[contains(.,'{quality_label}')]]",
            root=pane,
        )

    @staticmethod
    def _is_error_message(text: str) -> bool:
        normalized = (text or "").strip().lower()
        return any(token in normalized for token in (
            "错误", "失败", "请重试", "异常", "error", "failed", "failure",
        ))

    def _website_error_text(self) -> str:
        error = self._find_first(((self._By.CSS_SELECTOR, ".el-message--error"),))
        if error is not None:
            return error.text.strip() or "网站报告下载错误"
        try:
            dialogs = self.driver.find_elements(self._By.CSS_SELECTOR, ".el-message-box")
            for dialog in dialogs:
                if dialog.is_displayed() and self._is_error_message(dialog.text):
                    return dialog.text.strip()
        except Exception:
            pass
        return ""

    def _confirm_neutral_dialog(self) -> None:
        try:
            dialogs = self.driver.find_elements(self._By.CSS_SELECTOR, ".el-message-box")
            for dialog in dialogs:
                if not dialog.is_displayed() or self._is_error_message(dialog.text):
                    continue
                buttons = dialog.find_elements(
                    self._By.XPATH,
                    ".//button[contains(@class,'el-button--primary')]"
                    "[contains(.,'确定') or contains(.,'继续') or contains(.,'下载')]",
                )
                button = next((item for item in buttons if item.is_displayed()), None)
                if button is not None:
                    button.click()
                    return
        except Exception:
            pass

    def _wait_for_zip(self, download_dir: Path, before: set[Path], stop_event: Event) -> Path:
        deadline = time.monotonic() + self.download_wait_seconds
        stable_size = -1
        stable_polls = 0
        while time.monotonic() < deadline:
            if stop_event.is_set():
                raise InterruptedError("图片下载已取消")
            error_text = self._website_error_text()
            if error_text:
                self.log(f"网站下载错误：{error_text}；页面 {self._safe_current_url()}")
                raise RuntimeError(error_text)
            self._confirm_neutral_dialog()
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
                    self.log(f"ZIP下载完成：{newest.name}（{size} bytes）")
                    return newest
            time.sleep(1)
        self.log(f"等待ZIP下载超时：页面 {self._safe_current_url()}；未检测到完整ZIP")
        raise TimeoutError("等待网站ZIP下载完成超时")

    def download_batch(
        self,
        product_ids: Sequence[str],
        image_codes: Sequence[str],
        quality: str,
        skip_rework: bool,
        download_dir: Path,
        stop_event: Event,
    ) -> Path:
        if len(product_ids) > 100:
            raise ValueError("网站单批最多接受100个产品号")
        self.start(download_dir)
        self._set_download_directory(download_dir)
        before = {path.resolve() for path in download_dir.glob("*.zip")}
        self._configure_direct_page(product_ids, image_codes, quality, skip_rework)
        self._click_xpath(
            ".//button[contains(@class,'el-button--primary') and contains(.,'开始下载')]",
            root=self._active_download_pane,
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
