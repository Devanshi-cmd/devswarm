class EventBus:
    def __init__(self):
        self._handlers = {}

    def subscribe(self, event, handler, priority=0):
        handlers = self._handlers.setdefault(event, [])
        entry = (priority, handler)
        handlers.append(entry)
        handlers.sort(key=lambda item: item[0])

        def unsubscribe():
            handlers.remove(entry)

        return unsubscribe

    def publish(self, event, payload=None):
        for _, handler in self._handlers.get(event, []):
            handler(payload)
