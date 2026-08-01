"""Authenticated loopback HTTP transport for an external MCP worker."""

from __future__ import annotations

import ipaddress
import re
import secrets
from typing import Any, Protocol

from starlette.requests import Request
from starlette.responses import JSONResponse

DEFAULT_MCP_PATH = "/mcp"
DEFAULT_HEALTH_PATH = "/health"
MINIMUM_TOKEN_BYTES = 32
BEARER_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9._~+/-]+=*$")


class HTTPServerProtocol(Protocol):
    """Public MCP SDK methods used by the transport factory."""

    def custom_route(
        self,
        path: str,
        methods: list[str],
        name: str | None = None,
        include_in_schema: bool = True,
    ) -> Any: ...

    def streamable_http_app(self, **kwargs: Any) -> Any: ...


def validate_loopback_host(host: str) -> str:
    """Return a safe bind host or reject any externally reachable address."""

    if not isinstance(host, str) or not host:
        raise ValueError("HTTP host must be a loopback address")
    if host.lower() == "localhost":
        return "localhost"
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError("HTTP host must be a loopback address") from exc
    if not address.is_loopback:
        raise ValueError("HTTP host must be a loopback address")
    return address.compressed


def _validate_token(token: str) -> bytes:
    if not isinstance(token, str):
        raise ValueError("HTTP bearer token must be a string")
    try:
        encoded = token.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("HTTP bearer token must contain only ASCII characters") from exc
    if len(encoded) < MINIMUM_TOKEN_BYTES:
        raise ValueError(
            f"HTTP bearer token must contain at least {MINIMUM_TOKEN_BYTES} bytes"
        )
    if not BEARER_TOKEN_PATTERN.fullmatch(token):
        raise ValueError("HTTP bearer token contains unsupported characters")
    return encoded


def _validate_route_path(path: str, *, name: str) -> str:
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or path.startswith("//")
        or "?" in path
        or "#" in path
    ):
        raise ValueError(f"{name} must be an absolute URL path")
    return path


class BearerTokenMiddleware:
    """Small ASGI middleware requiring one exact bearer credential."""

    def __init__(self, app: Any, token: str) -> None:
        self.app = app
        self._token = _validate_token(token)

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Any,
        send: Any,
    ) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        authorization_values = [
            value
            for name, value in scope.get("headers", ())
            if name.lower() == b"authorization"
        ]
        if len(authorization_values) != 1 or not self._authorized(
            authorization_values[0]
        ):
            response = JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={
                    "WWW-Authenticate": "Bearer",
                    "Cache-Control": "no-store",
                },
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)

    def _authorized(self, raw_header: bytes) -> bool:
        try:
            header = raw_header.decode("ascii")
        except UnicodeDecodeError:
            return False
        scheme, separator, credential = header.partition(" ")
        if (
            not separator
            or scheme.lower() != "bearer"
            or not credential
            or any(character.isspace() for character in credential)
        ):
            return False
        return secrets.compare_digest(credential.encode("utf-8"), self._token)


def create_authenticated_http_app(
    server: HTTPServerProtocol,
    *,
    host: str,
    token: str,
    mcp_path: str = DEFAULT_MCP_PATH,
    health_path: str = DEFAULT_HEALTH_PATH,
    max_request_body_size: int = 1_048_576,
) -> BearerTokenMiddleware:
    """Build the SDK Streamable HTTP app behind mandatory bearer auth."""

    safe_host = validate_loopback_host(host)
    _validate_token(token)
    safe_mcp_path = _validate_route_path(mcp_path, name="mcp_path")
    safe_health_path = _validate_route_path(health_path, name="health_path")
    if safe_mcp_path == safe_health_path:
        raise ValueError("mcp_path and health_path must be different")
    if (
        isinstance(max_request_body_size, bool)
        or not isinstance(max_request_body_size, int)
        or not 1 <= max_request_body_size <= 16_777_216
    ):
        raise ValueError("max_request_body_size must be from 1 to 16777216 bytes")

    @server.custom_route(
        safe_health_path,
        methods=["GET"],
        name="blender_mcp_health",
        include_in_schema=False,
    )
    async def health(_: Request) -> JSONResponse:
        return JSONResponse(
            {"status": "ok"},
            headers={"Cache-Control": "no-store"},
        )

    app = server.streamable_http_app(
        streamable_http_path=safe_mcp_path,
        stateless_http=False,
        max_request_body_size=max_request_body_size,
        host=safe_host,
    )
    return BearerTokenMiddleware(app, token)


def run_authenticated_http_server(
    server: HTTPServerProtocol,
    *,
    host: str,
    port: int,
    token: str,
    mcp_path: str = DEFAULT_MCP_PATH,
    health_path: str = DEFAULT_HEALTH_PATH,
    max_request_body_size: int = 1_048_576,
    log_level: str = "info",
) -> None:
    """Serve an authenticated worker without accepting commands or shell hooks."""

    safe_host = validate_loopback_host(host)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65_535:
        raise ValueError("HTTP port must be from 1 to 65535")
    app = create_authenticated_http_app(
        server,
        host=safe_host,
        token=token,
        mcp_path=mcp_path,
        health_path=health_path,
        max_request_body_size=max_request_body_size,
    )

    import uvicorn

    uvicorn.run(
        app,
        host=safe_host,
        port=port,
        log_level=log_level,
    )
