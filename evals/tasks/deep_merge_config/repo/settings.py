def merge_config(base: dict, override: dict) -> dict:
    return {**base, **override}
