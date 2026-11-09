def dependents(tasks: dict[str, list[str]]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {name: [] for name in tasks}
    for name, deps in tasks.items():
        for dep in deps:
            result[dep].append(name)
    return result
