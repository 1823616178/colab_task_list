"""用量账本: 记录每次调用的 token 数与花费, 按本地时区汇总成日/月用量, 并做额度判定。

注意: 这是本地估算, 只统计经过本网关/CLI 的调用, 真实额度以 Kaggle 平台为准。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo
from pathlib import Path

from ..common.config import DAILY_BUDGET_DOLLARS, MONTHLY_BUDGET_DOLLARS
from ..common.logging import get_logger

logger = get_logger(__name__)

NANODOLLARS_PER_DOLLAR = 1_000_000_000


@dataclass(frozen=True)
class UsageSummary:
    """一段时间内的用量汇总。"""

    call_count: int
    streamed_call_count: int
    input_tokens: int
    output_tokens: int
    cost_dollars: float
    incomplete_call_count: int = 0


def extract_record(model: str, response_body: dict, streamed: bool,
                   incomplete: bool = False) -> dict:
    """从一次响应中提取账本记录。incomplete 表示流式转发中途断开。"""
    usage = response_body.get("usage") or {}
    cost = usage.get("cost") or {}
    cost_nanodollars = int(cost.get("input_tokens_cost_nanodollars", 0)) + int(
        cost.get("output_tokens_cost_nanodollars", 0)
    )
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "streamed": streamed,
        "input_tokens": int(usage.get("prompt_tokens", 0)),
        "output_tokens": int(usage.get("completion_tokens", 0)),
        "cost_nanodollars": cost_nanodollars,
    }
    if incomplete:
        record["incomplete"] = True
    return record


def append_record(usage_path: Path, record: dict) -> None:
    """把一条记录追加进账本, 失败不影响主流程。"""
    try:
        usage_path.parent.mkdir(parents=True, exist_ok=True)
        with usage_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
    except OSError as error:
        logger.warning("写用量账本 %s 失败: %s", usage_path, error)


def load_records(usage_path: Path) -> list[dict]:
    """读取账本全部记录, 跳过损坏行。"""
    if not usage_path.exists():
        return []
    records = []
    for line in usage_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    return records


def local_stamp(record: dict, tz: tzinfo | None) -> str:
    """把记录的 UTC 时间换成目标时区的 ISO 字符串, 用于按日/月前缀筛选。"""
    raw = str(record.get("ts", ""))
    if tz is None:
        return raw
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(tz).isoformat()


def summarize(records: list[dict], prefix: str, tz: tzinfo | None = None) -> UsageSummary:
    """汇总(按 tz 换算后)时间戳以 prefix 开头的记录, prefix 形如 2026-08-28 或 2026-08。"""
    selected = [r for r in records if local_stamp(r, tz).startswith(prefix)]
    return UsageSummary(
        call_count=len(selected),
        streamed_call_count=sum(1 for r in selected if r.get("streamed")),
        input_tokens=sum(int(r.get("input_tokens", 0)) for r in selected),
        output_tokens=sum(int(r.get("output_tokens", 0)) for r in selected),
        cost_dollars=sum(int(r.get("cost_nanodollars", 0)) for r in selected)
        / NANODOLLARS_PER_DOLLAR,
        incomplete_call_count=sum(1 for r in selected if r.get("incomplete")),
    )


@dataclass(frozen=True)
class BudgetStatus:
    """当前额度状态。limit <= 0 表示不限制。"""

    day_key: str
    month_key: str
    today: UsageSummary
    month: UsageSummary
    daily_limit: float
    monthly_limit: float
    warn_ratio: float

    @staticmethod
    def _ratio(spent: float, limit: float) -> float:
        return spent / limit if limit > 0 else 0.0

    @property
    def daily_ratio(self) -> float:
        return self._ratio(self.today.cost_dollars, self.daily_limit)

    @property
    def monthly_ratio(self) -> float:
        return self._ratio(self.month.cost_dollars, self.monthly_limit)

    @property
    def exceeded(self) -> str | None:
        """超限时返回说明, 否则 None。"""
        if self.daily_limit > 0 and self.today.cost_dollars >= self.daily_limit:
            return f"今日用量 ${self.today.cost_dollars:.4f} 已达上限 ${self.daily_limit:.2f}"
        if self.monthly_limit > 0 and self.month.cost_dollars >= self.monthly_limit:
            return f"本月用量 ${self.month.cost_dollars:.4f} 已达上限 ${self.monthly_limit:.2f}"
        return None

    @property
    def warning(self) -> str | None:
        """接近上限(>= warn_ratio)时返回说明。"""
        if self.daily_ratio >= self.warn_ratio:
            return f"今日已用 {self.daily_ratio:.0%} (${self.today.cost_dollars:.4f}/${self.daily_limit:.2f})"
        if self.monthly_ratio >= self.warn_ratio:
            return f"本月已用 {self.monthly_ratio:.0%} (${self.month.cost_dollars:.4f}/${self.monthly_limit:.2f})"
        return None

    def to_dict(self) -> dict:
        def part(summary: UsageSummary, limit: float, ratio: float) -> dict:
            return {
                "calls": summary.call_count,
                "streamed_calls": summary.streamed_call_count,
                "incomplete_calls": summary.incomplete_call_count,
                "input_tokens": summary.input_tokens,
                "output_tokens": summary.output_tokens,
                "cost_dollars": round(summary.cost_dollars, 6),
                "limit_dollars": limit,
                "used_ratio": round(ratio, 4),
            }
        return {
            "day": self.day_key,
            "month": self.month_key,
            "today": part(self.today, self.daily_limit, self.daily_ratio),
            "this_month": part(self.month, self.monthly_limit, self.monthly_ratio),
            "warning": self.warning,
            "exceeded": self.exceeded,
        }


class Ledger:
    """线程安全的账本: 启动时读一次文件, 之后在内存里累加, 额度判定不必每次读盘。"""

    def __init__(self, usage_path: Path, tz: tzinfo,
                 daily_limit: float = DAILY_BUDGET_DOLLARS,
                 monthly_limit: float = MONTHLY_BUDGET_DOLLARS,
                 warn_ratio: float = 0.8):
        self.usage_path = usage_path
        self.tz = tz
        self.daily_limit = daily_limit
        self.monthly_limit = monthly_limit
        self.warn_ratio = warn_ratio
        self._lock = threading.Lock()
        self._records = load_records(usage_path)

    def record(self, record: dict) -> None:
        """追加一条记录(落盘 + 内存)。"""
        with self._lock:
            append_record(self.usage_path, record)
            self._records.append(record)

    def status(self, now: datetime | None = None) -> BudgetStatus:
        """按本地时区计算今日/本月用量。"""
        now = (now or datetime.now(timezone.utc)).astimezone(self.tz)
        day_key, month_key = now.strftime("%Y-%m-%d"), now.strftime("%Y-%m")
        with self._lock:
            records = list(self._records)
        return BudgetStatus(
            day_key=day_key,
            month_key=month_key,
            today=summarize(records, day_key, self.tz),
            month=summarize(records, month_key, self.tz),
            daily_limit=self.daily_limit,
            monthly_limit=self.monthly_limit,
            warn_ratio=self.warn_ratio,
        )


def ledger_from_settings(settings) -> Ledger:
    """按 Settings 构造账本。"""
    return Ledger(settings.usage_path, settings.tzinfo, settings.daily_limit_dollars,
                  settings.monthly_limit_dollars, settings.budget_warn_ratio)


def format_report(usage_path: Path, tz: tzinfo | None = None,
                  daily_limit: float = DAILY_BUDGET_DOLLARS,
                  monthly_limit: float = MONTHLY_BUDGET_DOLLARS) -> str:
    """生成 kllm usage 的输出文本(按 tz 切分日/月, 默认 UTC)。"""
    tz = tz or timezone.utc
    status = Ledger(usage_path, tz, daily_limit, monthly_limit).status()

    lines = [f"账本: {usage_path}", f"时区: {tz}  今日={status.day_key}", ""]
    for label, summary, budget in (
        ("今日", status.today, daily_limit),
        ("本月", status.month, monthly_limit),
    ):
        lines.append(
            f"{label}: {summary.call_count} 次调用"
            f"(流式 {summary.streamed_call_count} 次, 中断 {summary.incomplete_call_count} 次)"
        )
        lines.append(
            f"      tokens 输入 {summary.input_tokens} / 输出 {summary.output_tokens}"
        )
        if budget > 0:
            lines.append(
                f"      花费 ${summary.cost_dollars:.4f} / ${budget:.2f}"
                f"  剩余约 ${max(0.0, budget - summary.cost_dollars):.4f}"
            )
        else:
            lines.append(f"      花费 ${summary.cost_dollars:.4f}(未设上限)")
        lines.append("")
    if status.warning:
        lines.append(f"⚠ {status.warning}")
    if status.exceeded:
        lines.append(f"⛔ {status.exceeded}, 网关会拒绝新请求")
    lines.append("提示: 这是本地估算, 真实额度以 Kaggle 平台为准。")
    return "\n".join(lines)
