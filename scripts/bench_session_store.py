#!/usr/bin/env python3
"""A+ A1 基准: 会话存储三项主路径耗时(人工验收, **不进 CI**).

用法::

    python scripts/bench_session_store.py                        # 默认 50/200/500/1000/2000 轮
    python scripts/bench_session_store.py --sizes 50,500 --samples 20
    python scripts/bench_session_store.py --keep                 # 保留临时目录(排障)

口径与 ``docs/A_PLUS_OPTIMIZATION.md`` §2.1(ADR-1 §15.3 基线)一致:

- 每轮 user + assistant 各约 1200 字符(中文 UTF-8 约 3 字节/字符 → 2000 轮 ≈ 15MB);
- ``load_recent(max_turns=50, max_chars=120000)`` 后再做一次 ``to_llm_message()`` 投影
  (与 ``ConversationService._load_history`` 同口径);
- 每个指标采样 ``--samples`` 次(默认 20), 输出 avg / p95(ms), 并与 §2.1 基线对照;
- 额外测量 ``list_sessions``(A2 验收项: 每会话只读 header + 末条有效 turn);
- A3 后 ``load_recent`` 应显著低于 §2.1 基线(负偏差为提速), 并检查 50→2000 轮 p95 平坦性。

实现说明: 生成阶段用 ``append_turn`` 逐轮写入(与真实路径同 schema);
测量阶段换一个**全新 store 实例**(指标从零开始, 不含生成阶段噪声)。
"""

import argparse
import asyncio
import platform
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

from meowmeowclaw.memory import JsonlSessionStore, SessionKey, SessionMessage
from meowmeowclaw.memory.metrics import LatencyStats, measure as measure_latency

# §2.1 基线: 轮数 -> (load_recent avg_ms, p95_ms)
BASELINE_LOAD_RECENT: dict[int, tuple[float, float]] = {
    50: (1.32, 1.64),
    200: (3.99, 4.40),
    500: (9.39, 10.53),
    1000: (18.12, 20.70),
    2000: (39.83, 44.64),
}
USER_CHARS = 1200
ASSISTANT_CHARS = 1200
WINDOW_TURNS = 50
WINDOW_CHARS = 120_000
DEFAULT_SIZES = (50, 200, 500, 1000, 2000)


def build_turn(index: int) -> list[SessionMessage]:
    """构造一轮 user+assistant(各约 1200 字符)."""
    user_text = f"[轮 {index}] " + "甲" * max(1, USER_CHARS - len(f"[轮 {index}] "))
    assistant_text = f"[轮 {index}] " + "乙" * max(1, ASSISTANT_CHARS - len(f"[轮 {index}] "))
    return [
        SessionMessage(role="user", content=user_text),
        SessionMessage(role="assistant", content=assistant_text),
    ]


async def generate(root: Path, key: SessionKey, turns: int) -> Optional[int]:
    """用真实写入路径生成本轮文件; 返回文件字节数."""
    writer = JsonlSessionStore(root)
    try:
        for index in range(1, turns + 1):
            await writer.append_turn(key, build_turn(index))
        return writer.stats()["last_file_size_bytes"]
    finally:
        await writer.close()


async def measure(
    root: Path,
    key: SessionKey,
    *,
    samples: int,
) -> dict[str, dict[str, Any]]:
    """在全新 store 实例上采样三项主路径(返回 store.metrics 快照)."""
    store = JsonlSessionStore(root)
    try:
        for _ in range(samples):
            messages = await store.load_recent(
                key, max_turns=WINDOW_TURNS, max_chars=WINDOW_CHARS
            )
            [_message.to_llm_message() for _message in messages]  # 投影(与编排层同口径)
        for _ in range(samples):
            await store.get_meta(key)
        for _ in range(samples):
            await store.append_turn(key, build_turn(0))
        metrics = dict(store.stats()["metrics"])
        # list_sessions 不在 store.metrics 的三项主路径里, 单独采样(A2 验收项)
        list_stats = LatencyStats()
        for _ in range(samples):
            with measure_latency(list_stats):
                await store.list_sessions(include_archived=True)
        metrics["list_sessions"] = list_stats.snapshot()
        return metrics
    finally:
        await store.close()


