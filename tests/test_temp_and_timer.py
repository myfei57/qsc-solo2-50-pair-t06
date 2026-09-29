"""模温确认与硫化计时：落盘、闩锁与进度判定。"""

from __future__ import annotations

import pytest

from rubbercure.errors import InterlockError
from rubbercure.temp.read import MoldTempReading
from rubbercure.temp.target import TempTarget
from rubbercure.temp.tolerance import band_summary, spread_ok, within_band
from rubbercure.timer.done import complete_cure
from rubbercure.timer.latch import CureLatch
from rubbercure.timer.validate import validate_progress


def test_tolerance_helpers_agree_with_target_band(platform):
    limits = platform.config.temp
    assert within_band(limits.setpoint_c, limits.setpoint_c, limits.tolerance_c)
    assert not within_band(limits.setpoint_c + limits.tolerance_c + 0.01, limits.setpoint_c, limits.tolerance_c)
    assert spread_ok(1.2, limits.max_cavity_spread_c)
    assert not spread_ok(9.9, limits.max_cavity_spread_c)
    summary = band_summary(limits.setpoint_c - 1.0, limits.setpoint_c, limits.tolerance_c)
    assert summary["in_band"] is True
    target = TempTarget(limits.setpoint_c, limits.tolerance_c, limits.warmup_rate_c_per_min)
    assert target.is_in_band(limits.setpoint_c)
    assert target.clamp(limits.setpoint_c + 99.0) == target.band()[1]


def test_confirm_rejects_temperature_outside_band(platform):
    unit = platform.press("press-01")
    reading = MoldTempReading(
        mold_id="mold-T",
        generation=1,
        sampled_at=platform.clock.now(),
        by_cavity={0: 100.0, 1: 101.0},
    )
    with pytest.raises(InterlockError):
        unit.temp_persister.confirm(reading, platform.config.temp.confirm_hold_s)


def test_confirm_rejects_short_hold(platform):
    unit = platform.press("press-01")
    reading = MoldTempReading(
        mold_id="mold-T2",
        generation=1,
        sampled_at=platform.clock.now(),
        by_cavity={0: platform.config.temp.setpoint_c},
    )
    with pytest.raises(InterlockError):
        unit.temp_persister.confirm(reading, platform.config.temp.confirm_hold_s - 1.0)


def test_confirmation_is_durable_and_restorable(platform):
    unit = platform.press("press-02")
    reading = MoldTempReading(
        mold_id="mold-T3",
        generation=2,
        sampled_at=platform.clock.now(),
        by_cavity={0: platform.config.temp.setpoint_c, 1: platform.config.temp.setpoint_c + 0.5},
    )
    confirmation = unit.temp_persister.confirm(reading, platform.config.temp.confirm_hold_s)
    assert unit.temp_persister.is_durable()
    assert unit.temp_persister.stored()["mold_id"] == "mold-T3"
    assert unit.temp_persister.snapshot()["stored_mold"] == "mold-T3"
    assert unit.temp_persister.require_durable().digest == confirmation.digest

    reloaded = type(unit.temp_persister)(
        platform.clock, platform.bus, unit.store, platform.config.temp
    )
    assert reloaded.restore() is True
    assert reloaded.latest().mold_id == "mold-T3"


def test_latch_is_per_mold(platform):
    unit = platform.press("press-01")
    unit.timer.begin("mold-L1", "RB-215", 600.0, 1)
    unit.timer.accumulate(600.0)
    result = complete_cure(unit.timer, platform.latch, platform.bus)
    assert result["latch_set"] is True
    assert platform.latch.is_set("mold-L1")
    assert not platform.latch.is_set("mold-L2")

    other = platform.press("press-02")
    other.timer.begin("mold-L2", "RB-215", 600.0, 1)
    other.timer.accumulate(600.0)
    complete_cure(other.timer, platform.latch, platform.bus)
    assert platform.latch.set_molds() == ["mold-L1", "mold-L2"]
    assert platform.latch.reset("mold-L1", platform.clock.now()).released is True
    assert platform.latch.set_molds() == ["mold-L2"]


def test_validate_progress_verdicts(platform):
    limits = platform.config.timer
    assert validate_progress(600.0, 600.0, limits)["verdict"] == "ok"
    assert validate_progress(300.0, 600.0, limits)["verdict"] == "short"
    assert validate_progress(900.0, 600.0, limits)["verdict"] == "over"


def test_timer_locks_thickness_and_keeps_verdict_until_reset(platform):
    unit = platform.press("press-01")
    unit.timer.begin("m-v1", "RB-215", 600.0, 2, thickness_mm=12.0)
    assert unit.timer.thickness_mm == 12.0
    assert unit.timer.product_code == "RB-215"
    assert unit.timer.snapshot()["verdict"] is None
    unit.timer.accumulate(300.0)
    complete_cure(unit.timer, platform.latch, platform.bus)
    assert unit.timer.verdict["verdict"] == "short"
    assert unit.timer.snapshot()["verdict"]["verdict_label"] == "欠硫"
    unit.timer.reset("m-v1")
    assert unit.timer.verdict is None
    assert unit.timer.thickness_mm == 0.0
    assert unit.timer.product_code == ""


def test_timer_persists_and_restores_basis_and_verdict(platform):
    unit = platform.press("press-02")
    unit.timer.begin("m-v2", "RB-215", 600.0, 2, thickness_mm=8.0)
    unit.timer.accumulate(900.0)
    complete_cure(unit.timer, platform.latch, platform.bus)
    unit.timer.persist_state()

    reloaded = type(unit.timer)(platform.bus, unit.store, platform.config.timer)
    assert reloaded.restore() is True
    assert reloaded.product_code == "RB-215"
    assert reloaded.generation == 2
    assert reloaded.thickness_mm == 8.0
    assert reloaded.verdict["verdict"] == "over"


def test_complete_cure_reports_thickness_generation_and_label(platform):
    unit = platform.press("press-03")
    unit.timer.begin("m-v3", "RB-310", 400.0, 5, thickness_mm=3.0)
    unit.timer.accumulate(400.0)
    result = complete_cure(unit.timer, platform.latch, platform.bus)
    assert result["thickness_mm"] == 3.0
    assert result["curve_generation"] == 5
    assert result["verdict"] == "ok"
    assert result["verdict_label"] == "合格"
    assert result["within_window"] is True


def test_latch_release_all_reports_count():
    latch = CureLatch()
    latch.set("a", 1.0)
    latch.set("b", 2.0)
    assert latch.release_all() == 2
    assert latch.set_molds() == []
