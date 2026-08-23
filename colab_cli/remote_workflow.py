# =============================================================================
# 在已安装 ComfyUI 的 Colab VM 上执行配置驱动的 API 工作流。
# 模型、输入节点和 ComfyUI 参数由 /content/workflow_config.json 描述。
# =============================================================================
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from collections import deque

COMFY = "/content/ComfyUI"
OUT_DIR = "/content/output"
OUTPUT_ARCHIVE = "/content/output.zip"
OUTPUT_TRANSFER_DIR = "/content/output_transfer"
OUTPUT_CHUNK_SIZE = 16 * 1024 * 1024
OUTPUT_STREAM_CHUNK_SIZE = 48 * 1024
OUTPUT_STREAM_PREFIX = "__COLAB_OUTPUT_V1__"
OUTPUT_TRANSFER_ACK = f"{OUTPUT_TRANSFER_DIR}/download.ack"
OUTPUT_TRANSFER_ACTIVE = f"{OUTPUT_TRANSFER_DIR}/download.active"
OUTPUT_TRANSFER_STREAM_REQUEST = f"{OUTPUT_TRANSFER_DIR}/stream.request"
OUTPUT_TRANSFER_INITIAL_WAIT_SECONDS = 30
OUTPUT_TRANSFER_STALE_SECONDS = 600
OUTPUT_TRANSFER_MAX_WAIT_SECONDS = 3600
RUN_CONFIG = "/content/colab_run_config.json"
WORKFLOW_CONFIG = "/content/workflow_config.json"
PORT = 8188
RESOURCE_SAMPLE_INTERVAL = 1
RESOURCE_DISPLAY_INTERVAL = 2
NON_TTY_RESOURCE_LOG_INTERVAL = 30
NON_TTY_PROGRESS_PERCENT_STEP = 10
RESOURCE_HISTORY_SECONDS = 30
HISTORY_POLL_INTERVAL = 30
PROGRESS_BAR_WIDTH = 28
DRIVE_MOUNT_POINT = "/content/drive"
DRIVE_MY_DRIVE_DIR = "/content/drive/MyDrive"
DRIVE_MODELS_DIR = "/content/drive/MyDrive/code/backup/models"
CUSTOM_NODES_DIR = f"{COMFY}/custom_nodes"


def _emit_structured(kind: str, payload: dict) -> None:
    """输出一行结构化协议消息（__COLAB_OUTPUT_V1__PROGRESS/METRICS）。

    仅用于非交互通道：本地 colab_transport.OutputStreamReceiver 解析为
    workflow.exec / metrics.sample 事件；payload["text"] 是与旧版逐字
    一致的人类可读文本，CLI 模式原样打印。
    """
    print(
        f"{OUTPUT_STREAM_PREFIX}{kind}:{json.dumps(payload, ensure_ascii=False)}",
        flush=True,
    )


