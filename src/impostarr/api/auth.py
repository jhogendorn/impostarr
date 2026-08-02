"""Identity attribution middleware.

Not authentication in the security sense — per spec, auth here is identity
*attribution* only: every resolved identity (trusted-header value, matched
API key name, or `anon`) has full admin rights. The middleware's job is to
name the requester (for the audit log and for `request.state.identity`,
which routes use to build worker ids like `api-<identity>`) and to enforce
an optional group allowlist gate.

Resolution order: trusted header value (if `Settings.auth.trusted_header`
is configured and present) > `X-Api-Key` match against
`Settings.auth.api_keys` > `anon`.

Group gate: only active when both `Settings.auth.group_header` and
`Settings.auth.required_group` are configured. The named header is read as
a comma-separated list of group names; a request whose list doesn't
contain the required group gets 403. `/api/v1/healthz` is exempt (health
checks must not depend on auth infrastructure).
"""

from __future__ import annotations

import logging

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from impostarr.config import Settings

logger = logging.getLogger(__name__)

HEALTHZ_PATH = "/api/v1/healthz"


def _resolve_identity(request: Request, settings: Settings) -> str:
    auth = settings.auth
    if auth.trusted_header:
        value = request.headers.get(auth.trusted_header)
        if value:
            return value
    api_key = request.headers.get("X-Api-Key")
    if api_key:
        for entry in auth.api_keys:
            if entry.key == api_key:
                return entry.name
    return "anon"


class AuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        super().__init__(app)
        self.settings = settings

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        identity = _resolve_identity(request, self.settings)
        request.state.identity = identity

        if request.url.path != HEALTHZ_PATH:
            auth = self.settings.auth
            if auth.group_header and auth.required_group:
                raw = request.headers.get(auth.group_header, "")
                groups = [g.strip() for g in raw.split(",") if g.strip()]
                if auth.required_group not in groups:
                    return JSONResponse(
                        {"detail": "forbidden: missing required group"}, status_code=403
                    )

        if request.method == "POST":
            logger.info(
                "audit: identity=%s method=%s path=%s", identity, request.method, request.url.path
            )

        return await call_next(request)
