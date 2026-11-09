class OutOfStock(Exception):
    pass


class Stock:
    def __init__(self, levels: dict[str, int]) -> None:
        self._levels = dict(levels)

    def reserve(self, sku: str, qty: int) -> None:
        if self._levels.get(sku, 0) < qty:
            raise OutOfStock(sku)
        self._levels[sku] -= qty

    def release(self, sku: str, qty: int) -> None:
        self._levels[sku] += qty

    def available(self, sku: str) -> int:
        return self._levels.get(sku, 0)
