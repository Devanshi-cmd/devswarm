from stock import Stock


class OrderError(Exception):
    pass


class Orders:
    def __init__(self, stock: Stock) -> None:
        self.stock = stock
        self._orders: dict[int, dict] = {}
        self._next_id = 1

    def place(self, lines: dict[str, int]) -> int:
        for sku, qty in lines.items():
            self.stock.reserve(sku, qty)
        order_id = self._next_id
        self._next_id += 1
        self._orders[order_id] = {"lines": dict(lines), "status": "open"}
        return order_id

    def ship(self, order_id: int) -> None:
        self._orders[order_id]["status"] = "shipped"

    def cancel(self, order_id: int) -> None:
        order = self._orders[order_id]
        for sku, qty in order["lines"].items():
            self.stock.release(sku, qty)
        order["status"] = "cancelled"