class ResourceMonitor:
    """在后台连续采样 CPU、内存和 NVIDIA GPU 使用情况。"""

    def __init__(self, psutil_module):
        """初始化固定长度的资源采样窗口。"""
        history_size = max(
            1,
            round(RESOURCE_HISTORY_SECONDS / RESOURCE_SAMPLE_INTERVAL),
        )
        self._psutil = psutil_module
        self._gpu_samples = {}
        self._history_size = history_size
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = None
        self._cpu_percent = None
        self._memory_status = None
        self._gpu_status = []
        self._gpu_error = "GPU 信息采集中"

    def start(self) -> None:
        """立即采样一次并启动后台采样线程。"""
        self._sample()
        self._thread = threading.Thread(
            target=self._sampling_loop,
            name="resource-monitor",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """停止后台采样线程。"""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=RESOURCE_SAMPLE_INTERVAL + 5)

    def status_text(self) -> str:
        """返回适合控制台状态栏显示的最新资源摘要。"""
        with self._lock:
            cpu_percent = self._cpu_percent
            memory_status = self._memory_status
            gpu_status = [dict(item) for item in self._gpu_status]
            gpu_error = self._gpu_error

        status_parts = []
        if cpu_percent is None or memory_status is None:
            status_parts.append("CPU/内存信息不可用")
        else:
            used_gib, total_gib, memory_percent = memory_status
            status_parts.append(f"CPU {cpu_percent:.1f}%")
            status_parts.append(
                f"RAM {used_gib:.1f}/{total_gib:.1f}G {memory_percent:.1f}%"
            )

        if not gpu_status:
            status_parts.append(gpu_error)
        for gpu in gpu_status:
            status_parts.append(
                f"{gpu['label']} {gpu['name']}: 当前 {gpu['current']:.0f}% | "
                f"近{RESOURCE_HISTORY_SECONDS}s均 {gpu['average']:.1f}% "
                f"峰 {gpu['peak']:.0f}% | "
                f"VRAM {gpu['memory_used_gib']:.1f}/"
                f"{gpu['memory_total_gib']:.1f}G | {gpu['temperature']:.0f}°C"
            )
        return " | ".join(status_parts)

    def sample_dict(self) -> dict:
        """返回最新采样的结构化指标（供 METRICS 前缀协议使用）。"""
        with self._lock:
            cpu_percent = self._cpu_percent
            memory_status = self._memory_status
            gpu_status = [dict(item) for item in self._gpu_status]

        sample = {}
        if cpu_percent is not None:
            sample["cpu_percent"] = round(cpu_percent, 1)
        if memory_status is not None:
            used_gib, total_gib, _percent = memory_status
            sample["ram_used_mb"] = round(used_gib * 1024)
            sample["ram_total_mb"] = round(total_gib * 1024)
        if gpu_status:
            gpu = gpu_status[0]
            sample["gpu_util"] = round(gpu["current"], 1)
            sample["gpu_util_avg30"] = round(gpu["average"], 1)
            sample["gpu_util_peak"] = round(gpu["peak"], 1)
            sample["gpu_name"] = gpu["name"]
            sample["vram_used_mb"] = round(gpu["memory_used_gib"] * 1024)
            sample["vram_total_mb"] = round(gpu["memory_total_gib"] * 1024)
        return sample

    def _sampling_loop(self) -> None:
        """按固定间隔持续采样，直到收到停止信号。"""
        while not self._stop_event.wait(RESOURCE_SAMPLE_INTERVAL):
            self._sample()

    def _sample(self) -> None:
        """采集一份主机和 GPU 快照并更新滑动窗口。"""
        cpu_percent = None
        memory_status = None
        try:
            cpu_percent = self._psutil.cpu_percent(interval=None)
            memory = self._psutil.virtual_memory()
            memory_status = (
                memory.used / 1024**3,
                memory.total / 1024**3,
                memory.percent,
            )
        except Exception:
            pass

        try:
            gpu_rows = self._read_nvidia_gpu_status()
            gpu_error = "GPU 信息不可用" if not gpu_rows else ""
        except FileNotFoundError:
            gpu_rows = []
            gpu_error = "未检测到 NVIDIA GPU"
        except (OSError, subprocess.SubprocessError, ValueError):
            gpu_rows = []
            gpu_error = "GPU 信息暂时不可用"

        with self._lock:
            self._cpu_percent = cpu_percent
            self._memory_status = memory_status
            self._gpu_error = gpu_error
            current_gpu_status = []
            for index, gpu in enumerate(gpu_rows):
                samples = self._gpu_samples.setdefault(
                    index,
                    deque(maxlen=self._history_size),
                )
                samples.append(gpu["current"])
                current_gpu_status.append({
                    **gpu,
                    "label": "GPU" if len(gpu_rows) == 1 else f"GPU{index}",
                    "average": sum(samples) / len(samples),
                    "peak": max(samples),
                })
            self._gpu_status = current_gpu_status

    @staticmethod
    def _read_nvidia_gpu_status() -> list[dict]:
        """通过 nvidia-smi 读取当前 NVIDIA GPU 快照。"""
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        gpu_rows = []
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            values = [value.strip() for value in line.split(",", maxsplit=4)]
            if len(values) != 5:
                continue
            name, utilization, memory_used, memory_total, temperature = values
            gpu_rows.append({
                "name": name,
                "current": float(utilization),
                "memory_used_gib": float(memory_used) / 1024,
                "memory_total_gib": float(memory_total) / 1024,
                "temperature": float(temperature),
            })
        return gpu_rows


class ProgressDisplay:
    """不依赖第三方库的工作流进度与服务器状态显示器。"""

    def __init__(self, total_nodes: int, interactive: bool) -> None:
        """初始化节点计数和输出模式。"""
        self.total_nodes = max(1, total_nodes)
        self.interactive = interactive
        self.completed_nodes = 0
        self.current_node = "等待执行"
        self.internal_progress = None
        self.resource_text = "服务器 | 信息采集中"
        self._rendered_dynamic_lines = False
        self._last_logged_workflow = None

    @staticmethod
    def _bar(value: float, maximum: float, width: int = PROGRESS_BAR_WIDTH) -> str:
        """生成固定宽度的 ASCII 进度条。"""
        ratio = 0.0 if maximum <= 0 else value / maximum
        ratio = min(1.0, max(0.0, ratio))
        filled = round(ratio * width)
        return f"[{'#' * filled}{'-' * (width - filled)}]"

    def update_workflow(
        self,
        completed_nodes: int,
        current_node: str = "",
        internal_progress: tuple[float, float] | None = None,
        force: bool = False,
    ) -> None:
        """更新节点总进度和可选的节点内部采样进度。"""
        self.completed_nodes = min(max(0, completed_nodes), self.total_nodes)
        if current_node:
            self.current_node = current_node
        self.internal_progress = internal_progress
        self._render(force=force)

    def update_resource(
        self, resource_text: str, metrics: dict | None = None
    ) -> None:
        """更新服务器资源状态行。"""
        self.resource_text = resource_text
        if self.interactive:
            self._render(force=True)
        else:
            payload = {"text": resource_text}
            if metrics:
                payload.update(metrics)
            _emit_structured("METRICS", payload)

    def write_message(self, message: str) -> None:
        """输出普通消息，并在 TTY 中保留动态双行显示。"""
        if self.interactive and self._rendered_dynamic_lines:
            self._clear_dynamic_lines()
            print(message, flush=True)
            self._rendered_dynamic_lines = False
            self._render(force=True)
            return
        print(message, flush=True)

    def close(self) -> None:
        """结束动态显示并将光标留在下一行。"""
        if self.interactive:
            self._render(force=True)

    def _workflow_text(self) -> str:
        """构建工作流进度行。"""
        percentage = self.completed_nodes / self.total_nodes * 100
        text = (
            f"工作流 {self._bar(self.completed_nodes, self.total_nodes)} "
            f"{self.completed_nodes}/{self.total_nodes} "
            f"({percentage:5.1f}%) | 当前 {self.current_node}"
        )
        if self.internal_progress is not None:
            value, maximum = self.internal_progress
            internal_percentage = 0.0 if maximum <= 0 else value / maximum * 100
            text += (
                f" | 节点 {self._bar(value, maximum, width=16)} "
                f"{value:g}/{maximum:g} ({internal_percentage:5.1f}%)"
            )
        return text

    def _render(self, force: bool = False) -> None:
        """根据终端类型刷新双行或输出一条结构化进度协议行。"""
        workflow_text = self._workflow_text()
        if not self.interactive:
            if force or workflow_text != self._last_logged_workflow:
                payload = {
                    "text": workflow_text,
                    "completed_nodes": self.completed_nodes,
                    "total_nodes": self.total_nodes,
                    "current_node": self.current_node,
                    "percent": round(
                        self.completed_nodes / self.total_nodes * 100, 1
                    ),
                }
                if self.internal_progress is not None:
                    value, maximum = self.internal_progress
                    payload["step"] = value
                    payload["steps"] = maximum
                _emit_structured("PROGRESS", payload)
                self._last_logged_workflow = workflow_text
            return

        if self._rendered_dynamic_lines:
            self._clear_dynamic_lines()
        sys.stdout.write(f"\r\033[2K{workflow_text}\n")
        sys.stdout.write(f"\r\033[2K{self.resource_text}\n")
        sys.stdout.flush()
        self._rendered_dynamic_lines = True

    @staticmethod
    def _clear_dynamic_lines() -> None:
        """清除刚刚绘制的两行并把光标移回首行。"""
        sys.stdout.write("\033[2A\r\033[2K\033[1B\r\033[2K\033[1A")


