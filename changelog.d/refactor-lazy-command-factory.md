### Changed — one factory builds the parsers' lazy command handlers

Thirteen command handlers in eleven `claudlobby/commands/_*_parsers.py` modules each wrapped `execute` around a lambda that imported one command module and called its `dispatch(args)`. `claudlobby.commands.lazy(module, request_id=False)` now builds them. A command module is still imported only when its route runs, and every command's result is unchanged: the handlers that put the route's `--request-id` on the result pass `request_id=True`, and `request show` still leaves its positional request ID off the result.
