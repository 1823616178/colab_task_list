"""录制 dry-run 事件流 fixture（tests/fixtures/events_dry_run.jsonl）。

用法（在 colab_cli/ 目录下）：

    python tests/record_dry_run_fixture.py

P2/P3 前端开发用该 fixture 回放完整任务事件流，不烧计算单元（doc/05 P0.4）。
契约变更后必须重新录制。
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import colab_core.task as core_task  # noqa: E402
from colab_core import TaskConfig, run_task  # noqa: E402

TASK_ID = "20260822-000000-deadbeef"


def main() -> None:
    # 用真实工作流配置录制，前端能看到真实模型清单与输入引用
    config_path = HERE / "workflows" / "minimax_h3.json"
    fixtures_dir = HERE / "tests" / "fixtures"
    fixtures_dir.mkdir(parents=True, exist_ok=True)
    output = fixtures_dir / "events_dry_run.jsonl"

    core_task._DRY_STEP_SECONDS = 0
    events = []
    cfg = TaskConfig(
        workflow_config=config_path,
        output_dir=fixtures_dir / "dry_run_output" / TASK_ID,
        task_id=TASK_ID,
        dry_run=True,
    )
    result = run_task(cfg, events.append)

    with output.open("w", encoding="utf-8") as fixture_file:
        for event in events:
            fixture_file.write(json.dumps(event, ensure_ascii=False) + "\n")
    print(
        f"已录制 {len(events)} 个事件 -> {output} "
        f"(任务状态: {result.status})"
    )


if __name__ == "__main__":
    main()
