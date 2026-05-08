from __future__ import annotations

import argparse
import json
import shlex
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .collector import enqueue_stop_event, run_daemon
from .paths import usage_home
from .store import UsageStore


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    home = usage_home(args.home)

    if args.command == "sample-stop":
        return _cmd_sample_stop(home, args)
    if args.command == "daemon":
        return _cmd_daemon(home, args)
    if args.command == "status":
        return _cmd_status(home, args)
    if args.command == "billing-stats":
        return _cmd_billing_stats(home, args)
    if args.command == "export":
        return _cmd_export(home, args)
    if args.command == "hook-config":
        return _cmd_hook_config(args)

    parser.print_help()
    return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codex-usage")
    parser.add_argument("--home", help="override usage home directory")
    sub = parser.add_subparsers(dest="command")

    daemon = sub.add_parser("daemon", help="run the local collector daemon")
    daemon.add_argument(
        "--no-app-server",
        action="store_true",
        help="disable account/rateLimits/read fallback",
    )

    sample = sub.add_parser("sample-stop", help="enqueue a Codex Stop hook event")
    sample.add_argument(
        "--timeout",
        type=float,
        default=0.2,
        help="socket send timeout in seconds",
    )

    status = sub.add_parser("status", help="show current weekly usage fit")
    status.add_argument("--json", action="store_true", help="print JSON")

    billing = sub.add_parser(
        "billing-stats",
        help="show token and usage stats for a billing period",
    )
    billing.add_argument(
        "--billing-day",
        type=int,
        required=True,
        help="day of month when the ChatGPT subscription renews",
    )
    billing.add_argument(
        "--period",
        choices=("current", "previous"),
        default="current",
        help="billing period to report",
    )
    billing.add_argument(
        "--timezone",
        default=None,
        help="IANA timezone for billing boundaries; defaults to local timezone",
    )
    billing.add_argument("--debug", action="store_true", help="print sample ledger")
    billing.add_argument("--json", action="store_true", help="print JSON")

    export = sub.add_parser("export", help="export raw samples")
    export.add_argument(
        "--format",
        choices=("jsonl", "csv"),
        default="jsonl",
        help="export format",
    )

    hook = sub.add_parser("hook-config", help="print Codex hook config snippet")
    hook.add_argument(
        "--command",
        dest="hook_command",
        help="command to place in the hook config; defaults to codex-usage",
    )

    return parser


def _cmd_sample_stop(home: Path, args: argparse.Namespace) -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            payload = {}
        enqueue_stop_event(home, payload, timeout_seconds=args.timeout)
    except Exception:
        # Stop hooks should never block a Codex turn. Deliberately stay silent.
        return 0
    return 0


def _cmd_daemon(home: Path, args: argparse.Namespace) -> int:
    run_daemon(home, use_app_server=not args.no_app_server)
    return 0


def _cmd_status(home: Path, args: argparse.Namespace) -> int:
    store = UsageStore(home)
    try:
        status = store.status()
    finally:
        store.close()

    if args.json:
        print(json.dumps(status, indent=2, sort_keys=True))
        return 0

    print(_format_status(status))
    return 0


def _cmd_billing_stats(home: Path, args: argparse.Namespace) -> int:
    store = UsageStore(home)
    try:
        stats = store.billing_stats(
            billing_day=args.billing_day,
            period=args.period,
            timezone_name=args.timezone,
            debug=args.debug,
        )
    finally:
        store.close()

    if args.json:
        print(json.dumps(stats, indent=2, sort_keys=True))
        return 0

    print(_format_billing_stats(stats, debug=args.debug))
    return 0


def _cmd_export(home: Path, args: argparse.Namespace) -> int:
    store = UsageStore(home)
    try:
        output = store.export_csv() if args.format == "csv" else store.export_jsonl()
    finally:
        store.close()
    sys.stdout.write(output)
    return 0


def _cmd_hook_config(args: argparse.Namespace) -> int:
    command = args.hook_command or _default_hook_command()
    hook_command = _toml_string(f"{command} sample-stop")
    print(
        f"""[features]
hooks = true

[hooks]

[[hooks.Stop]]
[[hooks.Stop.hooks]]
type = "command"
command = "{hook_command}"
timeout = 2
statusMessage = "recording Codex usage"
"""
    )
    return 0


def _default_hook_command() -> str:
    found = shutil.which("codex-usage")
    if found:
        return shlex.quote(found)
    repo_src = Path(__file__).resolve().parents[2] / "src"
    return (
        f"PYTHONPATH={shlex.quote(str(repo_src))} "
        f"{shlex.quote(sys.executable)} -m codex_usage"
    )


