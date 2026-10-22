"""多后端（colab / kaggle / modal）选择与 Kaggle/Modal 通道的离线测试。"""
import hashlib
import io
import json
import subprocess
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from colab_core.config import TaskConfig, resolve_backend
from colab_transport import CliTransport, get_transport
from colab_transport.batch import fetch_manifest_output, forward_remote_line
from colab_transport.kaggle_backend import KaggleTransport, kernel_slug, parse_status
from colab_transport.modal_backend import DRIVE_MODELS_DIR, ModalTransport

HERE = Path(__file__).resolve().parent.parent
WORKFLOW = HERE / "workflows" / "minimax_h3.json"


def make_transfer(files: dict) -> dict:
    """生成远端 output_transfer 目录内容：{name: bytes}（与 remote_workflow 格式一致）。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("out/video.mp4", b"x" * 3000)
    data = buffer.getvalue()
    parts, out = [], {}
    for index in range(0, len(data), 1000):
        chunk = data[index:index + 1000]
        name = f"part-{index // 1000:05d}.bin"
        out[name] = chunk
        parts.append({"name": name, "size": len(chunk), "sha256": hashlib.sha256(chunk).hexdigest()})
    out["manifest.json"] = json.dumps({
        "version": 1, "archive_name": "output.zip", "archive_size": len(data),
        "archive_sha256": hashlib.sha256(data).hexdigest(), "chunk_size": 1000, "parts": parts,
    }).encode()
    files.update(out)
    return out


# ── 选择 ────────────────────────────────────────────────────
def test_backend_defaults_to_colab(monkeypatch):
    monkeypatch.delenv("COMPUTE_BACKEND", raising=False)
    assert TaskConfig(workflow_config=WORKFLOW).backend == "colab"
    monkeypatch.setenv("COMPUTE_BACKEND", "modal")
    assert resolve_backend(None) == "modal"
    with pytest.raises(ValueError):
        resolve_backend("aws")


def test_specs_validated_per_backend():
    TaskConfig(workflow_config=WORKFLOW, backend="modal", accelerator_spec="H100")
    TaskConfig(workflow_config=WORKFLOW, backend="kaggle", accelerator_spec="P100")
    with pytest.raises(ValueError):
        TaskConfig(workflow_config=WORKFLOW, backend="kaggle", accelerator_spec="H100")
    with pytest.raises(ValueError):
        TaskConfig(workflow_config=WORKFLOW, backend="modal", accelerator_kind="tpu",
                   accelerator_spec="v5e1")
    cfg = TaskConfig.from_dict({"workflow_config": str(WORKFLOW), "backend": "kaggle",
                                "accelerator": {"kind": "gpu", "spec": "T4x2"}})
    assert cfg.backend == "kaggle"


def test_get_transport_by_backend(monkeypatch):
    monkeypatch.delenv("COMPUTE_BACKEND", raising=False)
    assert isinstance(get_transport("cli"), CliTransport)
    assert get_transport(None, backend="modal").backend == "modal"
    assert get_transport(None, backend="kaggle").backend == "kaggle"
    with pytest.raises(ValueError):
        get_transport(None, backend="nope")


def test_forward_remote_line_parses_progress():
    events = []
    sink = lambda *a: events.append(a)  # noqa: E731
    forward_remote_line('__COLAB_OUTPUT_V1__PROGRESS:{"text":"50%","value":5}\n', sink)
    forward_remote_line("plain", sink)
    forward_remote_line("__COLAB_OUTPUT_V1__READY:{}", sink)
    assert [e[0] for e in events] == ["workflow.exec", "log.line"]


def test_fetch_manifest_rejects_corruption(tmp_path):
    files = {}
    make_transfer(files)
    files["part-00001.bin"] = b"y" * len(files["part-00001.bin"])
    with pytest.raises(RuntimeError):
        fetch_manifest_output(lambda p: files[Path(p).name], tmp_path / "o.zip")
    assert not (tmp_path / "o.zip").exists()


# ── Modal（假 SDK）────────────────────────────────────────────
class FakeFS:
    def __init__(self):
        self.files = {}

    def make_directory(self, path, create_parents=True):
        pass

    def copy_from_local(self, local, remote):
        self.files[remote] = Path(local).read_bytes()

    def read_bytes(self, remote):
        return self.files[remote]


class FakeSandbox:
    registry = {}

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.object_id = f"sb-{len(self.registry)}"
        self.filesystem = FakeFS()
        self.alive = True
        self.commands = []
        self.registry[self.object_id] = self

    @classmethod
    def create(cls, *args, **kwargs):
        return cls(args=args, **kwargs)

    @classmethod
    def from_id(cls, sandbox_id):
        return cls.registry[sandbox_id]

    def poll(self):
        return None if self.alive else 0

    def terminate(self):
        self.alive = False

    def exec(self, *args, timeout=None, env=None):
        self.commands.append((args, env))
        if "remote_workflow.py" in args[-1]:
            for name, data in make_transfer({}).items():
                self.filesystem.files[f"/content/output_transfer/{name}"] = data
        lines = ['__COLAB_OUTPUT_V1__PROGRESS:{"text":"step 1/2"}\n', "done\n"]
        return SimpleNamespace(stdout=iter(lines), wait=lambda: 0)


class FakeImage:
    @staticmethod
    def debian_slim(**kwargs):
        return FakeImage()

    def apt_install(self, *a):
        return self

    def run_commands(self, *a):
        return self


fake_modal = SimpleNamespace(
    Sandbox=FakeSandbox, Image=FakeImage,
    App=SimpleNamespace(lookup=lambda name, create_if_missing: f"app:{name}"),
    Volume=SimpleNamespace(from_name=lambda name, create_if_missing: f"vol:{name}"),
    Secret=SimpleNamespace(from_name=lambda name: f"secret:{name}"),
)


def test_modal_full_flow(tmp_path, monkeypatch):
    monkeypatch.setenv("MODAL_SECRETS", "hf-token")
    events = []
    transport = ModalTransport(lambda *a: events.append(a), modal_module=fake_modal,
                               state_dir=tmp_path)
    transport.create_session("s1", "gpu", "A100")
    sandbox = next(reversed(FakeSandbox.registry.values()))
    assert sandbox.kwargs["gpu"] == "A100"
    assert sandbox.kwargs["volumes"] == {DRIVE_MODELS_DIR: "vol:colab-task-models"}
    assert sandbox.kwargs["secrets"] == ["secret:hf-token"]
    assert "Variant: GPU" in transport.session_status("s1")
    assert transport.mount_drive("s1") is True

    script = tmp_path / "remote_workflow.py"
    script.write_text("print('hi')")
    out = tmp_path / "out" / "output.zip"
    assert transport.exec_workflow("s1", script, 60, out) is True
    with zipfile.ZipFile(out) as archive:
        assert archive.namelist() == ["out/video.mp4"]
    assert sandbox.commands[-1][1]["REMOTE_OUTPUT_NO_WAIT"] == "1"
    assert any(e[0] == "workflow.exec" for e in events)
    assert transport.remote_output_ready("s1") is True

    transport.stop_session("s1")
    assert sandbox.alive is False
    with pytest.raises(Exception):
        transport.check_session_readable("s1")


# ── Kaggle（假 kaggle CLI）─────────────────────────────────────
class FakeKaggle:
    def __init__(self, final_status="complete", exit_code=0):
        self.calls = []
        self.final_status = final_status
        self.exit_code = exit_code
        self.polls = 0
        self.pushed = None

    def __call__(self, args, timeout=None):
        self.calls.append(args)
        out = ""
        if args[:2] == ["kernels", "push"]:
            folder = Path(args[args.index("-p") + 1])
            self.pushed = {
                "meta": json.loads((folder / "kernel-metadata.json").read_text()),
                "code": (folder / "run.py").read_text(),
            }
        elif args[:2] == ["kernels", "status"]:
            self.polls += 1
            status = "running" if self.polls < 2 else self.final_status
            out = f'me/x has status "KernelWorkerStatus.{status.upper()}"'
        elif args[:2] == ["kernels", "output"]:
            root = Path(args[args.index("-p") + 1])
            transfer = root / "output_transfer"
            transfer.mkdir(parents=True)
            for name, data in make_transfer({}).items():
                (transfer / name).write_bytes(data)
            (root / "backend_result.json").write_text(json.dumps({"exit_code": self.exit_code}))
            (root / "x.log").write_text(json.dumps([
                {"stream_name": "stdout", "data": '__COLAB_OUTPUT_V1__PROGRESS:{"text":"k"}\n'}]))
        return subprocess.CompletedProcess(args, 0, out, "")


def kaggle_transport(tmp_path, fake, events=None):
    return KaggleTransport((lambda *a: events.append(a)) if events is not None else None,
                           runner=fake, state_dir=tmp_path / "state", sleep=lambda s: None,
                           username="me")


def test_kaggle_full_flow(tmp_path, monkeypatch):
    monkeypatch.setenv("KAGGLE_MODELS_DATASET", "me/comfy-models")
    fake, events = FakeKaggle(), []
    transport = kaggle_transport(tmp_path, fake, events)
    transport.create_session("comfyui-h3", "gpu", "T4x2")
    assert transport.mount_drive("comfyui-h3") is True
    image = tmp_path / "in.png"
    image.write_bytes(b"png")
    transport.upload("comfyui-h3", image, "/content/workflow_input_000.png")
    setup = tmp_path / "remote_setup.py"
    setup.write_text("print('setup')")
    workflow = tmp_path / "remote_workflow.py"
    workflow.write_text("print('wf')")
    transport.exec_script("comfyui-h3", setup, 60)
    out = tmp_path / "out" / "output.zip"
    assert transport.exec_workflow("comfyui-h3", workflow, 600, out) is True
    assert zipfile.ZipFile(out).namelist() == ["out/video.mp4"]

    meta = fake.pushed["meta"]
    assert meta["id"] == "me/colab-comfyui-h3" and meta["enable_gpu"] is True
    assert meta["dataset_sources"] == ["me/comfy-models"]
    push = next(c for c in fake.calls if c[:2] == ["kernels", "push"])
    assert push[push.index("--accelerator") + 1] == "NvidiaTeslaT4"
    compile(fake.pushed["code"], "run.py", "exec")
    assert "/content/_backend/remote_setup.py" in fake.pushed["code"]
    assert fake.pushed["code"].index("remote_setup.py") < fake.pushed["code"].index("remote_workflow.py")
    assert any(e[0] == "workflow.exec" and e[3].get("text") == "k" for e in events)


def test_kaggle_large_inputs_go_to_dataset(tmp_path, monkeypatch):
    monkeypatch.setenv("KAGGLE_INLINE_MAX_BYTES", "10")
    fake = FakeKaggle()
    transport = kaggle_transport(tmp_path, fake)
    transport.create_session("s", "gpu", "P100")
    big = tmp_path / "big.bin"
    big.write_bytes(bytes(range(256)) * 100)
    transport.upload("s", big, "/content/big.bin")
    wf = tmp_path / "remote_workflow.py"
    wf.write_text("pass")
    transport.exec_workflow("s", wf, 600, tmp_path / "o.zip")
    assert ["datasets", "version"] == next(c for c in fake.calls if c[0] == "datasets")[:2]
    assert fake.pushed["meta"]["dataset_sources"] == ["me/colab-s-inputs"]
    assert "INLINE_BUNDLE = ''" in fake.pushed["code"]


def test_kaggle_failure_reports_kernel_url(tmp_path):
    transport = kaggle_transport(tmp_path, FakeKaggle(exit_code=1))
    transport.create_session("s", "gpu", "T4")
    wf = tmp_path / "remote_workflow.py"
    wf.write_text("pass")
    with pytest.raises(Exception, match="kaggle.com/code/me/colab-s"):
        transport.exec_workflow("s", wf, 600, tmp_path / "o.zip")


def test_kaggle_rejects_unsupported_gpu(tmp_path):
    with pytest.raises(Exception):
        kaggle_transport(tmp_path, FakeKaggle()).create_session("s", "gpu", "H100")


def test_kaggle_helpers():
    assert kernel_slug("ComfyUI_H3") == "colab-comfyui-h3"
    assert parse_status('has status "KernelWorkerStatus.COMPLETE"') == "complete"
    assert parse_status("weird") == "unknown"


def test_kernel_script_runs_locally(tmp_path, monkeypatch):
    """生成的 kernel 脚本至少能在本地语法/格式化层面通过（不依赖 /kaggle）。"""
    transport = kaggle_transport(tmp_path, FakeKaggle())
    transport.create_session("s", "gpu", "T4")
    code = (transport.build_kernel_dir("s") / "run.py").read_text()
    compile(code, "run.py", "exec")
