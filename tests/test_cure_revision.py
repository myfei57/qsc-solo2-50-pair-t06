"""按厚度折算时长、曲线改版代际与欠硫/过硫判定。

对应三件现场诉求：
1. 薄厚制品不再共用同一个固定时长，按厚度和最新曲线折算这一模的时长；
2. 曲线改版后登记新一代，在制中的老模次仍挂老代际，重启也不混淆；
3. 跑完直接给出欠硫／合格／过硫结论，并随完成上报进调度记录。
"""

from __future__ import annotations

import pytest

from rubbercure.app import CycleInputs, build_platform
from rubbercure.config import default_config
from rubbercure.store.jsonfile import JsonStore
from rubbercure.timer.curve import REFERENCE_THICKNESS_MM, CurveBook


def test_thickness_scales_planned_seconds():
    book = CurveBook()
    book.register("RB-215", 600.0, 1.0)
    assert book.baseline("RB-215", REFERENCE_THICKNESS_MM) == 600.0
    assert book.baseline("RB-215", 3.0) < 600.0
    assert book.baseline("RB-215", 12.0) > 600.0
    with pytest.raises(Exception):
        book.baseline("RB-215", 0.0)


def test_platform_quote_uses_latest_generation(platform):
    thin = platform.quote_cure_seconds("RB-215", 6.0)
    thick = platform.quote_cure_seconds("RB-215", 12.0)
    assert thin["curve_generation"] == 1
    assert thin["planned_s"] == 600.0
    assert thick["planned_s"] > thin["planned_s"]

    revision = platform.register_curve_revision("RB-215", 720.0)
    assert revision["generation"] == 2
    revised = platform.quote_cure_seconds("RB-215", 6.0)
    assert revised["planned_s"] == 720.0
    assert revised["curve_generation"] == 2
    # 老代际仍在台账里，不会被新版覆盖
    assert [curve.generation for curve in platform.curves.history("RB-215")] == [1, 2]


def test_curve_book_persists_and_restores_revisions(tmp_path):
    store = JsonStore(str(tmp_path / "curves"))
    book = CurveBook(store=store)
    book.register("RB-215", 600.0, 1.0)
    book.register("RB-310", 480.0, 2.0)
    book.register("RB-215", 720.0, 3.0)

    reloaded = CurveBook(store=JsonStore(str(tmp_path / "curves")))
    assert reloaded.restore() is True
    assert reloaded.latest_for("RB-215").nominal_s == 720.0
    assert reloaded.latest_for("RB-310").nominal_s == 480.0
    # 新登记的代际接着历史最大值继续分配，不与老记录撞号
    another = reloaded.register("RB-310", 500.0, 4.0)
    assert another.generation == 4


def test_running_mold_keeps_old_generation_after_revision(platform):
    platform.run_cure("mold-old", thickness_mm=6.0)
    timer = platform.press("press-01").timer
    assert timer.generation == 1
    assert timer.planned_s == 600.0

    platform.register_curve_revision("RB-215", 720.0)
    # 改版不影响已经在计时的这一模
    assert timer.generation == 1
    assert timer.planned_s == 600.0
    assert timer.thickness_mm == 6.0


def _advance(platform, press_id: str, seconds: int) -> None:
    for _ in range(seconds):
        platform.cycle(
            CycleInputs(inlet_c=30.0, steam_target_kpa=640.0, dt=1.0)
        )


def test_open_report_marks_under_cure(platform):
    platform.run_cure("mold-short", thickness_mm=6.0)
    _advance(platform, "press-01", 12)
    verdict = platform.run_open("press-01")["verdict"]
    assert verdict["verdict"] == "short"
    assert verdict["verdict_label"] == "欠硫"
    assert verdict["planned_s"] == 600.0
    assert verdict["elapsed_s"] == 12.0
    assert verdict["deviation_s"] == pytest.approx(-588.0)
    assert verdict["within_window"] is False