def copy_with_progress(src, dst):
    """将文件原子复制到目标位置并定期打印进度。"""
    file_size = os.path.getsize(src)
    print(f"正在复制 (大小: {file_size / 1024**3:.2f} GB): {os.path.basename(src)}", flush=True)
    partial_dst = f"{dst}.part"
    try:
        with open(src, "rb") as source, open(partial_dst, "wb") as target:
            copied = 0
            last_print = time.time()
            while True:
                chunk = source.read(1024 * 1024 * 16)
                if not chunk:
                    break
                target.write(chunk)
                copied += len(chunk)
                now = time.time()
                if now - last_print > 3:
                    percent = copied / file_size * 100 if file_size else 100
                    print(
                        f"复制进度: {copied / 1024**3:.2f} GB / "
                        f"{file_size / 1024**3:.2f} GB ({percent:.1f}%)",
                        flush=True,
                    )
                    last_print = now
        os.replace(partial_dst, dst)
    except Exception:
        if os.path.exists(partial_dst):
            os.remove(partial_dst)
        raise
    print(f"复制完成: {os.path.basename(dst)}", flush=True)


def drive_is_mounted() -> bool:
    """检查 Google Drive 是否已挂载且 MyDrive 目录可访问。"""
    if not os.path.ismount(DRIVE_MOUNT_POINT):
        print("未检测到已挂载的 Google Drive", flush=True)
        return False

    try:
        with os.scandir(DRIVE_MY_DRIVE_DIR) as entries:
            next(entries, None)
    except OSError as error:
        print(f"Google Drive 已挂载但不可访问: {error}", flush=True)
        return False

    print("检测到 Google Drive 已挂载，将直接复用且不会再次认证", flush=True)
    return True


def load_json(path, description):
    """读取必需的 JSON 配置。"""
    if not os.path.isfile(path):
        raise FileNotFoundError(f"缺少{description}: {path}")
    with open(path, encoding="utf-8") as config_file:
        return json.load(config_file)


def safe_relative_path(path, description):
    """验证配置中的路径不会逃逸其预期根目录。"""
    normalized = os.path.normpath(path)
    if (os.path.isabs(path) or normalized == ".." or
            normalized.startswith(f"..{os.sep}")):
        raise ValueError(f"{description}必须是安全的相对路径: {path}")
    return normalized


def valid_model_file(path, expected_size=None):
    """检查模型文件是否存在、非空且符合可选的精确大小。"""
    if not os.path.isfile(path) or os.path.getsize(path) <= 0:
        return False
    return expected_size is None or os.path.getsize(path) == expected_size


def link_or_copy(source, target):
    """优先创建模型软链接，失败时回退为原子复制。"""
    if os.path.islink(target) or os.path.exists(target):
        os.remove(target)
    try:
        os.symlink(source, target)
    except OSError as error:
        print(f"软链接失败({error})，改为复制文件...", flush=True)
        copy_with_progress(source, target)


def run_command(command, cwd=None):
    """执行子进程，并实时显示安装输出。"""
    print("+", " ".join(str(part) for part in command), flush=True)
    subprocess.run(command, cwd=cwd, check=True)


