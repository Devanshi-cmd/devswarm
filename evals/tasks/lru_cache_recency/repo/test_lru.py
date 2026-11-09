from lru import LRUCache


def test_get_and_put():
    cache = LRUCache(2)
    cache.put("a", 1)
    assert cache.get("a") == 1


def test_evicts_when_full():
    cache = LRUCache(1)
    cache.put("a", 1)
    cache.put("b", 2)
    assert cache.get("a") is None
