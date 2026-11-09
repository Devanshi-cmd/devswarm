def retry(fn, attempts: int = 3):
    for _ in range(attempts - 1):
        try:
            return fn()
        except Exception:
            pass
    return None
