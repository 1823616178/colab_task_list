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


def _empty_totals() -> list[int]:
    """聚合桶: [调用数, 流式数, 输入 tokens, 输出 tokens, 花费纳美元, 中断数]。"""
    return [0, 0, 0, 0, 0, 0]


def _totals_to_summary(totals: list[int]) -> UsageSummary:
    return UsageSummary(
        call_count=totals[0],
        streamed_call_count=totals[1],
        input_tokens=totals[2],
        output_tokens=totals[3],
        cost_dollars=totals[4] / NANODOLLARS_PER_DOLLAR,
        incomplete_call_count=totals[5],
    )


class Ledger:
    """线程安全的账本: 启动时读一次文件并建成日/月聚合, 之后 status() 是 O(1)。

    原先每次 status() 都要把全部记录按时间戳重算一遍, 账本用几个月后
    每次网关请求都要扫全量。现在聚合在内存里增量维护, 只有当账本文件
    被本账本之外的进程改动(例如 kllm ask 直接写文件)时才重建一次。
    """

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
        self._records: list[dict] = []
        self._by_day: dict[str, list[int]] = {}
        self._by_month: dict[str, list[int]] = {}
        self._by_day_model: dict[tuple[str, str], list[int]] = {}
        self._by_month_model: dict[tuple[str, str], list[int]] = {}
        self._known_size: int | None = None
        self._known_mtime_ns: int | None = None
        with self._lock:
            self._rebuild_locked()

    def _accumulate_locked(self, record: dict) -> None:
        stamp = local_stamp(record, self.tz)
        day_key, month_key = stamp[:10], stamp[:7]
        model = str(record.get("model") or "(未知)")
        cost = int(record.get("cost_nanodollars", 0) or 0)
        values = (
            1,
            1 if record.get("streamed") else 0,
            int(record.get("input_tokens", 0) or 0),
            int(record.get("output_tokens", 0) or 0),
            cost,
            1 if record.get("incomplete") else 0,
        )
        for table, key in (
            (self._by_day, day_key),
            (self._by_month, month_key),
            (self._by_day_model, (day_key, model)),
            (self._by_month_model, (month_key, model)),
        ):
            bucket = table.setdefault(key, _empty_totals())
            for index, value in enumerate(values):
                bucket[index] += value

    def _rebuild_locked(self) -> None:
        self._records = load_records(self.usage_path)
        self._by_day.clear()
        self._by_month.clear()
        self._by_day_model.clear()
        self._by_month_model.clear()
        for record in self._records:
            self._accumulate_locked(record)
        self._refresh_known_locked()

    def _refresh_known_locked(self) -> None:
        try:
            stat = self.usage_path.stat()
        except OSError:
            self._known_size = None
            self._known_mtime_ns = None
            return
        self._known_size = stat.st_size
        self._known_mtime_ns = stat.st_mtime_ns

    def _maybe_reload_locked(self) -> None:
        """文件被外部改动时重建聚合, 否则直接用内存聚合。"""
        try:
            stat = self.usage_path.stat()
        except OSError:
            if self._known_size is not None:
                self._rebuild_locked()
            return
        if stat.st_size != self._known_size or stat.st_mtime_ns != self._known_mtime_ns:
            self._rebuild_locked()

    def record(self, record: dict) -> None:
        """追加一条记录(落盘 + 内存聚合)。"""
        with self._lock:
            append_record(self.usage_path, record)
            self._records.append(record)
            self._accumulate_locked(record)
            self._refresh_known_locked()

    def status(self, now: datetime | None = None) -> BudgetStatus:
        """按本地时区计算今日/本月用量(聚合查询, 不扫全量记录)。"""
        now = (now or datetime.now(timezone.utc)).astimezone(self.tz)
        day_key, month_key = now.strftime("%Y-%m-%d"), now.strftime("%Y-%m")
        with self._lock:
            self._maybe_reload_locked()
            today = _totals_to_summary(self._by_day.get(day_key, _empty_totals()))
            month = _totals_to_summary(self._by_month.get(month_key, _empty_totals()))
        return BudgetStatus(
            day_key=day_key,
            month_key=month_key,
            today=today,
            month=month,
            daily_limit=self.daily_limit,
            monthly_limit=self.monthly_limit,
            warn_ratio=self.warn_ratio,
        )

    def model_breakdown(self, scope: str = "today",
                        now: datetime | None = None) -> dict[str, dict]:
        """按模型分的花费: scope 为 today 或 month, 便于一眼看出谁在烧钱。"""
        now = (now or datetime.now(timezone.utc)).astimezone(self.tz)
        key = now.strftime("%Y-%m-%d") if scope == "today" else now.strftime("%Y-%m")
        table = self._by_day_model if scope == "today" else self._by_month_model
        with self._lock:
            self._maybe_reload_locked()
            result = {}
            for (period, model), totals in table.items():
                if period != key:
                    continue
                result[model] = {
                    "calls": totals[0],
                    "input_tokens": totals[2],
                    "output_tokens": totals[3],
                    "cost_dollars": round(totals[4] / NANODOLLARS_PER_DOLLAR, 6),
                }
            return dict(sorted(result.items(), key=lambda item: -item[1]["cost_dollars"]))


