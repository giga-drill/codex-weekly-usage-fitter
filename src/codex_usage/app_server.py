from __future__ import annotations

import json
import select
import subprocess
import time
from dataclasses import dataclass
from typing import Any

from .transcript import WeeklyLimit


@dataclass
class AppServerError(Exception):
    message: str

    def __str__(self) -> str:
        return self.message


class AppServerClient:
    def __init__(self, command: list[str] | None = None, timeout: float = 5.0) -> None:
        self.command = command or ["codex", "app-server"]
        self.timeout = timeout
        self._proc: subprocess.Popen[str] | None = None
        self._next_id = 1

    def close(self) -> None:
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None

    def read_weekly_limit(self) -> WeeklyLimit | None:
        try:
            self._ensure_started()
            response = self._request("account/rateLimits/read", params={})
        except (OSError, AppServerError):
            self.close()
            return None

        result = response.get("result")
        if not isinstance(result, dict):
            return None
        return _extract_weekly_limit(result)

    def _ensure_started(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
        self.close()
        self._proc = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        init_id = 0
        self._send(
            {
                "id": init_id,
                "method": "initialize",
                "params": {
                    "clientInfo": {
                        "name": "codex_usage_fitter",
                        "title": "Codex Usage Fitter",
                        "version": "0.1.0",
                    }
                },
            }
        )
        self._read_response(init_id)
        self._send({"method": "initialized", "params": {}})

    def _request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        payload: dict[str, Any] = {"id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        self._send(payload)
        return self._read_response(request_id)

    def _send(self, payload: dict[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise AppServerError("app-server is not running")
        self._proc.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
        self._proc.stdin.flush()

    def _read_response(self, request_id: int) -> dict[str, Any]:
        if self._proc is None or self._proc.stdout is None:
            raise AppServerError("app-server is not running")

        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AppServerError("app-server response timed out")
            ready, _, _ = select.select([self._proc.stdout], [], [], remaining)
            if not ready:
                raise AppServerError("app-server response timed out")
            line = self._proc.stdout.readline()
            if not line:
                raise AppServerError("app-server closed stdout")
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(message, dict):
                continue
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise AppServerError("app-server returned an error")
            return message


def _first(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _extract_weekly_limit(result: dict[str, Any]) -> WeeklyLimit | None:
    rate_limits = _first(result, "rateLimitsByLimitId", "rate_limits_by_limit_id")
    codex_limits = None
    if isinstance(rate_limits, dict):
        codex_limits = rate_limits.get("codex")

    if not isinstance(codex_limits, dict):
        codex_limits = _first(result, "rateLimits", "rate_limits")
    if not isinstance(codex_limits, dict):
        return None

    secondary = codex_limits.get("secondary")
    if not isinstance(secondary, dict):
        return None

    used_percent = _as_float(_first(secondary, "usedPercent", "used_percent"))
    if used_percent is None:
        return None

    return WeeklyLimit(
        used_percent=used_percent,
        resets_at=_as_int(_first(secondary, "resetsAt", "resets_at")),
        window_minutes=_as_int(
            _first(
                secondary,
                "windowDurationMins",
                "window_duration_mins",
                "windowMinutes",
                "window_minutes",
            )
        ),
        source="app_server",
    )
