from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable
from typing import Any

import pytest
from mcp.server import MCPServer

from blender_mcp.runtime import create_authenticated_http_app

TOKEN = "test-token-" + ("a" * 32)


def _request(
    app: Any,
    path: str,
    *,
    method: str = "GET",
    headers: Iterable[tuple[bytes, bytes]] = (),
) -> tuple[int, dict[bytes, bytes], bytes]:
    sent: list[dict[str, Any]] = []
    received = False

    async def receive() -> dict[str, Any]:
        nonlocal received
        if not received:
            received = True
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "scheme": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "root_path": "",
        "headers": list(headers),
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 8765),
    }
    asyncio.run(app(scope, receive, send))
    start = next(message for message in sent if message["type"] == "http.response.start")
    body = b"".join(
        message.get("body", b"")
        for message in sent
        if message["type"] == "http.response.body"
    )
    return start["status"], dict(start["headers"]), body


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.20", "example.com"])
def test_http_factory_rejects_non_loopback_bind(host: str) -> None:
    with pytest.raises(ValueError, match="loopback"):
        create_authenticated_http_app(MCPServer("test"), host=host, token=TOKEN)


@pytest.mark.parametrize(
    "token",
    [
        "",
        "short",
        "contains whitespace " + ("a" * 32),
        "contains:separator" + ("a" * 32),
        "unicode-token-" + ("é" * 32),
    ],
)
def test_http_factory_requires_a_strong_header_safe_token(token: str) -> None:
    with pytest.raises(ValueError, match="token"):
        create_authenticated_http_app(
            MCPServer("test"),
            host="127.0.0.1",
            token=token,
        )


def test_every_http_route_requires_the_exact_bearer_token() -> None:
    app = create_authenticated_http_app(
        MCPServer("test"),
        host="127.0.0.1",
        token=TOKEN,
    )

    missing_status, missing_headers, _ = _request(app, "/health")
    wrong_status, _, _ = _request(
        app,
        "/health",
        headers=[(b"authorization", b"Bearer wrong-token")],
    )
    mcp_status, _, _ = _request(app, "/mcp")

    assert missing_status == 401
    assert wrong_status == 401
    assert mcp_status == 401
    assert missing_headers[b"www-authenticate"] == b"Bearer"


def test_authenticated_health_is_minimal_and_no_command_route_is_added() -> None:
    app = create_authenticated_http_app(
        MCPServer("test"),
        host="localhost",
        token=TOKEN,
    )
    auth = [(b"authorization", f"Bearer {TOKEN}".encode("ascii"))]

    health_status, health_headers, health_body = _request(
        app,
        "/health",
        headers=auth,
    )
    command_status, _, _ = _request(app, "/commands", headers=auth)

    assert health_status == 200
    assert health_headers[b"cache-control"] == b"no-store"
    assert json.loads(health_body) == {"status": "ok"}
    assert command_status == 404


def test_duplicate_authorization_headers_are_rejected() -> None:
    app = create_authenticated_http_app(
        MCPServer("test"),
        host="::1",
        token=TOKEN,
    )
    duplicate = [
        (b"authorization", f"Bearer {TOKEN}".encode("ascii")),
        (b"authorization", f"Bearer {TOKEN}".encode("ascii")),
    ]

    status, _, _ = _request(app, "/health", headers=duplicate)

    assert status == 401
