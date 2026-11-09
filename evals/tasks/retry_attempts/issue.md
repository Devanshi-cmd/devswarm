retry makes too few attempts and hides the error

`retry(fn, attempts=3)` only calls `fn` twice. It should call it up to `attempts` times. If every attempt fails, it should raise the exception from the last attempt, but right now it returns None.
