from __future__ import annotations

import json
import os
import socket
import socketserver
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .app_server import AppServerClient
from .paths import ensure_usage_dirs, socket_path, spool_dir
from .store import UsageStore, utc_now_iso
from .transcript import TranscriptSnapshot, parse_transcript


@dataclass(frozen=True)
class TranscriptTurn:
    session_id: str | None
    turn_id: str | None
    transcript_path: Path
    cwd: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None


class UsageCollector:
    def __init__(
        self,
        home: Path,
        *,
        delay_seconds: float = 0.25,
        app_server: AppServerClient | None = None,
        use_app_server: bool = True,
        codex_home: Path | None = None,
    ) -> None:
        ensure_usage_dirs(home)
        self.home = home
        self.codex_home = codex_home or (Path.home() / ".codex")
        self.delay_seconds = delay_seconds
        self.store = UsageStore(home)
        self.app_server = app_server if app_server is not None else AppServerClient()
        self.use_app_server = use_app_server

    def close(self) -> None:
        self.store.close()
        self.app_server.close()

    def process_event(self, event: dict[str, Any]) -> bool:
        if self.delay_seconds > 0:
            time.sleep(self.delay_seconds)

        transcript_path = event.get("transcript_path")
        if not transcript_path:
            return False

        turn_id = event.get("turn_id")
        snapshot = parse_transcript(
            str(transcript_path),
            turn_id=str(turn_id) if turn_id is not None else None,
        )

        fallback_weekly = None
        if snapshot.weekly_limit is None and self.use_app_server:
            fallback_weekly = self.app_server.read_weekly_limit()

        return self.store.record_sample(event, snapshot, fallback_weekly)

    def drain_spool(self) -> int:
        count = 0
        for path in sorted(spool_dir(self.home).glob("*.jsonl")):
            try:
                with path.open("r", encoding="utf-8") as handle:
                    for line in handle:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(event, dict):
                            self.process_event(event)
                            count += 1
                path.unlink()
            except OSError:
                continue
        return count

    def scan_recent_transcripts(self, *, since_seconds: float = 48 * 3600) -> int:
        sessions_dir = self.codex_home / "sessions"
        if not sessions_dir.exists():
            return 0

        cutoff = time.time() - since_seconds
        count = 0
        candidates = [
            path
            for path in sessions_dir.rglob("rollout-*.jsonl")
            if _safe_mtime(path) >= cutoff
        ]
        for path in sorted(candidates, key=lambda item: (_safe_mtime(item), str(item))):
            for turn in _transcript_turns(path):
                event = {
                    "session_id": turn.session_id,
                    "turn_id": turn.turn_id,
                    "transcript_path": str(turn.transcript_path),
                    "model": turn.model,
                    "reasoning_effort": turn.reasoning_effort,
                    "cwd": turn.cwd,
                    "received_at": utc_now_iso(),
                    "source": "transcript_scan",
                }
                if self._record_event(event):
                    count += 1
        return count

    def _record_event(self, event: dict[str, Any]) -> bool:
        transcript_path = event.get("transcript_path")
        if not transcript_path:
            return False

        turn_id = event.get("turn_id")
        snapshot = parse_transcript(
            str(transcript_path),
            turn_id=str(turn_id) if turn_id is not None else None,
        )
        if snapshot.error:
            return False

        fallback_weekly = None
        if snapshot.weekly_limit is None and self.use_app_server:
            fallback_weekly = self.app_server.read_weekly_limit()

        return self.store.record_sample(event, snapshot, fallback_weekly)


class _UsageRequestHandler(socketserver.StreamRequestHandler):
    def _write_response(self, payload: bytes) -> None:
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def handle(self) -> None:
        line = self.rfile.readline(1_000_000)
        if not line:
            return
        try:
            event = json.loads(line.decode("utf-8"))
        except json.JSONDecodeError:
            self._write_response(b"ERR invalid-json\n")
            return
        if not isinstance(event, dict):
            self._write_response(b"ERR invalid-event\n")
            return
        inserted = self.server.collector.process_event(event)  # type: ignore[attr-defined]
        self.server.collector.drain_spool()  # type: ignore[attr-defined]
        self._write_response(b"OK inserted\n" if inserted else b"OK duplicate\n")


class _UnixUsageServer(socketserver.UnixStreamServer):
    def __init__(self, path: str, collector: UsageCollector) -> None:
        self.collector = collector
        super().__init__(path, _UsageRequestHandler)


def run_daemon(home: Path, *, use_app_server: bool = True) -> None:
    run_daemon_with_scan(home, use_app_server=use_app_server)