def format_cell(latency: dict[str, Any]) -> str:
    avg, p95 = latency.get("avg"), latency.get("p95")
    if avg is None or p95 is None:
        return "-"
    return f"{avg:.2f} / {p95:.2f}"


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="A+ A1 会话存储基准")
    parser.add_argument(
        "--sizes",
        default=",".join(str(size) for size in DEFAULT_SIZES),
        help="轮数列表(逗号分隔), 默认 50,200,500,1000,2000",
    )
    parser.add_argument("--samples", type=int, default=20, help="每项指标采样次数(默认 20)")
    parser.add_argument("--keep", action="store_true", help="保留临时目录(默认用完即删)")
    args = parser.parse_args(argv)

    sizes = [int(item) for item in str(args.sizes).split(",") if item.strip()]
    if args.samples <= 0:
        print("[错误] --samples 必须为正整数", file=sys.stderr)
        return 2

    print(f"# 环境: {platform.platform()} / Python {platform.python_version()} / "
          f"CPU {platform.processor() or 'unknown'}")
    print(f"# 口径: 每轮 user+assistant 各约 {USER_CHARS} 字符; "
          f"窗口 max_turns={WINDOW_TURNS}, max_chars={WINDOW_CHARS}; 采样 {args.samples} 次")
    print()
    header = (
        f"{'轮数':>6}  {'文件':>9}  {'生成':>7}  "
        f"{'load_recent avg/p95':>21}  {'get_meta avg/p95':>18}  "
        f"{'append_turn avg/p95':>21}  {'list_sessions avg/p95':>22}  "
        f"{'§2.1 基线':>13}  {'偏差':>8}"
    )
    print(header)
    print("-" * len(header))

    results: list[tuple[int, Optional[int], dict[str, dict[str, Any]], float]] = []
    for turns in sizes:
        workdir = Path(tempfile.mkdtemp(prefix=f"meowbench-{turns}-"))
        key = SessionKey(channel="cli", scope="session", conversation_id=f"bench-{turns}")
        try:
            started = time.perf_counter()
            size_bytes = asyncio.run(generate(workdir, key, turns))
            generate_seconds = time.perf_counter() - started
            metrics = asyncio.run(measure(workdir, key, samples=args.samples))
        finally:
            if not args.keep:
                shutil.rmtree(workdir, ignore_errors=True)
            else:
                print(f"# 保留临时目录: {workdir}", file=sys.stderr)

        size_text = f"{(size_bytes or 0) / 1024 / 1024:.2f}MB"
        baseline = BASELINE_LOAD_RECENT.get(turns)
        baseline_text = f"{baseline[0]:.2f} / {baseline[1]:.2f}" if baseline else "-"
        measured_avg = metrics["load_recent"].get("avg")
        if baseline and measured_avg is not None:
            deviation = (float(measured_avg) - baseline[0]) / baseline[0] * 100.0
            deviation_text = f"{deviation:+.0f}%"
        else:
            deviation = float("nan")
            deviation_text = "-"
        results.append((turns, size_bytes, metrics, deviation))

        print(
            f"{turns:>6}  {size_text:>9}  {generate_seconds:>6.2f}s  "
            f"{format_cell(metrics['load_recent']):>21}  "
            f"{format_cell(metrics['get_meta']):>18}  "
            f"{format_cell(metrics['append_turn']):>21}  "
            f"{format_cell(metrics['list_sessions']):>22}  "
            f"{baseline_text:>13}  {deviation_text:>8}"
        )

    checked = [deviation for _, _, _, deviation in results if deviation == deviation]  # 非 NaN
    if checked:
        worst = max(abs(value) for value in checked)
        worst_positive = max(checked)  # 负偏差 = 提速, 不应触发告警
        if worst <= 20.0:
            verdict = "✅ 与 §2.1 基线一致(±20% 内)"
        elif worst_positive <= 20.0:
            verdict = "✅ 整体优于 §2.1 基线(小文件持平, 长会话显著提速)"
        else:
            verdict = "⚠️ 与 §2.1 偏差 >20%, 请确认机器负载"
        print()
        print(f"# load_recent avg 最大偏差 {worst:.0f}% → {verdict}")

    # A3 验收: 50 → 2000 轮之间 load_recent p95 基本平坦(差值 ≤2ms)
    by_turns = {turns: metrics for turns, _, metrics, _ in results}
    if 50 in by_turns and 2000 in by_turns:
        small = by_turns[50]["load_recent"].get("p95")
        large = by_turns[2000]["load_recent"].get("p95")
        if small is not None and large is not None:
            delta_p95 = abs(float(large) - float(small))
            flat_verdict = "✅" if delta_p95 <= 2.0 else "⚠️"
            print(
                f"# load_recent p95 平坦性: 50→2000 轮差值 {delta_p95:.2f}ms → {flat_verdict} "
                "(A3 验收 ≤2ms)"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
