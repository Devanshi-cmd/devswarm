schedule() is unreliable

`schedule(tasks)` takes a dict mapping each task to the tasks it depends on, and returns an order to run them in. It has several problems:

1. If tasks form a cycle, it silently returns a partial list. It should raise `CycleError`, a new exception defined in scheduler.py that subclasses ValueError. The message must include the names of the tasks that could not be scheduled.
2. A dependency that isn't a key in `tasks` crashes with a bare KeyError. It should raise `UnknownTaskError`, also new in scheduler.py and a ValueError subclass, and the message must name the unknown dependency.
3. The order is not deterministic. Whenever several tasks are ready to run, the alphabetically first one must always go next.
