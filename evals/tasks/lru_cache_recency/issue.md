LRUCache evicts the wrong entries

`LRUCache` doesn't behave like a least-recently-used cache:

- `get` doesn't count as using an entry, so a key that was just read can still be evicted first.
- `put` on a key that already exists evicts another entry, even though the cache isn't growing. Updating a key should also count as using it.
