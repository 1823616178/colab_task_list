"""用量账本的提取与汇总。"""

from kaggle_tools.kllm.usage import (
    append_record,
    extract_record,
    format_report,
    load_records,
    summarize,
)


def test_extract_record_sums_input_and_output_cost():
    body = {"usage": {"prompt_tokens": 10, "completion_tokens": 20,
                      "cost": {"input_tokens_cost_nanodollars": 1000,
                               "output_tokens_cost_nanodollars": 2000}}}
    record = extract_record("m", body, streamed=False)
    assert record["cost_nanodollars"] == 3000
    assert record["input_tokens"] == 10
    assert record["output_tokens"] == 20


def test_extract_record_tolerates_a_body_without_usage():
    record = extract_record("m", {}, streamed=True)
    assert record["cost_nanodollars"] == 0
    assert record["streamed"] is True


def test_summarize_filters_by_timestamp_prefix():
    records = [
        {"ts": "2026-08-28T01:00:00+00:00", "input_tokens": 1, "output_tokens": 2,
         "cost_nanodollars": 1_000_000_000, "streamed": False},
        {"ts": "2026-08-27T01:00:00+00:00", "input_tokens": 5, "output_tokens": 5,
         "cost_nanodollars": 500_000_000, "streamed": True},
    ]
    day = summarize(records, "2026-08-28")
    assert day.call_count == 1
    assert day.cost_dollars == 1.0

    month = summarize(records, "2026-08")
    assert month.call_count == 2
    assert month.streamed_call_count == 1
    assert month.cost_dollars == 1.5


def test_load_records_skips_corrupt_lines(tmp_path):
    path = tmp_path / "usage.jsonl"
    path.write_text('{"ts": "2026-08-28"}\nnot json\n\n{"ts": "2026-08-28"}\n')
    assert len(load_records(path)) == 2


def test_format_report_runs_on_an_empty_ledger(tmp_path):
    report = format_report(tmp_path / "usage.jsonl")
    assert "今日" in report and "本月" in report


def test_append_record_creates_parent_directory(tmp_path):
    path = tmp_path / "nested" / "usage.jsonl"
    append_record(path, {"ts": "2026-08-28", "cost_nanodollars": 0})
    assert len(load_records(path)) == 1
