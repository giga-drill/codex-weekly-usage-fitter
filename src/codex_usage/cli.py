from __future__ import annotations

import argparse
import json
import shlex
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .collector import UsageCollector, enqueue_stop_event, run_daemon_with_scan
from .coverage_diagnostics import (
    DEFAULT_MISSING_LIMIT,
    DEFAULT_RECENT_WINDOW_HOURS,
    build_coverage_diagnostics,
)
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
    if args.command == "scan-transcripts":
        return _cmd_scan_transcripts(home, args)
    if args.command == "backfill-transcripts":
        return _cmd_backfill_transcripts(home, args)
    if args.command == "status":
        return _cmd_status(home, args)
    if args.command == "coverage":
        return _cmd_coverage(home, args)
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
    daemon.add_argument(
        "--scan-interval",
        type=float,
        default=10,
        help="seconds between transcript fallback scans",
    )

    scan = sub.add_parser(
        "scan-transcripts",
        help="scan recent Codex transcripts without waiting for hooks",
    )
    scan.add_argument(
        "--since-hours",
        type=float,
        default=48,
        help="scan transcripts modified in the last N hours",
    )
    scan.add_argument(
        "--no-app-server",
        action="store_true",
        help="disable account/rateLimits/read fallback",
    )

    backfill = sub.add_parser(
        "backfill-transcripts",
        help="explicitly backfill all local Codex transcripts across full history",
    )
    backfill.add_argument(
        "--with-app-server",
        action="store_true",
        help="allow app-server weekly fallback for transcripts missing rate_limits",
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

    coverage = sub.add_parser(
        "coverage",
        help="audit transcript coverage in raw and completed layers",
    )
    coverage.add_argument(
        "--codex-home",
        default=None,
        help="override Codex home (default: ~/.codex)",
    )
    coverage.add_argument(
        "--since-hours",
        type=float,
        default=DEFAULT_RECENT_WINDOW_HOURS,
        help="recent coverage window in hours",
    )
    coverage.add_argument(
        "--missing-limit",
        type=int,
        default=DEFAULT_MISSING_LIMIT,
        help="max recent missing examples to print",
    )
    coverage.add_argument("--json", action="store_true", help="print JSON")

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
    run_daemon_with_scan(
        home,
        use_app_server=not args.no_app_server,
        scan_interval_seconds=args.scan_interval,
    )
    return 0


def _cmd_scan_transcripts(home: Path, args: argparse.Namespace) -> int:
    collector = UsageCollector(home, delay_seconds=0, use_app_server=not args.no_app_server)
    try:
        inserted = collector.scan_recent_transcripts(
            since_seconds=max(0, args.since_hours) * 3600
        )
    finally:
        collector.close()
    print(f"Inserted {inserted} transcript sample(s).")
    return 0


def _cmd_backfill_transcripts(home: Path, args: argparse.Namespace) -> int:
    collector = UsageCollector(
        home,
        delay_seconds=0,
        use_app_server=bool(args.with_app_server),
    )
    try:
        stats = collector.scan_transcripts(
            since_seconds=None, rebuild_per_insert=False
        )
    finally:
        collector.close()
    print(
        "Considered "
        f"{stats.files_considered} transcript file(s), "
        f"{stats.turns_considered} transcript turn(s). "
        f"Inserted {stats.samples_inserted} transcript sample(s)."
    )
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


def _cmd_coverage(home: Path, args: argparse.Namespace) -> int:
    codex_home = (
        Path(args.codex_home).expanduser()
        if args.codex_home
        else (Path.home() / ".codex")
    )
    report = build_coverage_diagnostics(
        home=home,
        codex_home=codex_home,
        since_hours=max(0.0, float(args.since_hours)),
        missing_limit=max(0, int(args.missing_limit)),
    )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    print(_format_coverage(report))
    return 0


def _cmd_billing_stats(home: Path, args: argparse.Namespace) -> int:
    store = UsageStore(home)
    try:
        try:
            stats = store.billing_stats(
                billing_day=args.billing_day,
                period=args.period,
                timezone_name=args.timezone,
                debug=args.debug,
            )
        except RuntimeError as exc:
            print(f"Error: {exc}")
            return 1
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
    latest_conversation = status.get("latest_conversation_turn")
    fit = status.get("latest_fit")
    epoch = status.get("latest_epoch")
    model_effort_key = status.get("latest_model_effort_key") or {}
    model_effort_fit = status.get("latest_clean_model_effort_fit") or status.get(
        "latest_model_effort_fit"
    )
    model_effort_fits = status.get("model_effort_fits") or []
    mixed_events = status.get("latest_mixed_movement_events") or []
    lines = ["Codex weekly usage fitter"]
    lines.append(f"Home: {status['home']}")
    lines.append(
        f"Samples: {status['sample_count']} across {status['session_count']} sessions"
    )
    lines.append(
        f"Completed conversation turns: {status.get('conversation_turn_count', 0)}"
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
            f"({today['level']}, conversations {today.get('conversation_turn_count', today['sample_count'])})"
        )
        if today.get("error"):
            lines.append(
                f"Today usage warning: {today['error']} "
                f"(raw samples {today.get('raw_sample_count', 0)})"
            )
    if latest_conversation:
        lines.append(
            "Latest conversation turn: "
            f"session={latest_conversation.get('session_id') or '-'} "
            f"internal={latest_conversation.get('last_internal_turn_id') or '-'} "
            f"model={latest_conversation.get('model') or '-'} "
            f"effort={latest_conversation.get('reasoning_effort') or '-'} "
            f"delta={latest_conversation.get('token_delta')} "
            f"total={latest_conversation.get('token_total_end')}"
        )
    lines.append(
        "Latest raw sample (internal): "
        f"session={latest.get('session_id') or '-'} "
        f"turn={latest.get('turn_id') or '-'} "
        f"model={latest.get('model') or '-'} "
        f"effort={latest.get('reasoning_effort') or '-'} "
        f"delta={latest.get('token_delta')} "
        f"total={latest.get('token_total')}"
    )
    if model_effort_key:
        lines.append(
            "Current model/effort: "
            f"{model_effort_key.get('model') or 'unknown'}/"
            f"{model_effort_key.get('reasoning_effort') or 'unknown'}"
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
                f"observations {fit['sample_count']})"
            )
        external = "yes" if fit.get("external_usage_observed") else "no"
        lines.append(f"External usage observed: {external}")
    elif epoch:
        lines.append("Fit: waiting for more samples")

    if model_effort_fit:
        lines.append(
            "Latest clean model/effort estimate: "
            + _format_model_effort_fit(model_effort_fit)
        )
    elif model_effort_fits:
        lines.append("Model/effort fits:")
        for group in model_effort_fits[:3]:
            lines.append("  - " + _format_model_effort_fit(group))
    else:
        lines.append("Latest clean model/effort estimate: waiting for clean movement")

    if mixed_events:
        lines.append("Recent mixed movement observations:")
        for event in mixed_events[:3]:
            external_hint = (
                " [external-like]" if event.get("external_usage_observed") else ""
            )
            lines.append(
                "  - "
                f"{event.get('combination') or 'unknown'}: "
                f"+{event.get('percent_delta', 0):.3g}% "
                f"{_format_token_count(event.get('token_delta_total'))}, "
                f"{event.get('turn_count', 0)} conversation turns{external_hint}"
            )

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
    lines.append(f"Conversation turns: {period['turn_count']}")
    lines.append(
        "Avg / conversation: "
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
            f"{window['turn_count']} conv"
        )
        for day in window["days"]:
            lines.append(
                f"  {_format_period_date(day['start']):<6}   "
                f"+{day['usage_percent_delta']:.3g}%   "
                f"{_format_token_count(day['token_delta_total']):>9}   "
                f"{day['turn_count']} conv"
            )
    mixed_combinations = stats.get("mixed_movement_combinations") or []
    if mixed_combinations:
        lines.append("")
        lines.append("Mixed movement observations")
        for item in mixed_combinations:
            lines.append(
                f"{item['combination']}: +{item['percent_delta']:.3g}%   "
                f"{_format_token_count(item['token_delta_total'])}   "
                f"{item['turn_count']} conversation turns   {item['event_count']} events"
            )
    if debug:
        lines.append("")
        lines.append("Conversation turns used")
        for sample in stats.get("debug_samples", []):
            usage_start = sample.get("usage_percent_start")
            usage_end = sample.get("usage_percent_end")
            usage_start_text = f"{usage_start:.3g}%" if usage_start is not None else "--"
            usage_end_text = f"{usage_end:.3g}%" if usage_end is not None else "--"
            model = sample.get("model") or "-"
            effort = sample.get("reasoning_effort") or "-"
            lines.append(
                f"{sample['observed_at']}  weekly={usage_start_text}->{usage_end_text} "
                f"move=+{sample['usage_percent_delta']:.3g}% "
                f"delta={_format_token_count(sample['token_delta']):>9} "
                f"model={model}/{effort} turn={sample.get('conversation_turn_key') or sample.get('turn_id') or '-'}"
            )
        mixed_events = stats.get("mixed_movement_events") or []
        if mixed_events:
            lines.append("")
            lines.append("Mixed movement events")
            for event in mixed_events:
                external_hint = (
                    " external-like" if event.get("external_usage_observed") else ""
                )
                lines.append(
                    f"{event.get('observed_at_local') or event.get('observed_at')}  "
                    f"{event['combination']}  +{event['percent_delta']:.3g}%  "
                    f"delta={_format_token_count(event['token_delta_total'])}  "
                    f"conversation_turns={event['turn_count']}{external_hint}"
                )
    return "\n".join(lines)


def _format_coverage(report: dict[str, Any]) -> str:
    coverage = report.get("coverage") or {}
    all_history = coverage.get("all_history") or {}
    recent = coverage.get("recent_window") or {}
    examples = coverage.get("recent_missing_examples") or []

    def _window_lines(label: str, window: dict[str, Any]) -> list[str]:
        return [
            f"{label}:",
            "  "
            f"transcripts={window.get('transcript_files_discovered_count', 0)} "
            f"sessions={window.get('sessions_discovered_count', 0)} "
            f"with_token_count={window.get('with_token_count_count', 0)} "
            f"with_token_count_and_task_complete={window.get('with_token_count_and_task_complete_count', 0)}",
            "  "
            f"raw_layer: sessions={window.get('present_in_sessions_count', 0)} "
            f"samples={window.get('present_in_samples_count', 0)} "
            f"union={window.get('present_in_raw_observation_layer_count', 0)}",
            "  "
            "completed_conversation_turns="
            f"{window.get('present_in_completed_conversation_turns_count', 0)} "
            f"missing_from_sessions={window.get('missing_from_sessions_count', 0)} "
            "missing_from_conversation_turns="
            f"{window.get('missing_from_conversation_turns_count', 0)}",
        ]

    lines = [
        "Transcript coverage audit",
        f"Usage home: {report.get('home')}",
        f"Codex home: {report.get('codex_home')}",
        f"Recent window: last {report.get('recent_window_hours')} hour(s)",
        f"Missing examples limit: {report.get('missing_limit')}",
        "",
    ]
    lines.extend(_window_lines("All history", all_history))
    lines.append("")
    lines.extend(_window_lines("Recent window", recent))
    lines.append("")
    lines.append("Recent missing examples:")
    if not examples:
        lines.append("  (none)")
        return "\n".join(lines)
    for item in examples:
        lines.append(
            "  - "
            f"session_id={item.get('session_id') or '-'} "
            f"cwd={item.get('cwd') or '-'} "
            f"model={item.get('model') or '-'} "
            f"effort={item.get('reasoning_effort') or '-'} "
            f"last_timestamp={item.get('last_timestamp') or '-'} "
            f"missing_from_sessions={bool(item.get('missing_from_sessions'))} "
            "missing_from_conversation_turns="
            f"{bool(item.get('missing_from_conversation_turns'))} "
            f"path={item.get('transcript_path') or '-'}"
        )
    return "\n".join(lines)


def _format_model_effort_fit(fit: dict[str, Any]) -> str:
    label = f"{fit.get('model') or 'unknown'}/{fit.get('reasoning_effort') or 'unknown'}"
    tpp = fit.get("tokens_per_weekly_percent")
    if tpp is None:
        return (
            f"{label}: waiting for weekly percent movement "
            f"(confidence {fit['confidence']}, observations {fit['sample_count']})"
        )
    turns = fit.get("turns_per_weekly_percent")
    turn_text = f", conversations/1% {turns:.2g}" if turns is not None else ""
    return (
        f"{label}: {tpp:.0f} tokens per 1% weekly "
        f"(confidence {fit['confidence']}, observations {fit['sample_count']}{turn_text}, "
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
