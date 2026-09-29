"""硫化曲线基线与代际。

硫化时长不是常数，而是由制品工艺曲线给出：曲线按制品型号登记，并随工艺
调整、配方迭代、客户改版产生新代际。计时必须以代际最新的曲线为基线计算，
否则用旧配方计时，轻则欠硫、重则过硫。基线登记同样单调递增，读取按代际
最大者胜出。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from ..errors import CureError
from ..mold.generation import GenerationCounter
from ..store.jsonfile import JsonStore

REFERENCE_THICKNESS_MM = 6.0

CURVE_STATE_KEY = "cure_curves"


@dataclass(frozen=True)
class CureCurve:
    """一份制品硫化曲线基线。"""

    product_code: str
    generation: int
    nominal_s: float
    registered_at: float

    def __post_init__(self) -> None:
        if self.nominal_s <= 0:
            raise CureError(f"曲线 {self.product_code} 的标称硫化时长必须为正", component="timer")

    def cure_seconds(self, thickness_mm: float) -> float:
        """按制品厚度折算硫化时长：厚度越大，时间越长。"""

        if thickness_mm <= 0:
            raise CureError("制品厚度必须为正", component="timer")
        return self.nominal_s * (thickness_mm / REFERENCE_THICKNESS_MM) ** 1.4

    def as_dict(self) -> dict:
        return {
            "product_code": self.product_code,
            "generation": self.generation,
            "nominal_s": round(self.nominal_s, 3),
            "registered_at": round(self.registered_at, 3),
        }


class CurveBook:
    """按制品型号保存全部代际曲线，读取时取最新代际。

    给出 :class:`JsonStore` 时曲线台账落盘：登记新一代曲线立即原子写入，
    进程重启后按落盘内容恢复。这样曲线改版不会被重启"抹掉"，老代际曲线
    也不会和新代际混在一起——每代记录都挂着自己的代际号与登记时刻。
    """

    def __init__(
        self,
        counter: GenerationCounter | None = None,
        store: JsonStore | None = None,
    ) -> None:
        self._counter = counter or GenerationCounter()
        self._store = store
        self._by_product: dict[str, list[CureCurve]] = {}
        self._lock = threading.RLock()

    def register(self, product_code: str, nominal_s: float, registered_at: float) -> CureCurve:
        """登记一份新代际曲线并落盘。"""

        if not product_code:
            raise CureError("制品型号不能为空", component="timer")
        with self._lock:
            generation = self._counter.next()
            curve = CureCurve(
                product_code=product_code,
                generation=generation,
                nominal_s=float(nominal_s),
                registered_at=float(registered_at),
            )
            self._by_product.setdefault(product_code, []).append(curve)
            self._persist_locked()
        return curve

    def latest_for(self, product_code: str) -> CureCurve:
        """返回该制品型号代际最新的曲线。"""

        with self._lock:
            history = list(self._by_product.get(product_code, ()))
        if not history:
            raise CureError(f"制品 {product_code} 尚未登记硫化曲线", component="timer")
        return max(history, key=lambda item: item.generation)

    def baseline(self, product_code: str, thickness_mm: float) -> float:
        """按最新代际曲线算出该制品的标称硫化时长。"""

        return self.latest_for(product_code).cure_seconds(thickness_mm)

    def history(self, product_code: str) -> list[CureCurve]:
        with self._lock:
            history = list(self._by_product.get(product_code, ()))
        return sorted(history, key=lambda item: item.generation)

    def products(self) -> list[str]:
        """返回已登记曲线的全部制品型号。"""

        with self._lock:
            return sorted(self._by_product)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                product_code: [curve.as_dict() for curve in history]
                for product_code, history in sorted(self._by_product.items())
            }

    def _persist_locked(self) -> str:
        if self._store is None:
            return ""
        payload = {
            "products": {
                product_code: [curve.as_dict() for curve in history]
                for product_code, history in self._by_product.items()
            }
        }
        self._store.write(CURVE_STATE_KEY, payload)
        return self._store.digest(CURVE_STATE_KEY)

    def restore(self) -> bool:
        """从磁盘恢复曲线台账，代际计数器接着最大代际继续分配。"""

        if self._store is None:
            return False
        snapshot = self._store.read(CURVE_STATE_KEY)
        if not snapshot:
            return False
        products = snapshot.get("products")
        if not isinstance(products, dict):
            raise CureError("曲线台账快照缺少 products 段落", component="timer")
        with self._lock:
            restored: dict[str, list[CureCurve]] = {}
            max_generation = 0
            for product_code, raw_history in products.items():
                if not isinstance(raw_history, list):
                    raise CureError(f"曲线台账中 {product_code} 的历史必须是列表", component="timer")
                curves: list[CureCurve] = []
                for item in raw_history:
                    curve = CureCurve(
                        product_code=str(product_code),
                        generation=int(item["generation"]),
                        nominal_s=float(item["nominal_s"]),
                        registered_at=float(item["registered_at"]),
                    )
                    curves.append(curve)
                    max_generation = max(max_generation, curve.generation)
                restored[str(product_code)] = sorted(curves, key=lambda item: item.generation)
            self._by_product = restored
            self._counter = GenerationCounter(start=max_generation)
        return True
