from config import TAX_RATE
from pricing import apply_discount


def order_total(prices: list[float], coupon_percent: float = 0) -> float:
    undiscounted = sum(prices)
    subtotal = sum(apply_discount(price, coupon_percent) for price in prices)
    subtotal = apply_discount(subtotal, coupon_percent)
    return round(subtotal + undiscounted * TAX_RATE, 2)
