"""本文件测试缺陷中心的停机识别、事件暴露与初步原因证据。"""

import pandas as pd

from src.defect_cause_analysis import (
    analyze_defect_causes, build_cause_evidence, build_product_event_exposure,
    detect_downtime_events, select_product_exposure_details,
)


def _event(dmc: str, station: str, moment: str, order: int) -> dict:
    return {
        "event_id": f"{station}-{dmc}", "dmc_raw": dmc, "station_id": station,
        "test_date": pd.Timestamp(moment), "production_order": order,
        "source_sheet": "synthetic", "state": "OK",
    }


def test_synchronous_station_gaps_are_inferred_as_one_high_confidence_stop() -> None:
    rows = []
    for station in ["35_wp1", "35_wp2", "35_wp3", "35_wp4", "35_wp5", "35_5s_aoi"]:
        for order, moment in enumerate(("10:00:00", "10:00:03", "10:00:06", "10:01:09"), 1):
            rows.append(_event(f"{station}-{order}", station, f"2026-08-01 {moment}", order))

    result = detect_downtime_events(pd.DataFrame(rows), scope="5S")

    assert len(result) == 1
    stop = result.iloc[0]
    assert stop.start_time == pd.Timestamp("2026-08-01 10:00:06")
    assert stop.restart_time == pd.Timestamp("2026-08-01 10:01:09")
    assert stop.duration_seconds == 63
    assert stop.station_count == 6
    assert stop.confidence == "高"
    assert stop.confirmation_status == "疑似停机"


def test_product_is_assigned_to_the_adjacent_station_segment_crossing_stop() -> None:
    events = pd.DataFrame([
        _event("DMC-1", "35_wp1", "2026-08-01 09:58:00", 1),
        _event("DMC-1", "35_wp2", "2026-08-01 10:00:00", 1),
        _event("DMC-1", "35_wp3", "2026-08-01 10:05:00", 1),
        _event("DMC-1", "35_5s_aoi", "2026-08-01 10:10:00", 1),
        _event("DMC-2", "35_wp1", "2026-08-01 10:11:00", 2),
        _event("DMC-2", "35_wp2", "2026-08-01 10:12:00", 2),
        _event("DMC-2", "35_wp3", "2026-08-01 10:13:00", 2),
        _event("DMC-2", "35_5s_aoi", "2026-08-01 10:14:00", 2),
    ])
    codes = pd.DataFrame([
        {"dmc_raw": "DMC-1", "analysis_scope": "5S", "source_type": "VI_BLOCK",
         "canonical_code": "5011", "code_status": "defect", "production_order": 1,
         "test_date": pd.Timestamp("2026-08-01 11:00:00")},
        {"dmc_raw": "DMC-2", "analysis_scope": "5S", "source_type": "VI_BLOCK",
         "canonical_code": "", "code_status": "normal", "production_order": 2,
         "test_date": pd.Timestamp("2026-08-01 11:01:00")},
    ])
    stops = pd.DataFrame([{
        "downtime_id": "STOP-5S-0001", "analysis_scope": "5S",
        "start_time": pd.Timestamp("2026-08-01 10:01:00"),
        "restart_time": pd.Timestamp("2026-08-01 10:04:00"),
        "duration_seconds": 180,
    }])

    result = build_product_event_exposure(
        events, pd.DataFrame(), codes, stops,
        source_type="VI_BLOCK", code="5011", scope="5S",
    ).set_index("dmc_raw")

    assert bool(result.loc["DMC-1", "experienced_downtime"])
    assert result.loc["DMC-1", "downtime_segments"] == "WP2—WP3"
    assert not bool(result.loc["DMC-2", "experienced_downtime"])