def run_daemon_with_scan(
    home: Path,
    *,
    use_app_server: bool = True,
    scan_interval_seconds: float = 10,
) -> None:
    ensure_usage_dirs(home)
    sock = socket_path(home)
    if sock.exists():
        sock.unlink()
    collector = UsageCollector(home, use_app_server=use_app_server)
    collector.drain_spool()
    collector.scan_recent_transcripts()
    try:
        with _UnixUsageServer(str(sock), collector) as server:
            server.timeout = 0.5
            last_scan = time.monotonic()
            while True:
                server.handle_request()
                now = time.monotonic()
                if now - last_scan >= scan_interval_seconds:
                    collector.drain_spool()
                    collector.scan_recent_transcripts()
                    last_scan = now
    finally:
        collector.close()
        try:
            sock.unlink()
        except OSError:
            pass


def enqueue_stop_event(
    home: Path,
    event: dict[str, Any],
    *,
    timeout_seconds: float = 0.2,
) -> bool:
    ensure_usage_dirs(home)
    event = _normalize_event(event)
    payload = json.dumps(event, separators=(",", ":")).encode("utf-8") + b"\n"
    sock = socket_path(home)

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout_seconds)
            client.connect(str(sock))
            client.sendall(payload)
            try:
                client.recv(128)
            except socket.timeout:
                pass
            return True
    except OSError:
        _write_spool(home, payload)
        return False


def _normalize_event(event: dict[str, Any]) -> dict[str, Any]:
    normalized = {
        "session_id": event.get("session_id"),
        "turn_id": event.get("turn_id"),
        "transcript_path": event.get("transcript_path"),
        "model": event.get("model"),
        "reasoning_effort": event.get("reasoning_effort")
        or event.get("reasoningEffort")
        or event.get("effort"),
        "cwd": event.get("cwd"),
        "received_at": utc_now_iso(),
    }
    for key in ("permission_mode", "stop_hook_active"):
        if key in event:
            normalized[key] = event[key]
    return normalized


def _write_spool(home: Path, payload: bytes) -> None:
    directory = spool_dir(home)
    directory.mkdir(parents=True, exist_ok=True)
    name = f"{time.time_ns()}-{os.getpid()}-{uuid.uuid4().hex}.jsonl"
    path = directory / name
    with path.open("wb") as handle:
        handle.write(payload)


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0


def _transcript_turns(path: Path) -> list[TranscriptTurn]:
    session_id: str | None = None
    cwd: str | None = None
    turns: list[TranscriptTurn] = []
    active_turn: dict[str, Any] | None = None
    active_has_token_count = False

    def finish_turn() -> None:
        nonlocal active_turn, active_has_token_count
        if not active_turn or not active_has_token_count:
            active_turn = None
            active_has_token_count = False
            return
        turn_id = _string_or_none(active_turn.get("turn_id"))
        if session_id is not None and turn_id is not None and turn_id < session_id:
            active_turn = None
            active_has_token_count = False
            return
        turns.append(
            TranscriptTurn(
                session_id=session_id,
                turn_id=turn_id,
                transcript_path=path,
                cwd=cwd,
                model=_string_or_none(active_turn.get("model")),
                reasoning_effort=_turn_effort(active_turn),
            )
        )
        active_turn = None
        active_has_token_count = False

    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    envelope = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(envelope, dict):
                    continue
                payload = envelope.get("payload")
                if envelope.get("type") == "session_meta" and isinstance(payload, dict):
                    if session_id is None:
                        session_id = _string_or_none(payload.get("id"))
                        cwd = _string_or_none(payload.get("cwd"))
                    continue
                if envelope.get("type") == "turn_context" and isinstance(payload, dict):
                    finish_turn()
                    active_turn = payload
                    active_has_token_count = False
                    continue
                if envelope.get("type") != "event_msg" or not isinstance(payload, dict):
                    continue
                if payload.get("type") in {"token_count", "TokenCount"}:
                    active_has_token_count = True
        finish_turn()
    except OSError:
        return []

    return turns


def _string_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _turn_effort(payload: dict[str, Any]) -> str | None:
    effort = _string_or_none(
        payload.get("effort")
        or payload.get("reasoning_effort")
        or payload.get("reasoningEffort")
    )
    if effort is not None:
        return effort
    collaboration = payload.get("collaboration_mode")
    if isinstance(collaboration, dict):
        settings = collaboration.get("settings")
        if isinstance(settings, dict):
            return _string_or_none(
                settings.get("reasoning_effort")
                or settings.get("reasoningEffort")
                or settings.get("effort")
            )
    return None