def test_open_report_marks_over_cure_and_ok(platform):
    # 新版曲线 720s，过硫窗口 +8% = 777.6s，跑 800s 判定过硫
    platform.register_curve_revision("RB-215", 720.0)
    platform.register_mold("mold-over", cavity_count=4)
    platform.run_cure("mold-over", thickness_mm=6.0, press_id="press-01")
    _advance(platform, "press-01", 800)
    over = platform.run_open("press-01")["verdict"]
    assert over["verdict"] == "over"
    assert over["verdict_label"] == "过硫"

    platform.register_mold("mold-ok", cavity_count=4)
    platform.run_cure("mold-ok", thickness_mm=6.0, press_id="press-02")
    _advance(platform, "press-02", 720)
    ok = platform.run_open("press-02")["verdict"]
    assert ok["verdict"] == "ok"
    assert ok["verdict_label"] == "合格"
    assert ok["within_window"] is True


def test_completion_report_carries_verdict(platform):
    platform.run_cure("mold-r", thickness_mm=6.0)
    _advance(platform, "press-01", 12)
    platform.run_open("press-01")
    platform.run_dispatch("press-01")
    report = platform.scheduler.recent_reports(limit=1)[0].as_dict()
    assert report["mold_id"] == "mold-r"
    assert report["verdict"] == "short"
    assert report["verdict_label"] == "欠硫"
    assert report["planned_s"] == 600.0
    assert report["elapsed_s"] == 12.0
    assert report["curve_generation"] == 1


def test_timer_state_round_trips_with_curve_metadata(platform, tmp_path):
    store = JsonStore(str(tmp_path / "press"))
    timer = type(platform.press("press-01").timer)(platform.bus, store, platform.config.timer)
    timer.begin("mold-p", "RB-215", 600.0, 1, thickness_mm=9.0)
    timer.accumulate(300.0)
    assert timer.persist_state()

    restored = type(timer)(platform.bus, store, platform.config.timer)
    assert restored.restore() is True
    assert restored.mold_id == "mold-p"
    assert restored.product_code == "RB-215"
    assert restored.generation == 1
    assert restored.thickness_mm == 9.0
    assert restored.planned_s == 600.0
    assert restored.elapsed_s == 300.0


def test_mid_cure_restart_resumes_timer_and_press(tmp_path):
    from rubbercure.clock import ManualClock

    root = str(tmp_path / "state")
    clock = ManualClock()
    platform = build_platform(default_config(), clock=clock, storage_root=root)
    platform.run_cure("mold-mid", thickness_mm=6.0)
    _advance(platform, "press-01", 100)
    assert platform.press().timer.elapsed_s == 100.0

    reopened = build_platform(default_config(), clock=clock, storage_root=root)
    press = reopened.press()
    assert press.timer.phase.value == "curing"
    assert press.timer.elapsed_s == 100.0
    assert press.mold_id == "mold-mid"
    assert press.lift.is_closed()
    assert press.phase.value == "curing"

    # 接续推进到 600s，开模判定合格
    _advance(reopened, "press-01", 500)
    verdict = reopened.run_open("press-01")["verdict"]
    assert verdict["verdict"] == "ok"
    assert verdict["elapsed_s"] == 600.0


def test_last_result_survives_restart_with_verdict(tmp_path):
    from rubbercure.clock import ManualClock

    root = str(tmp_path / "state")
    clock = ManualClock()
    platform = build_platform(default_config(), clock=clock, storage_root=root)
    platform.run_cure("mold-restart", thickness_mm=6.0)
    _advance(platform, "press-01", 12)
    platform.run_open("press-01")

    reopened = build_platform(default_config(), clock=clock, storage_root=root)
    assert [c.generation for c in reopened.curves.history("RB-215")] == [1]
    last = reopened.press("press-01").timer.last_result()
    assert last["verdict"] == "short"
    assert last["curve_generation"] == 1
    assert last["mold_id"] == "mold-restart"
