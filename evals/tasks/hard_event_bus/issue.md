EventBus misbehaves when handlers change subscriptions

Several problems with `EventBus`:

1. When a handler unsubscribes itself during `publish`, the next handler is skipped. Every handler subscribed when `publish` starts should run, with the exception in point 3.
2. A handler subscribed during a `publish` must not be called until the next `publish`.
3. If a handler unsubscribes a different handler that hasn't run yet in the same `publish`, that handler must not be called.
4. Handlers with a higher `priority` must run first. Handlers with equal priority run in the order they subscribed. Right now the order is backwards.
5. An exception from one handler stops the rest. All handlers should still run, and after they finish `publish` should raise `PublishError`, a new exception, whose `errors` attribute is a list of the exceptions in the order they happened.
6. Calling an unsubscribe function twice crashes. The second call should do nothing.
7. The same handler may be subscribed twice, and each subscription counts separately, with its own unsubscribe.
