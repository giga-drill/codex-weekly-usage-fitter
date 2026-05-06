from __future__ import annotations

import json
import os
import socket
import socketserver
import time
import uuid
from pathlib import Path
from typing import Any

from .app_server import AppServerClient
from .paths import ensure_usage_dirs, socket_path, spool_dir
from .store import UsageStore, utc_now_iso
from .transcript import TranscriptSnapshot, parse_transcript


class UsageCollector:
    def __init__(
        self,
        home: Path,
        *,
        delay_seconds: float = 0.25,
        app_server: AppServerClient | None = None,
        use_app_server: bool = True,
    ) -> None:
        ensure_usage_dirs(home)
        self.home = home
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
        if transcript_path:
            snapshot = parse_transcript(str(transcript_path))
        else:
            snapshot = TranscriptSnapshot(path="", error="missing transcript_path")

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


class _UsageRequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        line = self.rfile.readline(1_000_000)
        if not line:
            return
        try:
            event = json.loads(line.decode("utf-8"))
        except json.JSONDecodeError:
            self.wfile.write(b"ERR invalid-json\n")
            return
        if not isinstance(event, dict):
            self.wfile.write(b"ERR invalid-event\n")
            return
        inserted = self.server.collector.process_event(event)  # type: ignore[attr-defined]
        self.server.collector.drain_spool()  # type: ignore[attr-defined]
        self.wfile.write(b"OK inserted\n" if inserted else b"OK duplicate\n")


class _UnixUsageServer(socketserver.UnixStreamServer):
    def __init__(self, path: str, collector: UsageCollector) -> None:
        self.collector = collector
        super().__init__(path, _UsageRequestHandler)


def run_daemon(home: Path, *, use_app_server: bool = True) -> None:
    ensure_usage_dirs(home)
    sock = socket_path(home)
    if sock.exists():
        sock.unlink()
    collector = UsageCollector(home, use_app_server=use_app_server)
    collector.drain_spool()
    try:
        with _UnixUsageServer(str(sock), collector) as server:
            server.serve_forever(poll_interval=0.5)
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