def test_missing_environment_measurements_are_reported_not_invented() -> None:
    events = pd.DataFrame([
        _event("DMC-1", "35_5s_aoi", "2026-08-01 10:00:00", 1),
        _event("DMC-2", "35_5s_aoi", "2026-08-01 10:00:03", 2),
    ])
    codes = pd.DataFrame([
        {"dmc_raw": "DMC-1", "analysis_scope": "5S", "source_type": "VI_BLOCK",
         "canonical_code": "5011", "code_status": "defect", "production_order": 1,
         "test_date": pd.Timestamp("2026-08-01 11:00:00"), "defect_name": "折皱"},
        {"dmc_raw": "DMC-2", "analysis_scope": "5S", "source_type": "VI_BLOCK",
         "canonical_code": "", "code_status": "normal", "production_order": 2,
         "test_date": pd.Timestamp("2026-08-01 11:01:00"), "defect_name": ""},
    ])

    result = analyze_defect_causes(
        events, pd.DataFrame(), codes,
        source_type="VI_BLOCK", code="5011", scope="5S",
    )

    text = " ".join(result.hypotheses.astype(str).to_numpy().ravel())
    assert "温度" not in text
    assert result.hypotheses["warning"].eq("候选机理，不是已确认根本原因").all()
    assert result.summary["warning"].startswith("Excel仅能推断")


def test_normal_cycle_single_station_gap_and_cross_midnight_are_distinguished() -> None:
    normal = pd.DataFrame([
        _event(f"DMC-{index}", "35_wp1", moment, index)
        for index, moment in enumerate((
            "2026-08-01 23:59:51", "2026-08-01 23:59:54",
            "2026-08-01 23:59:57", "2026-08-02 00:00:00",
        ), 1)
    ])
    assert detect_downtime_events(normal, scope="5S").empty

    with_gap = pd.concat([normal, pd.DataFrame([
        _event("DMC-5", "35_wp1", "2026-08-02 00:01:03", 5),
    ])], ignore_index=True)
    stop = detect_downtime_events(with_gap, scope="5S").iloc[0]
    assert stop.duration_seconds == 63
    assert stop.confidence == "低"
    assert stop.confirmation_status == "数据空档/低可信疑似停机"


def test_repeated_pass_and_window_censorship_remain_explicit() -> None:
    events = pd.DataFrame([
        _event("DMC-1", "35_wp2", "2026-08-01 10:00:00", 1),
        _event("DMC-1", "35_wp2", "2026-08-01 10:00:01", 1),
        _event("DMC-1", "35_wp3", "2026-08-01 10:00:03", 1),
    ])
    codes = pd.DataFrame([{
        "dmc_raw": "DMC-1", "analysis_scope": "5S", "source_type": "VI_BLOCK",
        "canonical_code": "5011", "code_status": "defect", "production_order": 1,
        "test_date": pd.Timestamp("2026-08-01 10:00:04"),
    }])

    exposure = build_product_event_exposure(
        events, pd.DataFrame(), codes, pd.DataFrame(),
        source_type="VI_BLOCK", code="5011", scope="5S",
    ).iloc[0]

    assert exposure.observed_station_count == 2
    assert exposure.route_match_status == "matched"
    assert exposure.route_coverage_status == "partial_route"
    assert bool(exposure.left_censored)
    assert bool(exposure.right_censored)


def test_products_before_cross_station_overlap_are_labeled_as_window_censored() -> None:
    events = pd.DataFrame([
        _event("DMC-MATCH", "35_wp1", "2026-08-01 10:00:00", 1),
        _event("DMC-MATCH", "35_5s_aoi", "2026-08-01 10:10:00", 1),
    ])
    codes = pd.DataFrame([
        {
            "dmc_raw": "DMC-EARLY", "analysis_scope": "5S", "source_type": "VI_BLOCK",
            "canonical_code": "", "code_status": "normal", "production_order": 1,
            "test_date": pd.Timestamp("2026-08-01 10:50:00"),
        },
        {
            "dmc_raw": " dmc-match ", "analysis_scope": "5S", "source_type": "VI_BLOCK",
            "canonical_code": "5011", "code_status": "defect", "production_order": 2,
            "test_date": pd.Timestamp("2026-08-01 11:00:00"),
        },
        {
            "dmc_raw": "DMC-LATE", "analysis_scope": "5S", "source_type": "VI_BLOCK",
            "canonical_code": "", "code_status": "normal", "production_order": 3,
            "test_date": pd.Timestamp("2026-08-01 11:10:00"),
        },
    ])

    result = build_product_event_exposure(
        events, pd.DataFrame(), codes, pd.DataFrame(),
        source_type="VI_BLOCK", code="5011", scope="5S",
    ).set_index("dmc_raw")

    assert result.loc[" dmc-match ", "route_match_status"] == "matched"
    assert result.loc["DMC-EARLY", "route_match_status"] == "suspected_left_window_censored"
    assert bool(result.loc["DMC-EARLY", "left_censored"])
    assert result.loc["DMC-LATE", "route_match_status"] == "not_in_current_route_window"