def prepare_custom_nodes(workflow_config):
    """安装工作流声明的第三方节点及其 Python 依赖。"""
    custom_nodes = workflow_config.get("custom_nodes", [])
    if not isinstance(custom_nodes, list):
        raise TypeError("workflow_config.custom_nodes 必须是列表")

    print(f">>> [1/4] 准备第三方节点 ({len(custom_nodes)} 个)", flush=True)
    os.makedirs(CUSTOM_NODES_DIR, exist_ok=True)
    for node in custom_nodes:
        if not isinstance(node, dict):
            raise TypeError("workflow_config.custom_nodes 中的每项必须是对象")
        name = node.get("name")
        repo = node.get("repo")
        if not all(isinstance(value, str) and value for value in (name, repo)):
            raise ValueError("每个第三方节点必须配置 name 和 repo")
        name = safe_relative_path(name, "custom_nodes.name")
        if os.sep in name:
            raise ValueError(f"custom_nodes.name 只能是单层目录名: {name}")

        node_dir = os.path.join(CUSTOM_NODES_DIR, name)
        if os.path.isdir(node_dir):
            print(f"第三方节点已存在，跳过克隆: {name}", flush=True)
        else:
            run_command([
                "git", "clone", "--depth", "1", repo, node_dir,
            ])

        marker_path = os.path.join(node_dir, ".colab_dependencies_ready")
        if os.path.isfile(marker_path):
            print(f"第三方节点依赖已安装，跳过: {name}", flush=True)
            continue

        requirements = node.get("requirements", ["requirements.txt"])
        if not isinstance(requirements, list) or not all(
                isinstance(item, str) for item in requirements):
            raise TypeError("custom_nodes.requirements 必须是字符串列表")
        for requirement in requirements:
            requirement = safe_relative_path(
                requirement, "custom_nodes.requirements")
            requirement_path = os.path.join(node_dir, requirement)
            if os.path.isfile(requirement_path):
                run_command([
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "-r",
                    requirement_path,
                ])

        install_scripts = node.get("install_scripts", [])
        if not isinstance(install_scripts, list) or not all(
                isinstance(item, str) for item in install_scripts):
            raise TypeError("custom_nodes.install_scripts 必须是字符串列表")
        for install_script in install_scripts:
            install_script = safe_relative_path(
                install_script, "custom_nodes.install_scripts")
            install_path = os.path.join(node_dir, install_script)
            if not os.path.isfile(install_path):
                raise FileNotFoundError(
                    f"第三方节点安装脚本不存在: {install_path}"
                )
            run_command([sys.executable, install_path], cwd=node_dir)

        with open(marker_path, "w", encoding="utf-8") as marker_file:
            marker_file.write("ready\n")
        print(f"第三方节点就绪: {name}", flush=True)


def prepare_models(workflow_config, mount_drive):
    """按工作流配置准备模型，并复用可用的 Drive 缓存。"""
    models = workflow_config.get("models", [])
    if not isinstance(models, list):
        raise TypeError("workflow_config.models 必须是列表")

    print(f">>> [2/4] 准备模型 ({len(models)} 个)", flush=True)
    if mount_drive:
        print(">>> 检查 Google Drive 现有挂载状态...", flush=True)
        has_drive = drive_is_mounted()
        if not has_drive:
            print(
                "不会调用 drive.mount() 或触发认证，改用 VM 本地模型缓存",
                flush=True,
            )
    else:
        print("已按本次启动配置禁用 Google Drive", flush=True)
        has_drive = False
    if has_drive:
        print(f"Google Drive 已就绪，模型存储目录: {DRIVE_MODELS_DIR}", flush=True)
        os.makedirs(DRIVE_MODELS_DIR, exist_ok=True)
    else:
        print("未检测到 Google Drive 挂载，将在 VM 本地下载模型", flush=True)

    if not models:
        print("当前工作流未配置需要下载的模型", flush=True)
        return

    for model in models:
        if not isinstance(model, dict) or model.get("repo"):
            continue
        filename = model.get("filename")
        comfy_dir = model.get("comfy_dir")
        if not all(isinstance(value, str) and value for value in
                   (filename, comfy_dir)):
            raise ValueError("每个模型必须配置 filename 和 comfy_dir")
        comfy_dir = safe_relative_path(comfy_dir, "comfy_dir")
        cache_path = safe_relative_path(
            model.get("cache_path", filename), "cache_path")
        expected_size = model.get("size_bytes")
        local_target = os.path.join(
            COMFY, "models", comfy_dir, os.path.basename(filename))
        drive_target = os.path.join(DRIVE_MODELS_DIR, cache_path)
        if valid_model_file(local_target, expected_size):
            continue
        if has_drive and valid_model_file(drive_target, expected_size):
            continue
        raise FileNotFoundError(
            f"模型 {filename} 没有配置公开下载源。请先将正确文件放到 "
            f"Google Drive: {drive_target}，然后挂载 Drive 后重试。"
        )

    os.environ["HF_HUB_ENABLE_HF_TRANSFER"] = "1"
    from huggingface_hub import hf_hub_download

    for model in models:
        if not isinstance(model, dict):
            raise TypeError("workflow_config.models 中的每项必须是对象")
        repo = model.get("repo")
        filename = model.get("filename")
        comfy_dir = model.get("comfy_dir")
        if not all(isinstance(value, str) and value for value in
                   (filename, comfy_dir)):
            raise ValueError("每个模型必须配置 filename 和 comfy_dir")
        if repo is not None and not isinstance(repo, str):
            raise TypeError("模型 repo 必须是字符串或省略")

        comfy_dir = safe_relative_path(comfy_dir, "comfy_dir")
        cache_path = safe_relative_path(
            model.get("cache_path", filename), "cache_path")
        expected_size = model.get("size_bytes")
        if expected_size is not None and not isinstance(expected_size, int):
            raise TypeError("模型 size_bytes 必须是整数")

        dest_dir = os.path.join(COMFY, "models", comfy_dir)
        os.makedirs(dest_dir, exist_ok=True)
        target = os.path.join(dest_dir, os.path.basename(filename))

        if os.path.islink(target) and not os.path.exists(target):
            print(f"检测到死链接, 清理: {target}", flush=True)
            os.remove(target)
        elif valid_model_file(target, expected_size):
            print("已存在, 跳过:", filename, flush=True)
            continue

        drive_target = (
            os.path.join(DRIVE_MODELS_DIR, cache_path) if has_drive else None)

        if drive_target and valid_model_file(drive_target, expected_size):
            print(f"[Google Drive] 命中缓存模型: {drive_target} -> {target}", flush=True)
            link_or_copy(drive_target, target)
            continue

        if not repo:
            expected_location = drive_target or os.path.join(
                DRIVE_MODELS_DIR, cache_path)
            raise FileNotFoundError(
                f"模型 {filename} 没有配置公开下载源。请先将正确文件放到 "
                f"Google Drive: {expected_location}，然后挂载 Drive 后重试。"
            )

        print(f"未在 Google Drive 找到，开始下载 {repo}/{filename}...", flush=True)
        downloaded = hf_hub_download(repo, filename)

        if drive_target:
            os.makedirs(os.path.dirname(drive_target), exist_ok=True)
            print(f"正在保存到 Google Drive 备份: {drive_target} ...", flush=True)
            copy_with_progress(downloaded, drive_target)
            link_or_copy(drive_target, target)
        else:
            link_or_copy(downloaded, target)
        print(f"模型就绪: {filename}", flush=True)


