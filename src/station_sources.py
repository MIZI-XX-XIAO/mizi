"""本文件统一工站定义、现场图片文件名解析与运行时产品索引生成。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
import re

import pandas as pd
import yaml


SUPPORTED_IMAGE_SUFFIXES = {".bmp", ".jpeg", ".jpg", ".png", ".tif", ".tiff"}
IMAGE_NAME_PATTERN = re.compile(
    r"^(?P<dmc>.+)(?P<date>\d{8})(?P<family>[DEFG])(?P<view>[A-Z])$",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True)
class ImageProfile:
    name: str
    family_code: str
    views: tuple[str, ...]
    primary_raw_view: str
    primary_result_view: str

    @property
    def primary_raw_code(self) -> str:
        return f"{self.family_code}{self.primary_raw_view}"

    @property
    def primary_result_code(self) -> str:
        return f"{self.family_code}{self.primary_result_view}"


@dataclass(frozen=True)
class StationDefinition:
    id: str
    group: str
    name: str
    location: str
    station_type: str
    image_profile: str
    excel_profile: str

    @property
    def display_name(self) -> str:
        return f"[{self.group}] {self.name}  ({self.location})"


@dataclass(frozen=True)
class StationCatalog:
    stations: tuple[StationDefinition, ...]
    image_profiles: dict[str, ImageProfile]

    def station(self, station_id: str) -> StationDefinition:
        match = next((item for item in self.stations if item.id == station_id), None)
        if match is None:
            raise KeyError(f"未知工站：{station_id}")
        return match

    def station_for_location(self, location: Any) -> StationDefinition | None:
        normalized = str(location or "").strip()
        return next((item for item in self.stations if item.location == normalized), None)


@dataclass(frozen=True)
class ParsedImageName:
    path: Path
    dmc_raw: str
    capture_date: pd.Timestamp
    family_code: str
    view: str

    @property
    def image_code(self) -> str:
        return f"{self.family_code}{self.view}"


@dataclass
class ImageIndexResult:
    products: pd.DataFrame
    issues: pd.DataFrame
    scanned_file_count: int


def load_station_catalog(path: Path) -> StationCatalog:
    """读取并校验工站目录，防止重复编号或重复Location造成串站。"""
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    profiles: dict[str, ImageProfile] = {}
    for name, values in (payload.get("image_profiles") or {}).items():
        pair = tuple(str(value).upper() for value in values.get("primary_pair", ()))
        if len(pair) != 2:
            raise ValueError(f"图片档 {name} 必须定义两个主图视图")
        profiles[str(name)] = ImageProfile(
            name=str(name),
            family_code=str(values["family_code"]).upper(),
            views=tuple(str(value).upper() for value in values.get("views", ())),
            primary_raw_view=pair[0],
            primary_result_view=pair[1],
        )
    stations = tuple(StationDefinition(**values) for values in payload.get("stations", ()))
    ids = [item.id for item in stations]
    locations = [item.location for item in stations]
    if len(stations) != 18:
        raise ValueError(f"工站目录应包含18个工站，实际为 {len(stations)}")
    if len(ids) != len(set(ids)):
        raise ValueError("工站ID存在重复")
    if len(locations) != len(set(locations)):
        raise ValueError("工站Location存在重复")
    missing_profiles = sorted({item.image_profile for item in stations} - set(profiles))
    if missing_profiles:
        raise ValueError(f"工站引用了未定义的图片档：{missing_profiles}")
    return StationCatalog(stations, profiles)


def parse_image_filename(path: Path) -> ParsedImageName | None:
    """按 Ident No.+YYYYMMDD+产品族/视图代码 解析现场图片名。"""
    if path.suffix.lower() not in SUPPORTED_IMAGE_SUFFIXES:
        return None
    match = IMAGE_NAME_PATTERN.fullmatch(path.stem.strip())
    if match is None:
        return None
    try:
        capture_date = pd.Timestamp(datetime.strptime(match.group("date"), "%Y%m%d").date())
    except ValueError:
        return None
    return ParsedImageName(
        path=path.resolve(),
        dmc_raw=match.group("dmc").strip(),
        capture_date=capture_date,
        family_code=match.group("family").upper(),
        view=match.group("view").upper(),
    )


def build_image_product_index(
    image_root: Path,
    station: StationDefinition,
    catalog: StationCatalog,
    excel_dmcs: list[str] | None = None,
) -> ImageIndexResult:
    """扫描独立图片根目录，并生成可替代products.csv的运行时索引。"""
    root = image_root.resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"图片根目录不存在：{root}")
    profile = catalog.image_profiles[station.image_profile]
    parsed: list[ParsedImageName] = []
    issue_rows: list[dict[str, Any]] = []
    scanned = 0
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if path.suffix.lower() not in SUPPORTED_IMAGE_SUFFIXES:
            continue
        scanned += 1
        item = parse_image_filename(path)
        if item is None:
            issue_rows.append({"级别": "警告", "DMC": "", "文件": str(path), "问题": "文件名不符合现场规则"})
            continue
        if item.family_code != profile.family_code:
            issue_rows.append({
                "级别": "警告", "DMC": item.dmc_raw, "文件": str(path),
                "问题": f"图片属于产品族 {item.family_code}，与工站 {station.image_profile} 不一致",
            })
            continue
        if item.view not in profile.views:
            issue_rows.append({
                "级别": "警告", "DMC": item.dmc_raw, "文件": str(path),
                "问题": f"未配置视图 {item.image_code}",
            })
            continue
        parsed.append(item)

    by_dmc: dict[str, list[ParsedImageName]] = {}
    for item in parsed:
        by_dmc.setdefault(item.dmc_raw, []).append(item)
    expected_dmcs = [str(value).strip() for value in (excel_dmcs or []) if str(value).strip()]
    all_dmcs = list(dict.fromkeys(expected_dmcs + sorted(by_dmc)))
    rows: list[dict[str, Any]] = []
    for dmc in all_dmcs:
        images = by_dmc.get(dmc, [])
        by_code: dict[str, list[ParsedImageName]] = {}
        for item in images:
            by_code.setdefault(item.image_code, []).append(item)
        row: dict[str, Any] = {
            "dmc_raw": dmc,
            "order_code": dmc,
            "camera": profile.name,
            "station_id": station.id,
            "station_location": station.location,
            "capture_date": min((item.capture_date for item in images), default=pd.NaT),
            "has_excel_record": dmc in expected_dmcs if excel_dmcs is not None else pd.NA,
        }
        for view in profile.views:
            code = f"{profile.family_code}{view}"
            candidates = by_code.get(code, [])
            row[f"image_{code}_path"] = str(candidates[0].path) if len(candidates) == 1 else ""
            if len(candidates) > 1:
                issue_rows.append({
                    "级别": "错误", "DMC": dmc, "文件": " | ".join(str(item.path) for item in candidates),
                    "问题": f"视图 {code} 存在多个候选文件，未自动选择",
                })
        row["a_image_path"] = row.get(f"image_{profile.primary_raw_code}_path", "")
        row["e_image_path"] = row.get(f"image_{profile.primary_result_code}_path", "")
        row["has_primary_pair"] = bool(row["a_image_path"] and row["e_image_path"])
        if not row["has_primary_pair"]:
            issue_rows.append({
                "级别": "警告", "DMC": dmc, "文件": "",
                "问题": f"缺少主图对 {profile.primary_raw_code}+{profile.primary_result_code}，Excel记录保留但跳过图片算法",
            })
        rows.append(row)
    products = pd.DataFrame(rows)
    if not products.empty:
        products = products.sort_values(
            ["capture_date", "dmc_raw"], na_position="last", kind="stable"
        ).reset_index(drop=True)
        products.insert(0, "global_order", range(1, len(products) + 1))
    issues = pd.DataFrame(issue_rows, columns=["级别", "DMC", "文件", "问题"])
    return ImageIndexResult(products, issues, scanned)


def validate_selected_station(
    station: StationDefinition,
    query_parameters: dict[str, Any],
    catalog: StationCatalog,
) -> list[str]:
    """当Excel声明了Location(s)时强制与界面所选工站一致。"""
    location = str(query_parameters.get("Location(s)", "") or "").strip()
    if not location:
        return ["Excel未提供 Location(s)，工站未经工作簿交叉验证"]
    actual = catalog.station_for_location(location)
    if location != station.location:
        actual_name = actual.display_name if actual else f"未知工站 ({location})"
        raise ValueError(
            f"所选工站与Excel不一致：所选 {station.display_name}；Excel {actual_name}"
        )
    return []
