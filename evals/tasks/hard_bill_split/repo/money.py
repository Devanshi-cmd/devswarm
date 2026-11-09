def split(amount_cents: int, weights: list[int]) -> list[int]:
    total = sum(weights)
    return [round(amount_cents * weight / total) for weight in weights]