def copy_input_files(run_config):
    """把本地调度器上传的暂存文件复制到 ComfyUI input。"""
    input_files = run_config.get("input_files", [])
    if not isinstance(input_files, list):
        raise TypeError("colab_run_config.input_files 必须是列表")

    print(f">>> [3/4] 拷贝输入素材 ({len(input_files)} 个)", flush=True)
    os.makedirs(f"{COMFY}/input", exist_ok=True)
    for item in input_files:
        if not isinstance(item, dict):
            raise TypeError("input_files 中的每项必须是对象")
        source = item.get("remote_path")
        workflow_path = item.get("workflow_path")
        if not isinstance(source, str) or not isinstance(workflow_path, str):
            raise ValueError("每个输入必须配置 remote_path 和 workflow_path")
        if not os.path.isfile(source):
            raise FileNotFoundError(f"缺少已上传的输入文件: {source}")
        workflow_path = safe_relative_path(workflow_path, "workflow_path")
        destination = os.path.join(COMFY, "input", workflow_path)
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        shutil.copy2(source, destination)
        print(f"输入就绪: {workflow_path}", flush=True)


def reset_output_directory() -> None:
    """重建本次任务的临时输出目录，避免打包先前任务的结果。"""
    if os.path.lexists(OUT_DIR):
        if os.path.islink(OUT_DIR) or os.path.isfile(OUT_DIR):
            os.remove(OUT_DIR)
        else:
            shutil.rmtree(OUT_DIR)
    if os.path.lexists(OUTPUT_ARCHIVE):
        if os.path.islink(OUTPUT_ARCHIVE) or os.path.isfile(OUTPUT_ARCHIVE):
            os.remove(OUTPUT_ARCHIVE)
        else:
            shutil.rmtree(OUTPUT_ARCHIVE)
    if os.path.lexists(OUTPUT_TRANSFER_DIR):
        if os.path.islink(OUTPUT_TRANSFER_DIR) or os.path.isfile(
            OUTPUT_TRANSFER_DIR
        ):
            os.remove(OUTPUT_TRANSFER_DIR)
        else:
            shutil.rmtree(OUTPUT_TRANSFER_DIR)
    os.makedirs(OUT_DIR)
    print(">>> 已清理本次任务输出目录", flush=True)


def file_sha256(path: str) -> str:
    """计算文件的 SHA-256。"""
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def prepare_output_transfer() -> None:
    """打包输出、准备分块并和本地下载端完成交接。"""
    archive_path = shutil.make_archive(
        os.path.splitext(OUTPUT_ARCHIVE)[0],
        "zip",
        OUT_DIR,
    )
    archive_size = os.path.getsize(archive_path)
    archive_sha256 = file_sha256(archive_path)
    os.makedirs(OUTPUT_TRANSFER_DIR, exist_ok=True)

    parts = []
    with open(archive_path, "rb") as archive_file:
        part_index = 0
        while chunk := archive_file.read(OUTPUT_CHUNK_SIZE):
            part_name = f"part-{part_index:05d}.bin"
            part_path = os.path.join(OUTPUT_TRANSFER_DIR, part_name)
            with open(part_path, "wb") as part_file:
                part_file.write(chunk)
            parts.append({
                "name": part_name,
                "size": len(chunk),
                "sha256": hashlib.sha256(chunk).hexdigest(),
            })
            part_index += 1

    manifest = {
        "version": 1,
        "archive_name": os.path.basename(archive_path),
        "archive_size": archive_size,
        "archive_sha256": archive_sha256,
        "chunk_size": OUTPUT_CHUNK_SIZE,
        "parts": parts,
    }
    manifest_path = os.path.join(OUTPUT_TRANSFER_DIR, "manifest.json")
    temporary_manifest_path = f"{manifest_path}.part"
    with open(temporary_manifest_path, "w", encoding="utf-8") as manifest_file:
        json.dump(manifest, manifest_file, ensure_ascii=False, indent=2)
        manifest_file.flush()
        os.fsync(manifest_file.fileno())
    os.replace(temporary_manifest_path, manifest_path)

    print(
        f">>> 完成, 输出包 {archive_size / 1024**2:.1f} MiB，"
        f"已准备 {len(parts)} 个下载分块: {OUTPUT_TRANSFER_DIR}",
        flush=True,
    )
    wait_for_output_transfer_client(
        archive_path,
        archive_size,
        archive_sha256,
        len(parts),
    )


