"""本文件生成轮廓提取、空间分组、周期规律和逐片预警的检查图表。"""

from pathlib import Path
from typing import Any

import cv2
import matplotlib
import numpy as np
import pandas as pd

from .contour_extractor import read_image

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402


def _save_figure(fig: Any, path: Path) -> None:
    fig.tight_layout(); fig.savefig(path, dpi=150); plt.close(fig)


def create_analysis_visualizations(products: pd.DataFrame, detections: pd.DataFrame, clusters: pd.DataFrame,
                                   patterns: pd.DataFrame, alerts: pd.DataFrame, project_root: Path,
                                   output_dir: Path, config: dict[str, Any]) -> None:
    vis_dir = output_dir / "visualizations"
    vis_dir.mkdir(parents=True, exist_ok=True)

    fig, ax = plt.subplots(figsize=(9, 5.5))
    if not detections.empty:
        for cluster_id, group in detections.groupby("cluster_id"):
            repeated = int((detections["cluster_id"] == cluster_id).sum()) >= int(config["minimum_repeat_occurrences"])
            ax.scatter(group.center_x_norm, group.center_y_norm, s=42 if repeated else 22,
                       alpha=0.85 if repeated else 0.45, label=cluster_id if repeated else None)
    ax.invert_yaxis(); ax.set_xlim(0, 1); ax.set_ylim(1, 0)
    ax.set_xlabel("normalized x"); ax.set_ylabel("normalized y"); ax.set_title("Automatically extracted spatial clusters")
    if ax.get_legend_handles_labels()[0]: ax.legend()
    _save_figure(fig, vis_dir / "detected_spatial_clusters.png")

    fig, ax = plt.subplots(figsize=(12, 4.5))
    if not detections.empty:
        cluster_order = {cluster_id: index + 1 for index, cluster_id in enumerate(sorted(detections.cluster_id.unique()))}
        y = detections.cluster_id.map(cluster_order)
        ax.scatter(detections.global_order, y, c=y, cmap="tab20", s=28)
        ax.set_yticks(list(cluster_order.values()), list(cluster_order.keys()))
    ax.set_xlabel("global_order"); ax.set_ylabel("spatial cluster"); ax.set_title("Detected defects over product order")
    _save_figure(fig, vis_dir / "cluster_timeline.png")

    fig, ax = plt.subplots(figsize=(12, 4.5))
    periodic = patterns[patterns.pattern_type == "periodic"] if not patterns.empty else patterns
    if not periodic.empty:
        row = periodic.sort_values("confidence", ascending=False).iloc[0]
        observed = [int(value) for value in str(row.observed_orders).split(";") if value]
        missing = [int(value) for value in str(row.inferred_missing_orders).split(";") if value and value != "nan"]
        ax.scatter(observed, np.ones(len(observed)), c="tab:red", label="observed", s=45)
        if missing: ax.scatter(missing, np.ones(len(missing)), c="black", marker="x", label="inferred missing", s=75)
        ax.set_title(f"Discovered periodic pattern: period={int(row.period)}, confidence={row.confidence:.3f}")
        ax.legend()
    else:
        ax.text(0.5, 0.5, "No periodic pattern discovered", ha="center", transform=ax.transAxes)
    ax.set_yticks([]); ax.set_xlim(1, int(products.global_order.max())); ax.set_xlabel("global_order")
    _save_figure(fig, vis_dir / "discovered_periodic_pattern.png")

    fig, ax = plt.subplots(figsize=(12, 4.5))
    if not alerts.empty:
        types = {name: index + 1 for index, name in enumerate(sorted(alerts.alert_type.unique()))}
        ax.scatter(alerts.alert_at_order, alerts.alert_type.map(types), c=alerts.alert_type.map(types), cmap="Set1", s=55)
        ax.set_yticks(list(types.values()), list(types.keys()))
    ax.set_xlabel("alert_at_order"); ax.set_title("Online replay warning events")
    _save_figure(fig, vis_dir / "warning_replay.png")

    candidates = detections.global_order.drop_duplicates().head(int(config["preview_count"])).tolist()
    if candidates:
        fig, axes = plt.subplots(len(candidates), 2, figsize=(12, 3.2 * len(candidates)), squeeze=False)
        lookup = products.set_index("global_order")
        for row_index, order in enumerate(candidates):
            product = lookup.loc[order]
            source_column = "a_image_path" if "a_image_path" in products.columns else "v_image_path"
            source_path = product[source_column]
            a_flag = cv2.IMREAD_GRAYSCALE if source_column == "a_image_path" else cv2.IMREAD_COLOR
            a_image = read_image(project_root / Path(source_path), a_flag)
            e_image = read_image(project_root / Path(product.e_image_path), cv2.IMREAD_COLOR)
            marked = e_image.copy()
            for detection in detections[detections.global_order == order].itertuples(index=False):
                cv2.rectangle(marked, (int(detection.bbox_x1), int(detection.bbox_y1)),
                              (int(detection.bbox_x2), int(detection.bbox_y2)), (0, 255, 0), 2)
            a_for_display = cv2.cvtColor(a_image, cv2.COLOR_GRAY2BGR) if a_image.ndim == 2 else a_image
            difference = cv2.absdiff(e_image, a_for_display)
            axes[row_index, 0].imshow(cv2.cvtColor(marked, cv2.COLOR_BGR2RGB)); axes[row_index, 0].set_title(f"#{order} extracted boxes")
            axes[row_index, 1].imshow(cv2.cvtColor(difference, cv2.COLOR_BGR2RGB)); axes[row_index, 1].set_title(f"#{order} E−V difference")
            axes[row_index, 0].axis("off"); axes[row_index, 1].axis("off")
        _save_figure(fig, vis_dir / "extraction_preview.png")
