"""HTTP API package: FastAPI router, auth middleware, and the SSE event bus.

`main.py` (the composition root) wires this package's `router` and
`AuthMiddleware` into the app; nothing in this package builds the app
itself.
"""
