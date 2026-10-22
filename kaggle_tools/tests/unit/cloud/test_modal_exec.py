import io
import tarfile
from pathlib import Path
from types import SimpleNamespace

from kaggle_tools.cloud.modal_exec import pack_directory, run_in_modal


class FakeSandbox:
    def __init__(self):
        self.files, self.calls, self.terminated = {}, [], False
        self.object_id = "sb-1"
        self.filesystem = SimpleNamespace(
            make_directory=lambda path, create_parents=True: None,
            copy_from_local=lambda local, remote: self.files.__setitem__(remote, Path(local).read_bytes()),
            write_bytes=lambda data, remote: self.files.__setitem__(remote, data),
            read_bytes=lambda remote: self.files[remote],
        )

    def exec(self, *args, env=None, timeout=None):
        self.calls.append((args, env))
        if args[:2] == ("tar", "czf"):
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
                data = b"diff --git a/x b/x\n"
                info = tarfile.TarInfo("./changes.diff")
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
            self.files["/work/out.tgz"] = buffer.getvalue()
        return SimpleNamespace(stdout=iter(["round 1\n"]), wait=lambda: 0)

    def terminate(self):
        self.terminated = True


def test_run_in_modal_roundtrip(tmp_path):
    sandbox = FakeSandbox()
    image = SimpleNamespace(apt_install=lambda *a: image, pip_install=lambda *a: image)
    fake = SimpleNamespace(
        App=SimpleNamespace(lookup=lambda name, create_if_missing: name),
        Image=SimpleNamespace(debian_slim=lambda **k: image),
        Sandbox=SimpleNamespace(create=lambda *a, **k: sandbox),
    )
    task = tmp_path / "task.py"
    task.write_text("print(1)")
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "a.py").write_text("x=1")
    out = tmp_path / "run"
    lines = []
    code = run_in_modal(task, {"MODEL_PROXY_URL": "https://k"}, out, repo=repo,
                        modal_module=fake, echo=lines.append)
    assert code == 0
    assert (out / "changes.diff").read_text().startswith("diff --git")
    run_env = next(env for args, env in sandbox.calls if args[0] == "bash")
    assert run_env["CODEGEN_REPO_PATH"] == "/work/repo"
    assert "round 1" in lines and sandbox.terminated


def test_pack_directory_skips_git(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref")
    (tmp_path / "keep.py").write_text("1")
    names = tarfile.open(fileobj=io.BytesIO(pack_directory(tmp_path))).getnames()
    assert "repo/keep.py" in names and not any(".git" in n for n in names)