def _toml_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _format_status(status: dict[str, Any]) -> str:
    latest = status.get("latest_sample")
    fit = status.get("latest_fit")
    epoch = status.get("latest_epoch")
    model_effort_fit = status.get("latest_model_effort_fit")
    model_effort_fits = status.get("model_effort_fits") or []
    lines = ["Codex weekly usage fitter"]
    lines.append(f"Home: {status['home']}")
    lines.append(
        f"Samples: {status['sample_count']} across {status['session_count']} sessions"
    )

    if not latest:
        lines.append("No samples recorded yet.")
        return "\n".join(lines)

    weekly = latest.get("weekly_used_percent")
    if weekly is None:
        lines.append("Weekly usage: unknown")
    else:
        reset = _format_epoch_time(latest.get("weekly_resets_at"))
        source = latest.get("percent_source") or "unknown"
        lines.append(f"Weekly usage: {weekly:.3g}% used, resets {reset} ({source})")

    lines.append(f"Observed tokens, all time: {status['total_observed_tokens']}")
    if status.get("epoch_observed_tokens") is not None:
        lines.append(f"Observed tokens, current epoch: {status['epoch_observed_tokens']}")
    today = status.get("today_usage")
    if today:
        lines.append(
            "Today usage: "
            f"{today['used_percent_delta']:.3g}% "
            f"({today['level']}, samples {today['sample_count']})"
        )
    lines.append(
        "Latest sample: "
        f"session={latest.get('session_id') or '-'} "
        f"turn={latest.get('turn_id') or '-'} "
        f"model={latest.get('model') or '-'} "
        f"effort={latest.get('reasoning_effort') or '-'} "
        f"delta={latest.get('token_delta')} "
        f"total={latest.get('token_total')}"
    )

    if fit:
        tpp = fit.get("tokens_per_weekly_percent")
        if tpp is None:
            lines.append(
                "Fit: waiting for weekly percent movement "
                f"(confidence {fit['confidence']})"
            )
        else:
            lines.append(
                "Fit: "
                f"{tpp:.0f} tokens per 1% weekly "
                f"(confidence {fit['confidence']}, "
                f"samples {fit['sample_count']})"
            )
        external = "yes" if fit.get("external_usage_observed") else "no"
        lines.append(f"External usage observed: {external}")
    elif epoch:
        lines.append("Fit: waiting for more samples")

    if model_effort_fit:
        lines.append("Model/effort fit: " + _format_model_effort_fit(model_effort_fit))
    elif model_effort_fits:
        lines.append("Model/effort fits:")
        for group in model_effort_fits[:3]:
            lines.append("  - " + _format_model_effort_fit(group))

    return "\n".join(lines)


def _format_billing_stats(stats: dict[str, Any], debug: bool = False) -> str:
    period = stats["period"]
    lines = [stats["label"]]
    lines.append(
        f"{_format_period_date(period['start'])} - {_format_period_date(period['end'])} "
        f"{stats['timezone']}"
    )
    lines.append("")
    lines.append("Summary")
    lines.append(f"Usage: +{period['usage_percent_delta']:.3g}%")
    lines.append(f"Tokens: {_format_token_count(period['token_delta_total'])}")
    lines.append(f"Turns: {period['turn_count']}")
    lines.append(
        "Avg / turn: "
        + (
            _format_token_count(period["avg_tokens_per_turn"])
            if period["avg_tokens_per_turn"] is not None
            else "--"
        )
    )
    lines.append("")
    lines.append("Weekly windows")
    for window in stats["weekly_windows"]:
        lines.append(
            f"{_format_period_date(window['start'])} - "
            f"{_format_period_date(window['end'])}   "
            f"+{window['usage_percent_delta']:.3g}%   "
            f"{_format_token_count(window['token_delta_total'])}   "
            f"{window['turn_count']} turns"
        )
        for day in window["days"]:
            lines.append(
                f"  {_format_period_date(day['start']):<6}   "
                f"+{day['usage_percent_delta']:.3g}%   "
                f"{_format_token_count(day['token_delta_total']):>9}   "
                f"{day['turn_count']} turns"
            )
    if debug:
        lines.append("")
        lines.append("Samples used")
        for sample in stats.get("debug_samples", []):
            usage = sample["usage_percent"]
            usage_text = f"{usage:.3g}%" if usage is not None else "--"
            model = sample.get("model") or "-"
            effort = sample.get("reasoning_effort") or "-"
            lines.append(
                f"{sample['observed_at']}  weekly={usage_text:<6} "
                f"move=+{sample['usage_percent_delta']:.3g}% "
                f"delta={_format_token_count(sample['token_delta']):>9} "
                f"model={model}/{effort} turn={sample.get('turn_id') or '-'}"
            )
    return "\n".join(lines)


def _format_model_effort_fit(fit: dict[str, Any]) -> str:
    label = f"{fit.get('model') or 'unknown'}/{fit.get('reasoning_effort') or 'unknown'}"
    tpp = fit.get("tokens_per_weekly_percent")
    if tpp is None:
        return (
            f"{label}: waiting for weekly percent movement "
            f"(confidence {fit['confidence']}, samples {fit['sample_count']})"
        )
    turns = fit.get("turns_per_weekly_percent")
    turn_text = f", turns/1% {turns:.2g}" if turns is not None else ""
    return (
        f"{label}: {tpp:.0f} tokens per 1% weekly "
        f"(confidence {fit['confidence']}, samples {fit['sample_count']}{turn_text}, "
        f"percent {fit['percent_delta']:.3g})"
    )


def _format_epoch_time(value: Any) -> str:
    if value is None:
        return "unknown"
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return "unknown"
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="seconds")


def _format_period_date(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    return parsed.strftime("%b %d")


def _format_token_count(value: Any) -> str:
    if value is None:
        return "--"
    count = float(value)
    abs_count = abs(count)
    if abs_count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M tok"
    if abs_count >= 1_000:
        return f"{count / 1_000:.1f}k tok"
    return f"{count:.0f} tok"
