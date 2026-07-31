"""Stable application errors exposed at the MCP boundary."""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    INVALID_REQUEST = "INVALID_REQUEST"
    INVALID_PATH = "INVALID_PATH"
    PATH_OUTSIDE_PROJECT = "PATH_OUTSIDE_PROJECT"
    PATH_NOT_FOUND = "PATH_NOT_FOUND"
    PATH_ALREADY_EXISTS = "PATH_ALREADY_EXISTS"
    UNSUPPORTED_FILE_TYPE = "UNSUPPORTED_FILE_TYPE"
    REQUEST_TOO_LARGE = "REQUEST_TOO_LARGE"
    BLENDER_NOT_FOUND = "BLENDER_NOT_FOUND"
    BLENDER_TIMEOUT = "BLENDER_TIMEOUT"
    BLENDER_CRASHED = "BLENDER_CRASHED"
    BLENDER_FAILED = "BLENDER_FAILED"
    ADAPTER_INVALID_RESPONSE = "ADAPTER_INVALID_RESPONSE"
    ARTIFACT_MISSING = "ARTIFACT_MISSING"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class BlenderMCPError(Exception):
    """Expected failure with a stable public error code."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
