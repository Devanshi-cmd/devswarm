class Cart:
    def __init__(self, items=[]):
        self.items = items

    def add(self, item: str) -> None:
        self.items.append(item)

    def count(self) -> int:
        return len(self.items)
