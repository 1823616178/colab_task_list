#!/usr/bin/env python3
"""一键设置 MiniMax H3 图生视频工作流的首帧/尾帧输入图片。

把首帧/尾帧图片复制到 image/ 目录，并在 api.json 中自动追踪
MiniMaxH3ImageToVideo 节点的 first_frame / last_frame 输入，
更新对应 LoadImage 节点引用的文件名。之后运行 start.sh 时
run.py 会按文件名自动找到并上传这些图片。

用法:
    python3 set_frames.py --first 首帧.png --last 尾帧.png
    python3 set_frames.py --first 首帧.png   # 只换首帧
"""

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_API_JSON = HERE.parent / "api.json"
DEFAULT_IMAGE_DIR = HERE / "image"

IMAGE_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff",
}
GENERATOR_CLASS_TYPE = "MiniMaxH3ImageToVideo"
FRAME_FIELDS = {"first": "first_frame", "last": "last_frame"}
IMAGE_TRANSFORM_CLASS_TYPES = {
    "ImageScaleToTotalPixels", "ImageScale", "ImageScaleBy",
}


def copy_to_image_dir(source, image_dir):
    """复制图片到输入目录，返回目录内的文件名。

    同名同内容直接复用；同名不同内容自动追加序号，避免覆盖已有素材。
    """
    source = Path(source).expanduser()
    if not source.is_file():
        sys.exit(f"错误: 图片不存在: {source}")
    if source.suffix.lower() not in IMAGE_EXTENSIONS:
        sys.exit(
            f"错误: 不支持的图片格式 {source.suffix}，"
            f"仅支持: {', '.join(sorted(IMAGE_EXTENSIONS))}"
        )

    image_dir = Path(image_dir).expanduser()
    image_dir.mkdir(parents=True, exist_ok=True)
    source_bytes = source.read_bytes()

    for index in range(1000):
        if index == 0:
            candidate = image_dir / source.name
        else:
            candidate = image_dir / f"{source.stem}_{index}{source.suffix}"
        if not candidate.exists():
            shutil.copyfile(source, candidate)
            return candidate.name
        if candidate.read_bytes() == source_bytes:
            return candidate.name
    sys.exit(f"错误: 无法为 {source.name} 生成不重名的文件名")


def find_frame_nodes(workflow, keys):
    """追踪生成节点的 first_frame/last_frame 输入，返回 LoadImage 节点 ID。

    只校验 keys 里要求更新的帧；未接线的帧（如已移除的 last_frame）
    只要本次不涉及就不报错。
    """
    generator = None
    for node in workflow.values():
        if node.get("class_type") == GENERATOR_CLASS_TYPE:
            generator = node
            break
    if generator is None:
        sys.exit(f"错误: api.json 中没有 {GENERATOR_CLASS_TYPE} 节点")

    frame_nodes = {}
    for key in keys:
        field = FRAME_FIELDS[key]
        reference = generator.get("inputs", {}).get(field)
        if (not isinstance(reference, list) or len(reference) != 2
                or not isinstance(reference[0], str)):
            sys.exit(
                f"错误: {GENERATOR_CLASS_TYPE} 节点没有 {field} 输入接线，"
                "无法更新该帧"
            )
        node_id = reference[0]
        visited = set()
        while node_id not in visited:
            visited.add(node_id)
            node = workflow.get(node_id)
            if not isinstance(node, dict):
                break
            if node.get("class_type") == "LoadImage":
                frame_nodes[key] = node_id
                break
            if node.get("class_type") not in IMAGE_TRANSFORM_CLASS_TYPES:
                break
            image_input = node.get("inputs", {}).get("image")
            if (not isinstance(image_input, list) or len(image_input) != 2
                    or not isinstance(image_input[0], str)):
                break
            node_id = image_input[0]
        else:
            sys.exit(f"错误: {field} 的图像连线存在循环")
        if key not in frame_nodes:
            sys.exit(
                f"错误: {field} 引用的节点 {node_id} 无法追踪到 LoadImage 节点"
            )
    return frame_nodes


def update_api_json(api_json_path, frame_images):
    """把首/尾帧文件名写入 api.json 对应的 LoadImage 节点。"""
    api_json_path = Path(api_json_path).expanduser()
    try:
        raw_text = api_json_path.read_text(encoding="utf-8")
        workflow = json.loads(raw_text)
    except FileNotFoundError:
        sys.exit(f"错误: api.json 不存在: {api_json_path}")
    except json.JSONDecodeError as error:
        sys.exit(f"错误: 解析 api.json 失败: {error}")
    if not isinstance(workflow, dict):
        sys.exit("错误: api.json 必须是节点字典")

    frame_nodes = find_frame_nodes(workflow, frame_images)
    updated = {}
    for key, image_name in frame_images.items():
        node_id = frame_nodes[key]
        workflow[node_id].setdefault("inputs", {})["image"] = image_name
        updated[key] = (node_id, image_name)

    output = json.dumps(workflow, ensure_ascii=False, indent=2)
    if raw_text.endswith("\n"):
        output += "\n"
    with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8",
            dir=api_json_path.parent, delete=False) as temp_file:
        temp_file.write(output)
    os.replace(temp_file.name, api_json_path)
    return updated


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="设置 MiniMax H3 工作流的首帧/尾帧输入图片")
    parser.add_argument("--first", help="首帧图片路径")
    parser.add_argument("--last", help="尾帧图片路径")
    parser.add_argument("--api-json", default=str(DEFAULT_API_JSON),
                        help=f"API 工作流路径，默认 {DEFAULT_API_JSON}")
    parser.add_argument("--image-dir", default=str(DEFAULT_IMAGE_DIR),
                        help=f"输入素材目录，默认 {DEFAULT_IMAGE_DIR}")
    args = parser.parse_args(argv)

    if not args.first and not args.last:
        parser.error("至少需要提供 --first 或 --last 之一")

    frame_images = {}
    for key, path in (("first", args.first), ("last", args.last)):
        if path:
            frame_images[key] = copy_to_image_dir(path, args.image_dir)

    updated = update_api_json(args.api_json, frame_images)
    labels = {"first": "首帧", "last": "尾帧"}
    for key, (node_id, image_name) in updated.items():
        print(f"{labels[key]}: 节点 {node_id} <- {image_name}")
    print(f"已更新 {args.api_json}，图片已放入 {args.image_dir}")
    print("下一步: 运行 ./start.sh 并选择 minimax_h3.json")


if __name__ == "__main__":
    main()
