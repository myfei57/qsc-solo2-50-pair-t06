"""完成上报。

一台硫化机取件后向调度上报一条完成记录，带完成时刻、模具、曲线代际与本
机上报序号。上报本身只组装数据，排序与后续分配交给调度器；上报序号由调
度器统一分配，保证跨机可比。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..eventbus import Event, EventBus


@dataclass(frozen=True)
class CompletionReport:
    """一条完成上报。"""

    press_id: str
    mold_id: str
    product_code: str
    completed_at: float
    curve_generation: int
    sequence: int
    thickness_mm: float = 0.0
    planned_s: float = 0.0
    elapsed_s: float = 0.0
    verdict: str = ""

    def as_dict(self) -> dict:
        return {
            "press_id": self.press_id,
            "mold_id": self.mold_id,
            "product_code": self.product_code,
            "completed_at": round(self.completed_at, 3),
            "curve_generation": self.curve_generation,
            "sequence": self.sequence,
            "thickness_mm": round(self.thickness_mm, 3),
            "planned_s": round(self.planned_s, 3),
            "elapsed_s": round(self.elapsed_s, 3),
            "verdict": self.verdict,
        }


def report_completion(
    press_id: str,
    mold_id: str,
    product_code: str,
    completed_at: float,
    curve_generation: int,
    sequence: int,
    bus: EventBus,
    *,
    thickness_mm: float = 0.0,
    planned_s: float = 0.0,
    elapsed_s: float = 0.0,
    verdict: str = "",
) -> CompletionReport:
    """组装一条完成上报并发布事件。"""

    report = CompletionReport(
        press_id=press_id,
        mold_id=mold_id,
        product_code=product_code,
        completed_at=float(completed_at),
        curve_generation=int(curve_generation),
        sequence=int(sequence),
        thickness_mm=float(thickness_mm),
        planned_s=float(planned_s),
        elapsed_s=float(elapsed_s),
        verdict=str(verdict),
    )
    bus.publish(
        Event.of(
            "batch",
            "batch",
            "completion_reported",
            subject=mold_id,
            severity="notice",
            **report.as_dict(),
        )
    )
    return report
