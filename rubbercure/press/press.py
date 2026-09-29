"""单台硫化机。

把开合模、蒸汽、模温、计时与冷却装成一台硫化机的完整回路，并保留相位机、
在机模具与最近一次循环摘要。控制循环每周期推进一次模温闭环、蒸汽闭母管、
计时进度与冷却采样；顺序类动作（合模、升温、计时、泄压、开模）由
``sequence`` 包统一编排，设备本身只提供被顺序调用的原语。
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from ..clock import Clock
from ..config import PlatformConfig
from ..cool.cool import CoolingUnit
from ..errors import InterlockError
from ..eventbus import Event, EventBus
from ..lift.lift import LiftUnit
from ..steam.steam import SteamUnit
from ..store.jsonfile import JsonStore
from ..temp.controller import TempController
from ..temp.persist import TempPersister
from ..temp.regulation import TempLoop
from ..timer.timer import CureTimer
from ..timer.state import TimerPhase
from ..timer.cycle import advance_cure
from .transitions import PressPhase, PressStateMachine

if TYPE_CHECKING:  # pragma: no cover - 仅用于类型标注
    from ..temp.read import MoldTempReading


PRESS_STATE_KEY = "press_state"


class CuringPress:
    """一台硫化机的完整回路。"""

    def __init__(
        self,
        press_id: str,
        clock: Clock,
        bus: EventBus,
        store: JsonStore,
        config: PlatformConfig,
    ) -> None:
        self.press_id = press_id
        self.clock = clock
        self.bus = bus
        self.store = store
        self.config = config
        self.lift = LiftUnit(bus)
        self.steam = SteamUnit(clock, bus, store, config.steam)
        self.temp_persister = TempPersister(clock, bus, store, config.temp)
        self.temp_loop = TempLoop(
            config.temp,
            TempController(kp=6.0, ki=0.5, output_min=-180.0, output_max=160.0),
            self.temp_persister,
            self.steam,
            bus,
        )
        self.timer = CureTimer(bus, store, config.timer)
        self.cooling = CoolingUnit(clock, bus, config.cool)
        self._state = PressStateMachine()
        self._mold_id: str | None = None
        self._cycles = 0
        self._last_step: dict = {}
        self._lock = threading.RLock()

    @property
    def phase(self) -> PressPhase:
        return self._state.phase

    @property
    def mold_id(self) -> str | None:
        with self._lock:
            return self._mold_id

    def transition(self, target: PressPhase) -> PressPhase:
        return self._state.transition(target)

    def load_mold(self, mold_id: str) -> dict:
        """装模：把模具挂到本机。"""

        with self._lock:
            self.transition(PressPhase.MOLD_LOADED)
            self._mold_id = mold_id
        self.bus.publish(
            Event.of("press", "press", "mold_loaded", subject=mold_id, press_id=self.press_id)
        )
        return {"press_id": self.press_id, "mold_id": mold_id, "phase": self._state.phase.value}

    def clear_mold(self) -> None:
        with self._lock:
            self._mold_id = None

    def require_mold(self) -> str:
        """返回在机模具；没有则报错。"""

        with self._lock:
            mold_id = self._mold_id
        if mold_id is None:
            raise InterlockError(f"硫化机 {self.press_id} 未装模", component="press")
        return mold_id

    def step(self, dt: float, reading_c: "MoldTempReading") -> dict:
        """推进一个控制周期：模温闭环 → 蒸汽充压 → 计时 → 冷却采样。"""

        with self._lock:
            self._cycles += 1
            self.timer.set_clock_now(self.clock.now())
            temperature = self.temp_loop.step(reading_c, dt)
            steam = self.steam.advance(dt)
            measured = reading_c.average_c()
            if self._state.phase is PressPhase.CURING:
                advance_cure(self.timer, dt, self.bus, measured_c=measured)
            cooling = self.cooling.measure(
                self.config.cool.alarm_inlet_c - 6.0, measured * 0.42
            )
            self._last_step = {
                "cycle": self._cycles,
                "press_id": self.press_id,
                "phase": self._state.phase.value,
                "temperature": temperature,
                "steam": steam,
                "cooling": cooling,
                "cure": self.timer.estimate(),
            }
            if self.timer.phase in (TimerPhase.CURING, TimerPhase.COMPLETE):
                self.persist_state()
            return self._last_step

    def finalize_cycle(self) -> dict:
        """一个模次结束：把相位推进到 DONE 并清在机模具。"""

        with self._lock:
            self.transition(PressPhase.DONE)
            mold_id = self._mold_id
        self.bus.publish(
            Event.of("press", "press", "cycle_done", subject=mold_id or self.press_id, press_id=self.press_id)
        )
        return {"press_id": self.press_id, "mold_id": mold_id, "phase": self._state.phase.value}

    def idle(self) -> dict:
        """回到待机，允许装载下一模。"""

        with self._lock:
            self.transition(PressPhase.IDLE)
            self._mold_id = None
            self.persist_state()
        return {"press_id": self.press_id, "phase": self._state.phase.value}

    def maintenance_reset(self) -> None:
        """检修复位：整机回路与在机模具全部回到初始状态。"""

        with self._lock:
            self.store.remove(PRESS_STATE_KEY)
            self._state = PressStateMachine()
            self._mold_id = None
            self._cycles = 0
            self._last_step = {}
            self.steam.maintenance_reset()
            self.temp_persister.maintenance_reset()
            self.timer.maintenance_reset()

    def persist_state(self) -> dict:
        """把蒸汽、模温确认、计时与在机模具一起落盘。"""

        with self._lock:
            self.store.write(
                PRESS_STATE_KEY,
                {"mold_id": self._mold_id, "phase": self._state.phase.value, "cycles": self._cycles},
            )
            steam_digest = self.steam.persist_state()
            timer_digest = self.timer.persist_state()
        return {
            "press": self.press_id,
            "mold_id": self.mold_id,
            "phase": self._state.phase.value,
            "cycles": self._cycles,
            "steam": steam_digest,
            "timer": timer_digest,
        }

    def restore(self) -> dict:
        """从磁盘恢复在机模具、蒸汽、计时与模温确认。"""

        result = {
            "steam": self.steam.restore(),
            "timer": self.timer.restore(),
            "temp": self.temp_persister.restore(),
        }
        snapshot = self.store.read(PRESS_STATE_KEY)
        if snapshot:
            mold_id = snapshot.get("mold_id")
            self._mold_id = str(mold_id) if mold_id else None
            self._cycles = int(snapshot.get("cycles") or 0)
            phase = snapshot.get("phase")
            if isinstance(phase, str):
                try:
                    self._state.restore({"phase": phase})
                except Exception:
                    self._state = PressStateMachine()
        # 计时器完成归位空闲后，机级相位也回到待机，保证下一模可投产。
        if self.timer.phase is TimerPhase.IDLE and self._state.phase in (
            PressPhase.CURING,
            PressPhase.COOLING,
            PressPhase.VENTING,
            PressPhase.OPENING,
            PressPhase.DONE,
        ):
            self._state = PressStateMachine()
        if self.timer.phase is TimerPhase.CURING and self._state.phase is PressPhase.IDLE:
            self._state.transition(PressPhase.CURING)
        if self.timer.phase is TimerPhase.CURING and self._mold_id and not self.lift.is_closed():
            self.lift.restore_closed(self._mold_id)
        return result

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "press_id": self.press_id,
                "phase": self._state.phase.value,
                "history": self._state.history(),
                "mold_id": self._mold_id,
                "cycles": self._cycles,
                "lift": self.lift.snapshot(),
                "steam": self.steam.snapshot(),
                "temp": self.temp_loop.snapshot(),
                "timer": self.timer.snapshot(),
                "cooling": self.cooling.snapshot(),
                "last_step": self._last_step,
            }
