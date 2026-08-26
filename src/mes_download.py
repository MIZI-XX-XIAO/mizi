"""本文件将既有 OIS/MES 自动化脚本封装为检测软件可调用的下载服务。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Event
from typing import Callable
import importlib
import shutil
import sys


LogCallback = Callable[[str], None]
ProgressCallback = Callable[[int, str], None]
QuestionCallback = Callable[[str, str, bool], bool]


@dataclass(frozen=True)
class MesDownloadRequest:
    begin_date: datetime
    end_date: datetime
    output_root: Path
    model: str = ""
    username: str = ""
    password: str = ""
    domain: str = "APAC"

    def validate(self) -> None:
        if self.end_date <= self.begin_date:
            raise ValueError("结束时间必须晚于开始时间")
        if self.model and len(self.model.strip()) != 10:
            raise ValueError("型号应为10位；如需查询全部型号请留空")
        if not str(self.output_root).strip():
            raise ValueError("请选择MES数据保存目录")


def _load_legacy_modules(project_root: Path):
    """延迟加载公司现有脚本，使不使用MES时无需安装Selenium。"""
    candidates = (
        project_root / "MES" / "OIS_Tool",
        project_root.parent / "MES" / "OIS_Tool",
        Path(getattr(sys, "_MEIPASS", project_root)) / "MES" / "OIS_Tool",
    )
    tool_dir = next((path for path in candidates if (path / "ois_automation.py").is_file()), None)
    if tool_dir is None:
        raise FileNotFoundError("未找到MES/OIS_Tool，请确认MES组件已随软件一起发布")
    tool_text = str(tool_dir.resolve())
    if tool_text not in sys.path:
        sys.path.insert(0, tool_text)
    try:
        config = importlib.import_module("config")
        excel_processor = importlib.import_module("excel_processor")
        automation = importlib.import_module("ois_automation")
    except ModuleNotFoundError as exc:
        if exc.name == "selenium":
            raise RuntimeError("MES下载组件缺少 Selenium，请安装软件完整依赖包") from exc
        raise
    return config, excel_processor, automation


def download_mes_workbook(
    project_root: Path,
    request: MesDownloadRequest,
    stop_event: Event | None = None,
    log: LogCallback | None = None,
    progress: ProgressCallback | None = None,
    question: QuestionCallback | None = None,
) -> Path:
    """执行全部MES查询和Excel整理，返回可直接分析的工作簿路径。"""
    request.validate()
    stop_event = stop_event or Event()
    log = log or (lambda _message: None)
    progress = progress or (lambda _value, _message: None)
    question = question or (lambda _title, _message, default: default)
    config, excel_processor, automation = _load_legacy_modules(project_root.resolve())

    output_root = request.output_root.expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    date_name = (
        request.begin_date.strftime("%Y%m%d_%H%M")
        + "-"
        + request.end_date.strftime("%Y%m%d_%H%M")
    )
    task_folder = output_root / date_name
    task_folder.mkdir(parents=True, exist_ok=True)
    excel_path = Path(excel_processor.create_failure_rate_excel(str(task_folder), date_name))
    download_dir = task_folder / "_downloads"
    download_dir.mkdir(parents=True, exist_ok=True)
    log(f"已创建工作簿：{excel_path}")

    def ask_continue(title: str, message: str) -> bool:
        return question(title, message, True)

    def ask_retry(title: str, message: str) -> bool:
        return question(title, message, False)

    ois = automation.OISAutomation(
        str(download_dir),
        log_callback=log,
        ask_continue_callback=ask_continue,
        error_callback=ask_retry,
        portal_username=request.username.strip(),
        portal_password=request.password,
        portal_domain=request.domain.strip() or "APAC",
    )
    ois.stop_event = stop_event
    try:
        progress(5, "正在启动Firefox并连接OIS Portal")
        ois.start_browser()
        ois.open_portal()
        if stop_event.is_set():
            raise InterruptedError("MES下载已取消")

        progress(15, "正在进入Quality Data / Traceability")
        ois.navigate_to_quality_data()
        results: dict[str, bool] = {}
        tasks = config.QUERY_TASKS
        for index, task in enumerate(tasks):
            if stop_event.is_set():
                raise InterruptedError("MES下载已取消")
            name = task["name"]
            progress(15 + int(index / max(1, len(tasks)) * 65), f"正在下载 {name}")
            try:
                results[name] = bool(ois.execute_query(
                    task, request.begin_date, request.end_date,
                    request.model.strip(), str(excel_path),
                ))
            except Exception as exc:
                results[name] = False
                log(f"{name} 查询失败：{exc}")
        failed = [name for name, ok in results.items() if not ok]
        if failed:
            log("未成功完成的查询：" + "、".join(failed))
        if stop_event.is_set():
            raise InterruptedError("MES下载已取消")

        progress(85, "正在整理Excel工作簿")
        excel_processor.process_all_sheets(str(excel_path))
        progress(95, "正在清理临时下载文件")
        shutil.rmtree(download_dir, ignore_errors=True)
        progress(100, "MES数据下载和整理完成")
        return excel_path.resolve()
    finally:
        try:
            ois.close(kill_browser=stop_event.is_set())
        except Exception:
            pass