def test_cause_summary_reports_traceability_instead_of_dmc_match_failure() -> None:
    events = pd.DataFrame([
        _event("DMC-MATCH", "35_wp1", "2026-08-01 10:00:00", 1),
        _event("DMC-MATCH", "35_wp2", "2026-08-01 10:00:03", 1),
        _event("DMC-MATCH", "35_wp3", "2026-08-01 10:00:06", 1),
        _event("DMC-MATCH", "35_wp4", "2026-08-01 10:00:09", 1),
        _event("DMC-MATCH", "35_wp5", "2026-08-01 10:00:12", 1),
        _event("DMC-MATCH", "35_5s_aoi", "2026-08-01 10:00:15", 1),
    ])
    codes = pd.DataFrame([
        {
            "dmc_raw": "DMC-EARLY", "analysis_scope": "5S", "source_type": "VI_BLOCK",
            "canonical_code": "", "code_status": "normal", "production_order": 1,
            "test_date": pd.Timestamp("2026-08-01 10:30:00"), "defect_name": "",
        },
        {
            "dmc_raw": "DMC-MATCH", "analysis_scope": "5S", "source_type": "VI_BLOCK",
            "canonical_code": "5011", "code_status": "defect", "production_order": 2,
            "test_date": pd.Timestamp("2026-08-01 11:00:00"), "defect_name": "折皱",
        },
    ])

    result = analyze_defect_causes(
        events, pd.DataFrame(), codes,
        source_type="VI_BLOCK", code="5011", scope="5S",
    )

    assert result.summary["current_window_traceable_count"] == 1
    assert result.summary["complete_route_count"] == 1
    assert result.summary["suspected_left_window_censored_count"] == 1
    assert "窗口外产品不等于DMC匹配失败" in result.summary["warning"]


def test_no_unexposed_control_is_kept_as_exploratory_evidence() -> None:
    exposure = pd.DataFrame([
        {
            "route_match_status": "matched", "is_target_defect": True,
            "target_time": pd.Timestamp("2026-08-01 10:00:00"),
            "experienced_downtime": True, "post_restart_90_seconds": True,
            "post_restart_30_products": True, "material_or_batch_change": False,
            "speed_anomaly": False, "tension_anomaly": False, "static_anomaly": False,
            "downtime_segments": "WP2—WP3", "restart_bucket": "1-10",
            "longest_downtime_seconds": 120,
        }
        for _ in range(12)
    ])

    evidence = build_cause_evidence(
        exposure, scope="5S", source_type="VI_BLOCK", code="5011",
    )
    stop = evidence[evidence["evidence_type"].eq("停机—缺陷")].iloc[0]

    assert stop.unexposed_count == 0
    assert stop.evidence_level == "探索性线索"
    assert pd.isna(stop.risk_ratio)


def test_cause_finding_selects_exact_exposed_and_comparison_products() -> None:
    exposure = pd.DataFrame([
        {
            "analysis_scope": "5S", "source_type": "VI_BLOCK", "canonical_code": "5011",
            "route_match_status": "matched", "dmc_raw": f"DMC-{index}",
            "experienced_downtime": index <= 2, "is_target_defect": index in {1, 4},
            "target_time": pd.Timestamp("2026-08-01 10:00:00") + pd.Timedelta(seconds=index),
        }
        for index in range(1, 5)
    ])
    finding = {
        "analysis_scope": "5S", "source_type": "VI_BLOCK", "canonical_code": "5011",
        "sample_filter_field": "experienced_downtime", "sample_filter_operator": "truthy",
        "sample_filter_value": True,
    }

    details = select_product_exposure_details(exposure, finding)

    assert len(details) == 4
    assert details["matches_finding"].sum() == 2
    assert set(details.loc[details["matches_finding"], "dmc_raw"]) == {"DMC-1", "DMC-2"}
    assert set(details["comparison_group"]) == {"暴露组", "未满足条件组"}
