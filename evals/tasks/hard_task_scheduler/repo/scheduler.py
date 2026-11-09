from graph import dependents


def schedule(tasks: dict[str, list[str]]) -> list[str]:
    remaining = {name: len(deps) for name, deps in tasks.items()}
    ready = [name for name, count in remaining.items() if count == 0]
    order = []
    while ready:
        name = ready.pop()
        order.append(name)
        for child in dependents(tasks)[name]:
            remaining[child] -= 1
            if remaining[child] == 0:
                ready.append(child)
    return order
