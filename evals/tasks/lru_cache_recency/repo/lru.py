from collections import OrderedDict


class LRUCache:
    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self._data: OrderedDict = OrderedDict()

    def get(self, key):
        return self._data.get(key)

    def put(self, key, value) -> None:
        if len(self._data) >= self.capacity:
            self._data.popitem(last=False)
        self._data[key] = value
