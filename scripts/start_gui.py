"""本文件创建QApplication并启动MEA缺陷规律分析Qt主窗口。"""

import argparse
import json
import sys
import traceback
from pathlib import Path

PROJECT_ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd  # noqa: E402
import yaml  # noqa: E402
from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402
from gui.main_window import MainWindow  # noqa: E402
from src.analysis_service import (  # noqa: E402
    AnalysisRequest, resolve_image_path, run_analysis_task,
)


REQUIRED_ANALYSIS_FILES = (
    "extracted_defects.csv",
    "spatial_clusters.csv",
    "discovered_patterns.csv",
    "alerts.csv",
    "analysis_summary.json",
    "analysis_config_snapshot.yaml",
    "task_manifest.json",
)


def _write_report(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _start_review_validation(
    application: QApplication,
    window: MainWindow,
    products: pd.DataFrame,
    detections: pd.DataFrame,
    config: dict[str, object],
    report_path: Path,
    report: dict[str, object],
) -> None:
    window.tabs.setCurrentWidget(window.review)
    window.review.set_data(products, detections, config)
    attempts = {"count": 0}

    def finish_validation() -> None:
        attempts["count"] += 1
        payload = window.review._payload
        failed = "加载失败" in window.review.info.text()
        timed_out = attempts["count"] >= 300
        if payload is None and not failed and not timed_out:
            return
        scenes = {name: len(view.scene.items()) for name, view in window.review.views.items()}
        passed = payload is not None and not failed and all(count > 0 for count in scenes.values())
        report.update({
            "passed": bool(report.get("analysis_passed", True) and passed),
            "review_status": window.review.info.text(),
            "scene_item_counts": scenes,
        })
        _write_report(report_path, report)
        application.exit(0 if report["passed"] else 2)

    timer = QTimer(window)
    timer.timeout.connect(finish_validation)
    timer.start(100)
    window._validation_timer = timer


def main() -> int:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--validate-images", type=Path, help="加载产品CSV中的首组真实A/E图片并退出")
    parser.add_argument("--validate-analysis", type=Path, help="使用产品CSV执行真实小批量完整分析并退出")
    parser.add_argument("--image-root", type=Path, help="诊断模式下解析相对图片路径的根目录")
    parser.add_argument("--report", type=Path, help="诊断模式JSON报告路径")
    parser.add_argument("--validation-output", type=Path, help="完整分析诊断的任务输出父目录")
    args, qt_args = parser.parse_known_args(sys.argv[1:])
    application = QApplication([sys.argv[0], *qt_args])
    application.setApplicationName("MEA 5S 缺陷规律分析")
    application.setOrganizationName("Bosch")
    application.setOrganizationDomain("local.bosch")
    window = MainWindow(PROJECT_ROOT)
    window.show()
    if args.validate_analysis is not None:
        report_path = args.report or (args.validate_analysis.parent / "portable_analysis_validation.json")
        output_parent = (args.validation_output or report_path.parent / "portable_analysis_tasks").resolve()
        output_parent.mkdir(parents=True, exist_ok=True)
        try:
            source_products = pd.read_csv(args.validate_analysis)
            selected_orders = list(range(1, 25)) + [37, 61, 85, 109]
            products = source_products[
                source_products["global_order"].isin(selected_orders)
            ].copy()
            if len(products) != 28:
                products = source_products.head(28).copy()
            if len(products) != 28:
                raise ValueError(f"完整诊断需要28组产品，当前仅有{len(products)}组")
            subset_path = output_parent / "portable_validation_products.csv"
            products.to_csv(subset_path, index=False, encoding="utf-8-sig")
            config_path = PROJECT_ROOT / "config/analysis_config.yaml"
            result = run_analysis_task(AnalysisRequest(
                products_path=subset_path,
                config_path=config_path,
                output_parent=output_parent,
                task_name="便携版完整分析诊断",
                image_root=args.image_root,
            ))
            missing_files = [
                name for name in REQUIRED_ANALYSIS_FILES if not (result.output_dir / name).is_file()
            ]
            reloaded = {
                name: len(pd.read_csv(result.output_dir / name))
                for name in REQUIRED_ANALYSIS_FILES if name.endswith(".csv")
            }
            summary = json.loads(
                (result.output_dir / "analysis_summary.json").read_text(encoding="utf-8")
            )
            yaml.safe_load(
                (result.output_dir / "analysis_config_snapshot.yaml").read_text(encoding="utf-8")
            )
            json.loads((result.output_dir / "task_manifest.json").read_text(encoding="utf-8"))
            analysis_passed = (
                not missing_files
                and summary.get("analyzed_product_count") == 28
                and summary.get("extracted_defect_count", 0) >= 1
            )
            report: dict[str, object] = {
                "analysis_passed": analysis_passed,
                "source_csv": str(args.validate_analysis.resolve()),
                "subset_csv": str(subset_path),
                "output_dir": str(result.output_dir),
                "exit_expectation": 0,
                "analyzed_product_count": summary.get("analyzed_product_count"),
                "extracted_defect_count": summary.get("extracted_defect_count"),
                "discovered_pattern_count": summary.get("discovered_pattern_count"),
                "alert_count": summary.get("alert_count"),
                "missing_files": missing_files,
                "reloaded_csv_rows": reloaded,
            }
            _start_review_validation(
                application, window, result.frames["products"], result.frames["extracted"],
                yaml.safe_load(config_path.read_text(encoding="utf-8")), report_path, report,
            )
        except Exception as exc:
            _write_report(report_path, {
                "passed": False,
                "analysis_passed": False,
                "error": str(exc),
                "traceback": traceback.format_exc(),
            })
            return 2
        return application.exec()
    if args.validate_images is not None:
        products = pd.read_csv(args.validate_images).head(1).copy()
        if products.empty:
            raise ValueError("诊断产品CSV为空")
        source = "a_image_path" if "a_image_path" in products else "v_image_path"
        root = (args.image_root or args.validate_images.parent).resolve()
        for column in (source, "e_image_path"):
            products[column] = products[column].map(
                lambda value: str(resolve_image_path(
                    str(value), PROJECT_ROOT, root, args.validate_images
                ))
            )
        detections = pd.DataFrame(columns=[
            "global_order", "center_x", "center_y", "component_area", "cluster_id",
            "bbox_x1", "bbox_y1", "bbox_x2", "bbox_y2",
        ])
        config = yaml.safe_load(
            (PROJECT_ROOT / "config/analysis_config.yaml").read_text(encoding="utf-8")
        )
        report_path = args.report or (args.validate_images.parent / "portable_image_validation.json")
        _start_review_validation(
            application, window, products, detections, config, report_path, {
                "analysis_passed": True,
                "product_order": int(products.global_order.iloc[0]),
                "source_csv": str(args.validate_images.resolve()),
            },
        )
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
