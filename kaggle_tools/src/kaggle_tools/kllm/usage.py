"""用量账本: 记录每次调用的 token 数与花费, 汇总成日/月余量估算。

注意: 这是本地估算。Kaggle 侧的真实计费以平台为准, 流式响应上游不返回 usage,
所以流式调用只能记一条无花费的占位记录。
"""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import DAILY_BUDGET_DOLLARS, MONTHLY_BUDGET_DOLLARS

NANODOLLARS_PER_DOLLAR = 1_000_000_000


@dataclass(frozen=True)
class UsageSummary:
    """一段时间内的用量汇总。"""

    call_count: int
    streamed_call_count: int
    input_tokens: int
    output_tokens: int
    cost_dollars: float


def extract_record(model: str, response_body: dict, streamed: bool) -> dict:
    """从一次响应中提取账本记录。"""
    usage = response_body.get("usage") or {}
    cost = usage.get("cost") or {}
    cost_nanodollars = int(cost.get("input_tokens_cost_nanodollars", 0)) + int(
        cost.get("output_tokens_cost_nanodollars", 0)
    )
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "streamed": streamed,
        "input_tokens": int(usage.get("prompt_tokens", 0)),
        "output_tokens": int(usage.get("completion_tokens", 0)),
        "cost_nanodollars": cost_nanodollars,
    }


def append_record(usage_path: Path, record: dict) -> None:
    """把一条记录追加进账本, 失败不影响主流程。"""
    try:
        usage_path.parent.mkdir(parents=True, exist_ok=True)
        with usage_path.open("a") as handle:
            handle.write(json.dumps(record) + "\n")
    except OSError:
        pass


def load_records(usage_path: Path) -> list[dict]:
    """读取账本全部记录, 跳过损坏行。"""
    if not usage_path.exists():
        return []
    records = []
    for line in usage_path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    return records


def summarize(records: list[dict], prefix: str) -> UsageSummary:
    """汇总时间戳以 prefix 开头的记录(prefix 形如 2026-08-28 或 2026-08)。"""
    selected = [r for r in records if str(r.get("ts", "")).startswith(prefix)]
    return UsageSummary(
        call_count=len(selected),
        streamed_call_count=sum(1 for r in selected if r.get("streamed")),
        input_tokens=sum(int(r.get("input_tokens", 0)) for r in selected),
        output_tokens=sum(int(r.get("output_tokens", 0)) for r in selected),
        cost_dollars=sum(int(r.get("cost_nanodollars", 0)) for r in selected)
        / NANODOLLARS_PER_DOLLAR,
    )


def format_report(usage_path: Path) -> str:
    """生成 kllm usage 的输出文本。"""
    records = load_records(usage_path)
    now = datetime.now(timezone.utc)
    today = summarize(records, now.strftime("%Y-%m-%d"))
    month = summarize(records, now.strftime("%Y-%m"))

    lines = [f"账本: {usage_path}", ""]
    for label, summary, budget in (
        ("今日", today, DAILY_BUDGET_DOLLARS),
        ("本月", month, MONTHLY_BUDGET_DOLLARS),
    ):
        lines.append(
            f"{label}: {summary.call_count} 次调用"
            f"(其中流式 {summary.streamed_call_count} 次, 无花费数据)"
        )
        lines.append(
            f"      tokens 输入 {summary.input_tokens} / 输出 {summary.output_tokens}"
        )
        lines.append(
            f"      花费 ${summary.cost_dollars:.4f} / ${budget:.2f}"
            f"  剩余约 ${max(0.0, budget - summary.cost_dollars):.4f}"
        )
        lines.append("")
    lines.append("提示: 流式调用上游不返回 usage, 花费统计会偏低; 真实额度以 Kaggle 平台为准。")
    return "\n".join(lines)
