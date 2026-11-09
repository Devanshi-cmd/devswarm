def page_count(total: int, size: int) -> int:
    return total // size


def get_page(items: list, page: int, size: int) -> list:
    start = (page - 1) * size
    return items[start : start + size]
