"""本文件验证18工站目录、现场图片命名解析和自动产品索引。"""

from pathlib import Path

import pandas as pd
import pytest

from src.station_sources import (
    build_image_product_index,
    load_station_catalog,
    parse_image_filename,
    validate_selected_station,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_station_catalog_contains_all_unique_company_stations() -> None:
    catalog = load_station_catalog(PROJECT_ROOT / "config/stations.yaml")
    assert len(catalog.stations) == 18
    assert len({station.location for station in catalog.stations}) == 18
    assert catalog.station("35_5s_aoi").location == "3003.10.1.1.6"
    assert catalog.station("conveyor_7x_vi").image_profile == "7X"


def test_real_filename_shape_extracts_excel_ident_number() -> None:
    parsed = parse_image_filename(Path("376W020BGO57424F00VF004AK20250624DA.png"))
    assert parsed is not None
    assert parsed.dmc_raw == "376W020BGO57424F00VF004AK"
    assert parsed.capture_date == pd.Timestamp("2025-06-24")
    assert parsed.image_code == "DA"


def test_image_index_uses_station_primary_pair_and_keeps_missing_excel_rows(tmp_path: Path) -> None:
    catalog = load_station_catalog(PROJECT_ROOT / "config/stations.yaml")
    station = catalog.station("35_5s_aoi")
    dmc = "376W020BGO57424F00VF004AK"
    for code in ("DA", "DC", "DE", "DX", "DY"):
        (tmp_path / f"{dmc}20250624{code}.png").write_bytes(b"not-decoded-during-indexing")
    result = build_image_product_index(tmp_path, station, catalog, [dmc, "EXCEL-WITHOUT-IMAGE"])
    indexed = result.products.set_index("dmc_raw")
    assert len(indexed) == 2
    assert indexed.loc[dmc, "has_primary_pair"]
    assert indexed.loc[dmc, "a_image_path"].endswith("DA.png")
    assert indexed.loc[dmc, "e_image_path"].endswith("DE.png")
    assert not indexed.loc["EXCEL-WITHOUT-IMAGE", "has_primary_pair"]
    assert result.issues["DMC"].eq("EXCEL-WITHOUT-IMAGE").any()


def test_wrong_family_is_not_mixed_into_selected_station(tmp_path: Path) -> None:
    catalog = load_station_catalog(PROJECT_ROOT / "config/stations.yaml")
    station = catalog.station("35_5s_aoi")
    (tmp_path / "DMC00120250624EA.png").write_bytes(b"x")
    result = build_image_product_index(tmp_path, station, catalog)
    assert result.products.empty
    assert result.issues["问题"].str.contains("不一致").any()


def test_excel_location_mismatch_is_blocked() -> None:
    catalog = load_station_catalog(PROJECT_ROOT / "config/stations.yaml")
    selected = catalog.station("35_5s_aoi")
    with pytest.raises(ValueError, match="Excel不一致"):
        validate_selected_station(selected, {"Location(s)": "3003.10.1.1.1"}, catalog)
    assert validate_selected_station(selected, {"Location(s)": selected.location}, catalog) == []