def wait_for_output_transfer_client(
    archive_path: str,
    archive_size: int,
    archive_sha256: str,
    part_count: int,
) -> None:
    """在 VM 仍存活时等待本地下载；失败时回退到 stdout 流。"""
    metadata = json.dumps(
        {
            "version": 1,
            "size": archive_size,
            "sha256": archive_sha256,
            "parts": part_count,
        },
        separators=(",", ":"),
    )
    print(f"{OUTPUT_STREAM_PREFIX}READY:{metadata}", flush=True)

    started_at = time.monotonic()
    initial_deadline = started_at + OUTPUT_TRANSFER_INITIAL_WAIT_SECONDS
    maximum_deadline = started_at + OUTPUT_TRANSFER_MAX_WAIT_SECONDS
    next_heartbeat = started_at + 30
    while time.monotonic() < maximum_deadline:
        if os.path.isfile(OUTPUT_TRANSFER_ACK):
            print(
                ">>> 本地已确认输出下载和完整性校验，结束远端任务",
                flush=True,
            )
            return
        if os.path.isfile(OUTPUT_TRANSFER_STREAM_REQUEST):
            print(
                ">>> 本地请求改用执行通道传输输出",
                flush=True,
            )
            break

        now = time.monotonic()
        active_is_fresh = False
        try:
            active_age = time.time() - os.path.getmtime(OUTPUT_TRANSFER_ACTIVE)
            active_is_fresh = active_age <= OUTPUT_TRANSFER_STALE_SECONDS
        except OSError:
            pass

        if now >= initial_deadline and not active_is_fresh:
            print(
                ">>> 未检测到有效的 Contents API 下载端，"
                "改用执行通道兜底传输",
                flush=True,
            )
            break
        if now >= next_heartbeat:
            print(
                ">>> 正在等待本地完成输出分块下载...",
                flush=True,
            )
            next_heartbeat = now + 30
        time.sleep(1)
    else:
        print(
            ">>> 等待本地下载确认超时，改用执行通道兜底传输",
            flush=True,
        )

    stream_output_archive(archive_path, archive_size, archive_sha256)


def stream_output_archive(
    archive_path: str,
    archive_size: int,
    archive_sha256: str,
) -> None:
    """通过当前 Jupyter 输出流发送最终 ZIP，避免依赖 Contents API。"""
    metadata = json.dumps(
        {"size": archive_size, "sha256": archive_sha256},
        separators=(",", ":"),
    )
    print(f"{OUTPUT_STREAM_PREFIX}BEGIN:{metadata}", flush=True)
    with open(archive_path, "rb") as archive_file:
        chunk_index = 0
        while chunk := archive_file.read(OUTPUT_STREAM_CHUNK_SIZE):
            encoded_chunk = base64.b64encode(chunk).decode("ascii")
            print(
                f"{OUTPUT_STREAM_PREFIX}CHUNK:{chunk_index}:{encoded_chunk}",
                flush=True,
            )
            chunk_index += 1
    print(f"{OUTPUT_STREAM_PREFIX}END:{chunk_index}", flush=True)


def main():
    """准备工作流专属资源，启动 ComfyUI 并执行 API 工作流。"""
    if not os.path.isfile(f"{COMFY}/requirements.txt"):
        raise RuntimeError("ComfyUI 尚未安装，请先执行 remote_setup.py")

    load_json("/content/api.json", "API 工作流")
    workflow_config = load_json(WORKFLOW_CONFIG, "工作流配置")
    run_config = load_json(RUN_CONFIG, "运行配置")
    workflow_name = workflow_config.get("name", "未命名工作流")
    print(f">>> 工作流: {workflow_name}", flush=True)

    prepare_custom_nodes(workflow_config)
    prepare_models(workflow_config, run_config.get("mount_drive", True))
    copy_input_files(run_config)

    print(">>> [4/4] 启动 ComfyUI 并运行工作流", flush=True)
    reset_output_directory()
    log_path = "/content/comfyui.log"
    flags = workflow_config.get("comfy_flags", [])
    if not isinstance(flags, list) or not all(
            isinstance(flag, str) for flag in flags):
        raise TypeError("workflow_config.comfy_flags 必须是字符串列表")
    flags = list(flags)
    flags_file = "/content/comfy_flags.txt"
    if os.path.exists(flags_file):
        import shlex
        with open(flags_file) as ff:
            flags.extend(shlex.split(ff.read().strip()))
    if flags:
        print("ComfyUI 启动参数:", flags, flush=True)
    log = open(log_path, "w")
    server = subprocess.Popen(
        [sys.executable, "main.py", "--port", str(PORT),
         "--disable-auto-launch", *flags],
        cwd=COMFY, stdout=log, stderr=subprocess.STDOUT)
    try:
        for _ in range(120):
            if server.poll() is not None:
                raise RuntimeError(
                    f"ComfyUI 进程启动后立即退出 (exit code: {server.returncode})")
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{PORT}/system_stats",
                    timeout=2,
                ):
                    pass
                break
            except Exception:
                time.sleep(5)
        else:
            raise RuntimeError("ComfyUI 启动超时, 日志: " + log_path)
        with urllib.request.urlopen(
            f"http://127.0.0.1:{PORT}/object_info",
            timeout=30,
        ) as response:
            object_info = json.load(response)
        workflow = load_json("/content/api.json", "API 工作流")
        missing_node_types = sorted({
            node.get("class_type")
            for node in workflow.values()
            if isinstance(node.get("class_type"), str)
            and node.get("class_type") not in object_info
        })
        if missing_node_types:
            raise RuntimeError(
                "ComfyUI 缺少工作流节点: " + ", ".join(missing_node_types)
            )
        run_workflow(server)
    except Exception:
        log.flush()
        print("---- comfyui.log 尾部 ----")
        with open(log_path) as lf:
            print(lf.read()[-3000:])
        raise
    finally:
        server.terminate()
        try:
            server.wait(timeout=15)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=15)
        log.close()

    prepare_output_transfer()


