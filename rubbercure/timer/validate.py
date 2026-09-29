"""硫化进度校验。

计时结束时按计划时长给出三种判定：提前结束（欠硫）、落在窗口内（合格）、
超时（过硫）。判定只读时长，不改状态；结果直接进审计，供工艺追溯。
"""

from __future__ import annotations

from ..config import TimerLimits

VERDICT_SHORT = "short"
VERDICT_OK = "ok"
VERDICT_OVER = "over"

VERDICT_LABELS = {
    VERDICT_SHORT: "欠硫",
    VERDICT_OK: "合格",
    VERDICT_OVER: "过硫",
}


def verdict_label(verdict: str) -> str:
    """返回判定结论的中文名，未知判定原样返回。"""

    return VERDICT_LABELS.get(verdict, verdict)


def validate_progress(elapsed_s: float, planned_s: float, limits: TimerLimits) -> dict:
    """按计划时长与工艺窗口判定硫化进度。"""

    if planned_s <= 0:
        raise ValueError("计划硫化时长必须为正")
    ratio = float(elapsed_s) / float(planned_s)
    deviation_s = float(elapsed_s) - float(planned_s)
    if elapsed_s < planned_s * (1.0 - limits.shortfall_ratio):
        verdict = VERDICT_SHORT
    elif elapsed_s > planned_s * (1.0 + limits.overrun_ratio):
        verdict = VERDICT_OVER
    else:
        verdict = VERDICT_OK
    return {
        "elapsed_s": round(float(elapsed_s), 3),
        "planned_s": round(float(planned_s), 3),
        "ratio": round(ratio, 4),
        "deviation_s": round(deviation_s, 3),
        "verdict": verdict,
        "verdict_label": verdict_label(verdict),
        "within_window": verdict == VERDICT_OK,
    }
