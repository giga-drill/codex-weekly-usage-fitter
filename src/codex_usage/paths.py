from __future__ import annotations

import os
from pathlib import Path


ENV_HOME = "CODEX_USAGE_HOME"


def usage_home(override: str | os.PathLike[str] | None = None) -> Path:
    raw = override or os.environ.get(ENV_HOME)
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".codex" / "usage-monitor"


def ensure_usage_dirs(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "spool").mkdir(parents=True, exist_ok=True)


def socket_path(home: Path) -> Path:
    return home / "collector.sock"


def db_path(home: Path) -> Path:
    return home / "usage.sqlite"


def spool_dir(home: Path) -> Path:
    return home / "spool"