def run_workflow(server_proc):
    """通过 WebSocket 实时追踪 ComfyUI 推理进度, 带 HTTP 回退检测"""
    with open("/content/api.json") as f:
        wf = json.load(f)

    import psutil
    import websocket  # websocket-client

    interactive_tty = bool(
        getattr(sys.stdout, "isatty", None) and sys.stdout.isatty()
    )
    resource_display_interval = (
        RESOURCE_DISPLAY_INTERVAL
        if interactive_tty
        else NON_TTY_RESOURCE_LOG_INTERVAL
    )
    last_logged_internal_progress = {}

    def http_get(path):
        with urllib.request.urlopen(
            f"http://127.0.0.1:{PORT}{path}", timeout=10
        ) as response:
            return json.load(response)

    def post(path, payload):
        req = urllib.request.Request(
            f"http://127.0.0.1:{PORT}{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.load(response)

    def check_history_completed(pid):
        """HTTP 回退: 直接查询 /history 判断是否已完成"""
        try:
            history = http_get(f"/history/{pid}")
            if pid not in history:
                return False, None
            entry = history[pid]
            status = entry.get("status", {})
            if status.get("completed"):
                return True, entry
            if status.get("status_str") == "error":
                raise RuntimeError("工作流执行出错: " + json.dumps(
                    status.get("messages", []), ensure_ascii=False))
        except (urllib.error.URLError, OSError):
            pass
        return False, None

    def write_status(message):
        """写入一条不会破坏动态进度栏的消息。"""
        progress_display.write_message(message)

    def update_resource_display(started_at):
        """更新服务器资源状态。"""
        elapsed_seconds = int(time.monotonic() - started_at)
        description = (
            f"服务器 | {resource_monitor.status_text()} | "
            f"运行 {elapsed_seconds}s"
        )
        metrics = resource_monitor.sample_dict()
        metrics["elapsed_seconds"] = elapsed_seconds
        progress_display.update_resource(description, metrics)

    def update_workflow_bar(
        completed_count,
        current_node="",
        internal_progress=None,
        force=False,
    ):
        """更新工作流节点进度条。"""
        progress_display.update_workflow(
            completed_count,
            current_node,
            internal_progress,
            force=force,
        )

    def log_internal_progress(node, class_type, value, maximum):
        """在非 TTY 日志中按百分比步长输出节点内部进度。"""
        if maximum <= 0:
            return
        percentage = min(100, max(0, value / maximum * 100))
        progress_bucket = int(percentage // NON_TTY_PROGRESS_PERCENT_STEP)
        if percentage >= 100:
            progress_bucket = 100 // NON_TTY_PROGRESS_PERCENT_STEP
        progress_key = (node, maximum)
        if last_logged_internal_progress.get(progress_key) == progress_bucket:
            return
        last_logged_internal_progress[progress_key] = progress_bucket
        update_workflow_bar(
            len(completed_nodes),
            f"{node}: {class_type}",
            (value, maximum),
        )

    # 先连 WebSocket, 再提交 prompt, 避免错过完成事件
    client_id = str(uuid.uuid4())
    ws = websocket.WebSocket()
    ws.connect(f"ws://127.0.0.1:{PORT}/ws?clientId={client_id}")
    ws.settimeout(1)

    prompt_id = post(
        "/prompt", {"prompt": wf, "client_id": client_id})["prompt_id"]
    print(f"prompt_id: {prompt_id}", flush=True)

    resource_monitor = ResourceMonitor(psutil)
    resource_monitor.start()
    workflow_started_at = time.monotonic()
    last_resource_display = workflow_started_at
    last_history_poll = workflow_started_at
    total_nodes = len(wf)
    completed_nodes = set()
    current_node_id = None
    last_node_type = ""
    entry = None
    progress_display = ProgressDisplay(total_nodes, interactive_tty)
    if not interactive_tty:
        print(
            "检测到非交互式日志通道；使用 ASCII 进度条，"
            "节点内部进度按 10% 输出，服务器状态每 30 秒输出一次。",
            flush=True,
        )
    update_workflow_bar(0, "等待执行", force=True)
    update_resource_display(workflow_started_at)

    try:
        while True:
            # 检查 ComfyUI 进程是否存活
            if server_proc.poll() is not None:
                raise RuntimeError(
                    f"ComfyUI 进程已崩溃 (exit code: {server_proc.returncode})")
            msg = None
            try:
                raw = ws.recv()
                if isinstance(raw, str):
                    msg = json.loads(raw)
            except websocket.WebSocketTimeoutException:
                now = time.monotonic()
                if now - last_history_poll >= HISTORY_POLL_INTERVAL:
                    completed, hist_entry = check_history_completed(prompt_id)
                    last_history_poll = now
                    if completed:
                        entry = hist_entry
                        update_workflow_bar(total_nodes, "完成", force=True)
                        write_status(
                            "✅ 工作流执行完成! (HTTP 回退检测)",
                        )
                        break
            except (ConnectionError, websocket.WebSocketConnectionClosedException):
                # WebSocket 断了, 用 HTTP 回退最后检查一次
                completed, hist_entry = check_history_completed(prompt_id)
                if completed:
                    entry = hist_entry
                    update_workflow_bar(total_nodes, "完成", force=True)
                    write_status(
                        "✅ 工作流执行完成! (连接断开后回退检测)",
                    )
                    break
                raise RuntimeError("ComfyUI WebSocket 连接断开, 服务可能已崩溃")

            now = time.monotonic()
            if now - last_resource_display >= resource_display_interval:
                update_resource_display(workflow_started_at)
                last_resource_display = now

            if msg is None:
                continue

            msg_type = msg.get("type")
            data = msg.get("data", {})

            if msg_type == "progress":
                val = data.get("value", 0)
                mx = data.get("max", 1)
                node = data.get("node", "")
                node_info = wf.get(node, {})
                class_type = node_info.get("class_type", last_node_type or "?")
                last_node_type = class_type
                if interactive_tty:
                    update_workflow_bar(
                        len(completed_nodes),
                        f"{node}: {class_type}",
                        (val, mx),
                    )
                else:
                    log_internal_progress(node, class_type, val, mx)

            elif msg_type == "executing":
                node = data.get("node")
                if node is None and data.get("prompt_id") == prompt_id:
                    if current_node_id in wf:
                        completed_nodes.add(current_node_id)
                    update_workflow_bar(total_nodes, "完成", force=True)
                    write_status("✅ 工作流执行完成!")
                    break
                elif node:
                    if current_node_id and current_node_id != node:
                        if current_node_id in wf:
                            completed_nodes.add(current_node_id)
                    current_node_id = node
                    node_info = wf.get(node, {})
                    class_type = node_info.get("class_type", "?")
                    last_node_type = class_type
                    current_label = f"{node}: {class_type}"
                    update_workflow_bar(
                        len(completed_nodes), current_label, force=True)

            elif msg_type == "execution_error":
                raise RuntimeError("工作流执行出错: " + json.dumps(
                    data, ensure_ascii=False))

            elif msg_type == "execution_cached":
                nodes = data.get("nodes", [])
                if nodes:
                    completed_nodes.update(node for node in nodes if node in wf)
                    write_status(f"已缓存节点 (跳过): {nodes}")
                    update_workflow_bar(len(completed_nodes), force=True)

    finally:
        ws.close()
        resource_monitor.stop()
        progress_display.close()

    # 从 history 获取输出文件 (如果 HTTP 回退已拿到 entry 则复用)
    if entry is None:
        history = http_get(f"/history/{prompt_id}")
        entry = history.get(prompt_id, {})

    saved = 0
    for node_out in entry.get("outputs", {}).values():
        for key in ("images", "gifs", "videos", "audio"):
            for item in node_out.get(key, []):
                if not isinstance(item, dict):
                    continue
                filename = item.get("filename")
                subfolder = item.get("subfolder", "")
                output_type = item.get("type", "output")
                if not isinstance(filename, str) or not filename:
                    continue
                if not isinstance(subfolder, str):
                    raise ValueError("ComfyUI 输出 subfolder 必须是字符串")
                filename = safe_relative_path(filename, "输出文件名")
                subfolder = (
                    safe_relative_path(subfolder, "输出子目录")
                    if subfolder else ""
                )
                query = urllib.parse.urlencode({
                    "filename": filename,
                    "subfolder": subfolder,
                    "type": output_type,
                })
                url = f"http://127.0.0.1:{PORT}/view?{query}"
                dest = os.path.join(OUT_DIR, subfolder, filename)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                partial_dest = f"{dest}.part"
                try:
                    with (
                        urllib.request.urlopen(url, timeout=300) as response,
                        open(partial_dest, "wb") as output_file,
                    ):
                        shutil.copyfileobj(
                            response,
                            output_file,
                            length=1024 * 1024,
                        )
                        output_file.flush()
                        os.fsync(output_file.fileno())
                    os.replace(partial_dest, dest)
                except Exception:
                    if os.path.exists(partial_dest):
                        os.remove(partial_dest)
                    raise
                print("已保存:", dest, flush=True)
                saved += 1

    if saved == 0:
        raise RuntimeError("工作流已结束，但 history 中没有可下载的输出文件")


if __name__ == "__main__":
    main()
