"""工作流配置与输入素材的解析（自 run.py 迁入，sys.exit 改为 TaskError）。

错误文案与旧 run.py 逐字一致，CLI 薄壳原样输出。
"""
import json
from pathlib import Path

from .errors import TaskError
from .paths import HERE


def load_json(path, description):
    """读取本地 JSON 文件。"""
    if not path.is_file():
        raise TaskError(f"缺少{description}: {path}")
    with path.open(encoding="utf-8") as json_file:
        return json.load(json_file)


def resolve_api_json(cli_path, config_path, workflow_config):
    """解析命令行覆盖值或工作流配置中的 API JSON 路径。"""
    if cli_path:
        return Path(cli_path).expanduser().resolve()
    configured_path = workflow_config.get("api_json")
    if not isinstance(configured_path, str) or not configured_path:
        raise TaskError("工作流配置缺少 api_json，或请通过 --api-json 指定")
    api_json = Path(configured_path).expanduser()
    if not api_json.is_absolute():
        api_json = config_path.parent / api_json
    return api_json.resolve()


def collect_input_references(workflow, input_fields):
    """按工作流配置声明的节点字段收集输入文件引用。"""
    if not isinstance(input_fields, list):
        raise TaskError("工作流配置的 input_fields 必须是列表")

    references = []
    seen = set()
    for rule in input_fields:
        if not isinstance(rule, dict):
            raise TaskError("input_fields 中的每项必须是对象")
        class_type = rule.get("class_type")
        field = rule.get("field")
        if not isinstance(class_type, str) or not isinstance(field, str):
            raise TaskError("每个 input_fields 规则必须配置 class_type 和 field")
        local_dir = rule.get("local_dir")
        if local_dir is not None and not isinstance(local_dir, str):
            raise TaskError("input_fields.local_dir 必须是字符串或省略")
        for node in workflow.values():
            if node.get("class_type") != class_type:
                continue
            reference = node.get("inputs", {}).get(field)
            if isinstance(reference, str) and reference not in seen:
                references.append({
                    "path": reference,
                    "local_dir": local_dir,
                })
                seen.add(reference)
    return references


def _linked_node_ids(node: dict) -> set[str]:
    """Return upstream IDs from ComfyUI API input connections."""
    return {
        value[0]
        for value in node.get("inputs", {}).values()
        if isinstance(value, list) and len(value) == 2
        and isinstance(value[0], str) and isinstance(value[1], int)
    }


def apply_optional_last_frame(
    workflow: dict,
    workflow_config: dict,
    use_last_frame: bool | None,
) -> bool:
    """Remove disabled tail-frame inputs from this task's in-memory graph.

    Only workflows explicitly declaring optional_last_frame opt in. Shared
    upstream nodes remain when another input still consumes them. Return
    whether the graph changed so even tasks without scalar overrides write
    the transformed API snapshot.
    """
    if workflow_config.get("optional_last_frame") is not True:
        if use_last_frame is not None:
            raise TaskError("当前工作流不支持尾帧开关")
        return False
    if use_last_frame is True:
        return False

    candidates = set()
    changed = False
    for node in workflow.values():
        if node.get("class_type") != "MiniMaxH3ImageToVideo":
            continue
        inputs = node.get("inputs", {})
        if "last_frame" not in inputs:
            continue
        connection = inputs.pop("last_frame")
        changed = True
        candidates.update(_linked_node_ids({"inputs": {"last_frame": connection}}))

    pending = list(candidates)
    while pending:
        node = workflow.get(pending.pop())
        if not isinstance(node, dict):
            continue
        for upstream in _linked_node_ids(node) - candidates:
            candidates.add(upstream)
            pending.append(upstream)

    while candidates:
        referenced = set().union(*(_linked_node_ids(node) for node in workflow.values()))
        unused = candidates - referenced
        if not unused:
            break
        for node_id in unused:
            workflow.pop(node_id, None)
        candidates -= unused
    return changed


def build_search_dirs(input_dir, api_json):
    """构建去重后的本地输入文件搜索目录。"""
    candidates = [
        input_dir,
        HERE / "image",
        api_json.parent / "image",
        api_json.parent,
        HERE,
    ]
    search_dirs = []
    seen = set()
    for directory in candidates:
        if not directory.is_dir():
            continue
        resolved = directory.resolve()
        if resolved not in seen:
            search_dirs.append(resolved)
            seen.add(resolved)
    return search_dirs


def resolve_input_files(
    references,
    search_dirs,
    workflow_config_path,
    input_dir_overridden,
):
    """将工作流中的输入引用解析为本地文件。"""
    resolved_inputs = {}
    missing = []
    searched_directories = []
    for input_reference in references:
        reference = input_reference["path"]
        configured_dir = input_reference.get("local_dir")
        reference_search_dirs = search_dirs
        if configured_dir and not input_dir_overridden:
            configured_path = Path(configured_dir).expanduser()
            if not configured_path.is_absolute():
                configured_path = workflow_config_path.parent / configured_path
            reference_search_dirs = [configured_path.resolve()]
        for directory in reference_search_dirs:
            if directory not in searched_directories:
                searched_directories.append(directory)
        local_path = next(
            (directory / reference for directory in reference_search_dirs
             if (directory / reference).is_file()),
            None,
        )
        if local_path is None:
            missing.append(reference)
        else:
            resolved_inputs[reference] = local_path
    if missing:
        raise TaskError("缺少输入文件 (搜索目录: %s):\n  %s" % (
            ", ".join(str(directory) for directory in searched_directories),
            "\n  ".join(missing),
        ))
    return resolved_inputs


def validate_workflow_accelerator(workflow_config, accelerator) -> None:
    """验证工作流是否声明支持当前加速器。"""
    supported = workflow_config.get("supported_accelerators", ["gpu"])
    if not isinstance(supported, list) or not all(
            isinstance(item, str) for item in supported):
        raise TaskError("工作流配置的 supported_accelerators 必须是字符串列表")
    normalized = {item.lower() for item in supported}
    invalid = normalized.difference({"cpu", "gpu", "tpu"})
    if invalid:
        raise TaskError(
            "supported_accelerators 包含未知类型: "
            + ", ".join(sorted(invalid))
        )
    if accelerator is None or accelerator in normalized:
        return

    workflow_name = workflow_config.get("name", "当前工作流")
    supported_text = "/".join(item.upper() for item in sorted(normalized))
    message = (
        f"{workflow_name} 不支持 {accelerator.upper()}，"
        f"配置声明仅支持: {supported_text}。"
    )
    accelerator_note = workflow_config.get("accelerator_note")
    if isinstance(accelerator_note, str) and accelerator_note:
        message += accelerator_note
    raise TaskError(message)
