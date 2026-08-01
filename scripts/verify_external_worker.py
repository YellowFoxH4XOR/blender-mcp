"""Run the real template-to-MP4 workflow through the HTTP MCP worker."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from uuid import uuid4

import anyio
import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

TERMINAL_STATES = {"succeeded", "failed", "cancelled", "orphaned"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:9876/mcp")
    parser.add_argument(
        "--token-file",
        type=Path,
        default=Path.home() / ".config/blender-mcp/worker-token",
    )
    parser.add_argument("--timeout", type=float, default=180)
    return parser.parse_args()


async def call(
    session: ClientSession,
    name: str,
    arguments: dict[str, object],
) -> dict[str, object]:
    response = await session.call_tool(name, arguments)
    if response.is_error or response.structured_content is None:
        raise RuntimeError(f"{name} failed: {response}")
    envelope = response.structured_content
    if not envelope.get("ok"):
        raise RuntimeError(f"{name} failed: {envelope.get('error')}")
    result = envelope.get("result")
    if not isinstance(result, dict):
        raise RuntimeError(f"{name} returned no result")
    return result


async def run(args: argparse.Namespace) -> None:
    token = args.token_file.expanduser().read_text(encoding="utf-8").strip()
    run_id = uuid4().hex[:12]
    scene = f"scenes/worker-{run_id}.blend"
    preview = f"previews/worker-{run_id}.png"
    output = f"renders/worker-{run_id}.mp4"
    timeout = httpx2.Timeout(args.timeout)
    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {token}"},
        timeout=timeout,
    ) as client:
        async with streamable_http_client(
            args.url,
            http_client=client,
        ) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                await call(
                    session,
                    "create_scene_from_template",
                    {
                        "template_id": "character-stage-v1",
                        "destination": scene,
                    },
                )
                inspected = await call(
                    session,
                    "inspect_scene",
                    {"scene_path": scene},
                )
                await call(
                    session,
                    "apply_scene_transaction",
                    {
                        "scene_path": scene,
                        "expected_revision": inspected["scene_revision"],
                        "transaction_id": f"txn_{run_id}",
                        "operations": [
                            {
                                "op": "set_transform",
                                "object_id": "fixture_cube_v1",
                                "location": [0.5, 0.0, 0.0],
                            }
                        ],
                    },
                )
                validation = await call(
                    session,
                    "validate_scene",
                    {"scene_path": scene},
                )
                if not validation.get("ready_for_render"):
                    raise RuntimeError(f"scene is not render-ready: {validation}")
                await call(
                    session,
                    "render_preview",
                    {
                        "scene_path": scene,
                        "output_path": preview,
                        "max_width": 96,
                        "max_height": 64,
                    },
                )
                started = await call(
                    session,
                    "start_render",
                    {
                        "scene_path": scene,
                        "output": output,
                        "timeout_seconds": int(args.timeout),
                        "idempotency_key": f"worker-e2e-{run_id}",
                    },
                )
                job_id = str(started["job_id"])
                with anyio.fail_after(args.timeout):
                    while True:
                        job = await call(
                            session,
                            "get_job",
                            {"job_id": job_id},
                        )
                        state = str(job["state"])
                        if state in TERMINAL_STATES:
                            break
                        await anyio.sleep(0.25)
                if state != "succeeded":
                    raise RuntimeError(f"render job ended as {state}: {job}")
                print(
                    json.dumps(
                        {
                            "state": state,
                            "job_id": job_id,
                            "scene": scene,
                            "preview": preview,
                            "output": output,
                        },
                        indent=2,
                    )
                )


if __name__ == "__main__":
    anyio.run(run, parse_args())