def archive_old_records(usage_path: Path, before_month: str) -> tuple[int, int]:
    """把 UTC 月份早于 before_month(形如 2026-01)的记录移到同目录的 usage-archive.jsonl。

    维护工具, 不是自动行为: 账本文件长期不清理会越来越大, 定期归档旧月份
    能让启动重建保持轻量。返回(保留条数, 归档条数)。
    """
    records = load_records(usage_path)
    kept = [r for r in records if str(r.get("ts", ""))[:7] >= before_month]
    archived = [r for r in records if str(r.get("ts", ""))[:7] < before_month]
    if not archived:
        return len(kept), 0
    archive_path = usage_path.with_name("usage-archive.jsonl")
    with archive_path.open("a", encoding="utf-8") as handle:
        for record in archived:
            handle.write(json.dumps(record) + "\n")
    temporary = usage_path.with_suffix(".jsonl.tmp")
    temporary.write_text("".join(json.dumps(r) + "\n" for r in kept), encoding="utf-8")
    temporary.replace(usage_path)
    return len(kept), len(archived)


def ledger_from_settings(settings) -> Ledger:
    """按 Settings 构造账本。"""
    return Ledger(settings.usage_path, settings.tzinfo, settings.daily_limit_dollars,
                  settings.monthly_limit_dollars, settings.budget_warn_ratio)


def format_report(usage_path: Path, tz: tzinfo | None = None,
                  daily_limit: float = DAILY_BUDGET_DOLLARS,
                  monthly_limit: float = MONTHLY_BUDGET_DOLLARS,
                  by_model: bool = False) -> str:
    """生成 kllm usage 的输出文本(按 tz 切分日/月, 默认 UTC)。"""
    tz = tz or timezone.utc
    ledger = Ledger(usage_path, tz, daily_limit, monthly_limit)
    status = ledger.status()

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
    if by_model:
        for scope, label in (("today", "今日按模型"), ("month", "本月按模型")):
            breakdown = ledger.model_breakdown(scope)
            if breakdown:
                lines.append(f"{label}:")
                for model, totals in breakdown.items():
                    lines.append(
                        f"      {model}: {totals['calls']} 次, "
                        f"${totals['cost_dollars']:.4f}"
                    )
                lines.append("")
    if status.warning:
        lines.append(f"⚠ {status.warning}")
    if status.exceeded:
        lines.append(f"⛔ {status.exceeded}, 网关会拒绝新请求")
    lines.append("提示: 这是本地估算, 只统计经网关/kllm ask 的调用; "
                 "kcloud local/Modal 直连代理的调用不在此账本, 真实额度以 Kaggle 平台为准。")
    return "\n".join(lines)
