# Codex Weekly Usage Fitter

Current release: `v0.1.0`

English | [中文](#中文说明)

Codex Weekly Usage Fitter is a small local monitor for Codex usage. It records
token usage from your local Codex conversations, reads the weekly usage
percentage reported by Codex, and estimates how many tokens or turns correspond
to 1% of your weekly usage.

It is useful when you want a clearer answer to questions like:

- How much weekly Codex usage have I used?
- How many tokens did the last turn consume?
- Which model and reasoning effort did that turn use?
- For my current model and reasoning-effort group, roughly how many turns or
  tokens equal 1% of weekly usage?

The project also includes a small native macOS floating widget that can stay on
your desktop and refresh the latest usage numbers every few seconds.

## Supported Platforms

Currently supported:

- macOS with Codex Desktop or Codex CLI local session logs.
- Python 3.11 or newer for the collector and CLI.
- macOS AppKit for the floating desktop widget.

Not currently supported:

- Windows or Linux desktop widgets.
- Browser-only Codex usage that does not appear in local Codex session logs.
- Usage from other machines, except as an unattributed movement in the weekly
  usage percentage.

## How It Works

The monitor runs entirely on your machine. It uses:

- Codex `Stop` hooks to collect one sample after each turn.
- Local Codex transcript files to read token counts, model, reasoning effort,
  and weekly rate-limit snapshots.
- `codex app-server` as a fallback source for weekly usage when the transcript
  does not contain a fresh weekly snapshot.
- A local SQLite database at `~/.codex/usage-monitor/usage.sqlite`.

It does not patch Codex or send your transcripts anywhere.

## Quick Start

Clone the repo and install the local CLI:

```bash
git clone <repo-url>
cd "codex usage"
python3 -m pip install -e .
```

Print the Codex hook config:

```bash
codex-usage hook-config
```

Add the printed snippet to your Codex config file:

```bash
open ~/.codex/config.toml
```

Then start the local collector daemon in a terminal:

```bash
codex-usage daemon
```

After your next Codex turn finishes, check the collected usage:

```bash
codex-usage status
```

You should see your latest weekly usage percentage, last-turn tokens, model,
reasoning effort, today's usage, and the current token-to-usage fit.

In this project, "last-turn tokens" means the latest observed sample delta
(`samples.token_delta`), not the transcript's internal last sub-step token
counter.

## macOS Desktop Widget

Build and open the native floating widget:

```bash
scripts/package-widget-app.sh
open "build/Codex Usage.app"
```

The widget shows:

- This week's total usage percentage.
- Today's usage increase and level: low, medium, or high.
- The latest model and reasoning effort.
- Last-turn token usage.
- The current estimate for turns and tokens per 1% weekly usage.

The last-turn token number is always based on `samples.token_delta` so desktop
and stats views use the same user-facing token policy.

For turn estimation, this project treats one user-visible turn as one positive
`token_delta` interval. Baseline samples and zero-delta samples are not counted
as turns.

The widget can be dragged around the desktop and switched between a compact view
and a full view with the button in the top-right corner.

## Common Commands

Run from the repo without installing:

```bash
PYTHONPATH=src python3 -m codex_usage status
PYTHONPATH=src python3 -m codex_usage daemon
PYTHONPATH=src python3 -m codex_usage export
PYTHONPATH=src python3 -m codex_usage hook-config
```

Run after `pip install -e .`:

```bash
codex-usage status
codex-usage daemon
codex-usage export
codex-usage hook-config
```

## Data Collected

The SQLite database contains:

- `sessions`: latest seen token totals, model, and reasoning effort per local
  Codex session.
- `samples`: per-turn hook samples, including token deltas, model, reasoning
  effort, weekly usage, and parse errors if any.
- `epochs`: weekly reset windows.
- `fits`: token-per-weekly-percent estimates for the current weekly window.
- `model_effort_fits`: token-per-weekly-percent and turn-per-weekly-percent
  estimates grouped by model and reasoning effort.

The first observed sample for a session is treated as a baseline and records
`token_delta = 0`, so enabling the tool in the middle of a long session does not
over-count previous work.

Stop events without a `transcript_path` are ignored because they cannot be tied
to a normal local Codex transcript or token sample.

## Limitations

Only local Codex sessions on this machine can contribute token deltas. If your
weekly usage changes because of Codex Web, another machine, or a session that
was not covered by the hook, the monitor can see the weekly percentage move but
cannot fully attribute the token usage.

The token-to-usage fit is an estimate. It becomes more useful after enough local
turns have been observed, especially after the weekly percentage has visibly
moved.

## License

This project is released under the MIT License. See [LICENSE](LICENSE).

## 中文说明

[English](#codex-weekly-usage-fitter) | 中文

当前版本：`v0.1.0`

Codex Weekly Usage Fitter 是一个本地 Codex 用量监控工具。它会在本机记录
Codex 对话产生的 token 用量，读取 Codex 当前显示的每周 usage 百分比，并估算
1% weekly usage 大约对应多少 token 或多少轮对话。

它适合回答这些问题：

- 我这一周的 Codex usage 已经用了多少？
- 上一轮对话消耗了多少 token？
- 上一轮用的是哪个模型、哪档 reasoning effort？
- 当前这个模型和 reasoning effort 组合下，平均多少轮或多少 token 会消耗 1%
  weekly usage？

项目还包含一个原生 macOS 桌面悬浮小组件，可以常驻桌面，每隔几秒刷新最新用量。

## 当前支持的平台

当前支持：

- macOS，本地需要有 Codex Desktop 或 Codex CLI 的 session 日志。
- Python 3.11 或更高版本，用于后端 collector 和 CLI。
- macOS AppKit 桌面小组件。

暂不支持：

- Windows 或 Linux 桌面小组件。
- 只发生在浏览器里的 Codex 使用，除非它也写入了本机 Codex session 日志。
- 其他机器上的 Codex 使用；这类使用最多只能体现为 weekly usage 百分比的变化，
  不能被准确归因到本机 token。

## 它是怎么工作的

这个工具完全在本机运行。它使用：

- Codex `Stop` hook，在每轮对话结束后采样一次。
- 本机 Codex transcript 文件，读取 token 数、模型、reasoning effort 和 weekly
  rate limit 快照。
- 当 transcript 里没有最新 weekly usage 快照时，用 `codex app-server` 作为兜底
  数据源。
- 本地 SQLite 数据库：`~/.codex/usage-monitor/usage.sqlite`。

它不会修改 Codex 本体，也不会把你的 transcript 发送到外部服务。

## 快速开始

克隆项目并安装本地 CLI：

```bash
git clone <repo-url>
cd "codex usage"
python3 -m pip install -e .
```

生成 Codex hook 配置：

```bash
codex-usage hook-config
```

把输出的配置片段加入 Codex 配置文件：

```bash
open ~/.codex/config.toml
```

然后启动本地 collector daemon：

```bash
codex-usage daemon
```

下一轮 Codex 对话结束后，查看用量：

```bash
codex-usage status
```

你应该能看到当前 weekly usage、上一轮 token、模型、reasoning effort、今日用量，
以及当前 token 到 usage 的拟合估算。

这里“上一轮 token”统一指最新一次采样间隔的增量（`samples.token_delta`），不是
transcript 内部某个子步骤的 last token 计数。

## macOS 桌面小组件

构建并打开原生桌面悬浮组件：

```bash
scripts/package-widget-app.sh
open "build/Codex Usage.app"
```

小组件会显示：

- 本周总 usage 百分比。
- 今日 usage 增量，以及 low、medium、high 档位。
- 最新一轮使用的模型和 reasoning effort。
- 上一轮消耗的 token。
- 当前估算的 1% weekly usage 对应多少轮和多少 token。

小组件里展示的上一轮 token 统一来自 `samples.token_delta`，确保桌面主面板和统计
面板口径一致。

在轮次估算里，本项目把“1 轮对话”定义为一次 `token_delta > 0` 的采样间隔；
baseline 样本和 `token_delta = 0` 的样本不会计入轮次。

小组件可以拖动，也可以用右上角按钮在简略视图和完整视图之间切换。

## 常用命令

不安装，直接从项目目录运行：

```bash
PYTHONPATH=src python3 -m codex_usage status
PYTHONPATH=src python3 -m codex_usage daemon
PYTHONPATH=src python3 -m codex_usage export
PYTHONPATH=src python3 -m codex_usage hook-config
```

安装后运行：

```bash
codex-usage status
codex-usage daemon
codex-usage export
codex-usage hook-config
```

## 收集的数据

SQLite 数据库包含：

- `sessions`：每个本地 Codex session 最新的 token 总数、模型和 reasoning effort。
- `samples`：每轮 hook 采样，包括 token delta、模型、reasoning effort、weekly
  usage，以及可能的解析错误。
- `epochs`：每周 reset 窗口。
- `fits`：当前 weekly window 下 token 到 weekly usage 百分比的整体估算。
- `model_effort_fits`：按模型和 reasoning effort 分组后的 token/turn 到 1%
  weekly usage 的估算。

每个 session 第一次被观察到时会作为 baseline，记录 `token_delta = 0`，这样即使
你在一个长会话中途启用工具，也不会把之前的历史 token 错算进去。

没有 `transcript_path` 的 Stop 事件会被忽略，因为它不能关联到正常的本地 Codex
transcript 或 token 样本。

## 限制

只有这台机器上的本地 Codex session 能贡献 token delta。如果 weekly usage 因为
Codex Web、另一台机器、或没有被 hook 覆盖的 session 而变化，这个工具可以看到
weekly 百分比变化，但不能准确归因具体 token 来源。

token 到 usage 的拟合是估算值。观察到的本地对话越多，尤其是在 weekly 百分比发生
可见变化之后，估算会更有参考价值。

## License / 许可证

本项目使用 MIT License 发布，详见 [LICENSE](LICENSE)。
