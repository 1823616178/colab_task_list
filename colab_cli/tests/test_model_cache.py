"""Exercise actual model files, atomic publication, fallback and local VM locks."""

import errno
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

import remote_workflow as remote

WEIGHTS = b"complete model weights"


@pytest.fixture
def cache_env(tmp_path, monkeypatch):
    """Use real temporary directories for Drive, VM models and HF partial files."""
    comfy = tmp_path / "ComfyUI"
    drive = tmp_path / "drive" / "models"
    drive.mkdir(parents=True)
    monkeypatch.setattr(remote, "COMFY", str(comfy))
    monkeypatch.setattr(remote, "DRIVE_MODELS_DIR", str(drive))
    monkeypatch.setattr(remote, "MODEL_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.setattr(remote, "MODEL_LOCAL_RESERVE_BYTES", 0)
    monkeypatch.setattr(remote, "drive_is_mounted", lambda: True)
    model = {
        "repo": "example/models",
        "filename": "upstream/nested/weights.safetensors",
        "comfy_dir": "latent_upscale_models",
        "cache_path": "latent_upscale_models/weights.safetensors",
        "size_bytes": len(WEIGHTS),
    }
    calls = []

    def download(repo, filename, directory, *, metadata_only=False):
        calls.append((repo, filename, Path(directory), metadata_only))
        if metadata_only:
            return {"size": len(WEIGHTS)}
        path = Path(directory) / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(WEIGHTS)
        return {"path": str(path)}

    monkeypatch.setattr(remote, "run_model_download_worker", download)
    return SimpleNamespace(
        config={"models": [model]}, model=model, drive=drive, comfy=comfy,
        target=comfy / "models" / model["comfy_dir"] / Path(model["filename"]).name,
        cached=drive / model["cache_path"], calls=calls, download=download,
    )


def test_missing_model_downloads_directly_to_drive_then_stages_local_file(cache_env):
    """Custom flattened cache paths must preserve Drive persistence and local loading."""
    env = cache_env
    remote.prepare_models(env.config, True)

    assert len(env.calls) == 1
    assert env.calls[0][2].is_relative_to(env.drive / ".downloads")
    assert env.cached.read_bytes() == WEIGHTS
    assert env.target.read_bytes() == WEIGHTS
    assert not env.target.is_symlink()
    assert not list(env.drive.rglob("*.part"))
    assert not (env.comfy / ".colab_model_cache").exists()

    remote.prepare_models(env.config, True)
    assert len(env.calls) == 1


@pytest.mark.parametrize("old_link", [False, True])
def test_drive_cache_hit_stages_locally_without_network(cache_env, old_link):
    """Old links are upgraded to regular local files, with no download."""
    env = cache_env
    env.cached.parent.mkdir(parents=True)
    env.cached.write_bytes(WEIGHTS)
    if old_link:
        env.target.parent.mkdir(parents=True)
        env.target.symlink_to(env.cached)

    remote.prepare_models(env.config, True)

    assert env.calls == []
    assert env.target.read_bytes() == WEIGHTS
    assert not env.target.is_symlink()


def test_vm_without_staging_space_keeps_drive_link(cache_env, monkeypatch):
    """Drive remains usable when a second model copy cannot fit locally."""
    env = cache_env
    monkeypatch.setattr(remote.shutil, "disk_usage", lambda _: SimpleNamespace(free=0))

    remote.prepare_models(env.config, True)

    assert env.target.is_symlink()
    assert env.target.resolve() == env.cached
    assert env.target.read_bytes() == WEIGHTS
    assert len(env.calls) == 1


@pytest.mark.parametrize("mount_drive", [False, True])
def test_disabled_or_missing_mount_downloads_only_locally(cache_env, monkeypatch, mount_drive):
    """A directory that merely looks like Drive is never used as persistent storage."""
    env = cache_env
    monkeypatch.setattr(remote, "drive_is_mounted", lambda: False)

    remote.prepare_models(env.config, mount_drive)

    assert not env.cached.exists()
    assert env.calls[0][2].is_relative_to(env.comfy)
    assert env.target.read_bytes() == WEIGHTS
    assert not env.target.is_symlink()


def test_readonly_drive_still_reuses_existing_model(cache_env, monkeypatch):
    """Write permissions are not required to read a complete Drive cache."""
    env = cache_env
    env.cached.parent.mkdir(parents=True)
    env.cached.write_bytes(WEIGHTS)
    monkeypatch.setattr(remote, "model_directory_writable", lambda _: False)

    remote.prepare_models(env.config, True)

    assert env.calls == []
    assert env.target.read_bytes() == WEIGHTS


def test_unwritable_drive_falls_back_to_local_download(cache_env, monkeypatch):
    """Missing models can still run when Drive cannot accept writes."""
    env = cache_env
    monkeypatch.setattr(remote, "model_directory_writable", lambda _: False)

    remote.prepare_models(env.config, True)

    assert env.calls[0][2].is_relative_to(env.comfy)
    assert env.target.read_bytes() == WEIGHTS
    assert not env.cached.exists()


@pytest.mark.parametrize("error_number", [errno.ENOSPC, errno.EDQUOT, errno.EROFS, errno.EIO])
def test_drive_storage_failure_uses_local_fallback(cache_env, monkeypatch, error_number):
    """Filesystem failures are distinct from Hugging Face network or permission errors."""
    env = cache_env
    attempted = []

    def download(repo, filename, directory, **kwargs):
        attempted.append(Path(directory))
        if Path(directory).is_relative_to(env.drive):
            (Path(directory) / "unfinished.incomplete").write_bytes(b"partial")
            raise OSError(error_number, "Drive storage failure")
        return env.download(repo, filename, directory, **kwargs)

    monkeypatch.setattr(remote, "run_model_download_worker", download)
    remote.prepare_models(env.config, True)

    assert len(attempted) == 2
    assert attempted[0].is_relative_to(env.drive)
    assert attempted[1].is_relative_to(env.comfy)
    assert not env.cached.exists()
    assert env.target.read_bytes() == WEIGHTS


@pytest.mark.parametrize("error", [RuntimeError("HTTP 403: gated repository"), TimeoutError("network timeout")])
def test_download_source_errors_do_not_trigger_another_download(cache_env, monkeypatch, error):
    """Network/authentication failures must not be mistaken for unavailable Drive."""
    env = cache_env
    attempted = []

    def fail(repo, filename, directory, **kwargs):
        attempted.append(directory)
        raise error

    monkeypatch.setattr(remote, "run_model_download_worker", fail)
    with pytest.raises(type(error)):
        remote.prepare_models(env.config, True)
    assert len(attempted) == 1
    assert not env.cached.exists()
    assert not env.target.exists()


def test_bad_download_is_never_published_and_retries_reuse_directory(cache_env, monkeypatch):
    """A failed size check leaves the final cache untouched and retains resume data."""
    env = cache_env
    attempted = []

    def download(repo, filename, directory, **kwargs):
        attempted.append(directory)
        result = env.download(repo, filename, directory, **kwargs)
        if len(attempted) == 1:
            Path(result["path"]).write_bytes(b"partial")
        return result

    monkeypatch.setattr(remote, "run_model_download_worker", download)
    with pytest.raises(ValueError, match="大小校验失败"):
        remote.prepare_models(env.config, True)
    assert not env.cached.exists()
    assert not env.target.exists()

    remote.prepare_models(env.config, True)
    assert attempted[0] == attempted[1]
    assert env.cached.read_bytes() == WEIGHTS
    assert env.target.read_bytes() == WEIGHTS


def test_corrupt_drive_file_is_replaced_by_complete_download(cache_env):
    """A nonempty truncated file must not count as a hit when a size is declared."""
    env = cache_env
    env.cached.parent.mkdir(parents=True)
    env.cached.write_bytes(b"partial")

    remote.prepare_models(env.config, True)

    assert len(env.calls) == 1
    assert env.cached.read_bytes() == WEIGHTS
    assert env.target.read_bytes() == WEIGHTS


def test_local_space_is_checked_before_fallback_download(cache_env, monkeypatch):
    """VM fallback cannot silently fill a disk that lacks the declared model size."""
    env = cache_env
    monkeypatch.setattr(remote, "model_directory_writable", lambda _: False)
    monkeypatch.setattr(remote.shutil, "disk_usage", lambda _: SimpleNamespace(free=0))
    with pytest.raises(OSError, match="VM 空间不足"):
        remote.prepare_models(env.config, True)
    assert env.calls == []


def test_missing_size_uses_remote_metadata_for_vm_space_check(cache_env):
    """Old manifests without size_bytes still check disk space before downloading."""
    env = cache_env
    env.model.pop("size_bytes")

    remote.prepare_models(env.config, False)

    assert [call[3] for call in env.calls] == [True, False]
    assert env.target.read_bytes() == WEIGHTS


def test_missing_manual_model_fails_before_any_large_download(cache_env):
    """The existing all-manual-model preflight remains ahead of public downloads."""
    env = cache_env
    env.config["models"].append({"filename": "manual.safetensors", "comfy_dir": "loras"})

    with pytest.raises(FileNotFoundError, match="没有配置公开下载源"):
        remote.prepare_models(env.config, True)
    assert env.calls == []


def test_manual_drive_cache_works_without_huggingface(cache_env):
    """User-supplied LoRAs can be staged without any downloadable repository."""
    env = cache_env
    env.model.pop("repo")
    env.cached.parent.mkdir(parents=True)
    env.cached.write_bytes(WEIGHTS)

    remote.prepare_models(env.config, True)

    assert env.calls == []
    assert env.target.read_bytes() == WEIGHTS


@pytest.mark.parametrize("field", ["filename", "cache_path", "comfy_dir"])
def test_model_paths_cannot_escape_storage_root(cache_env, field):
    """Reject unsafe manifests before creating download files."""
    env = cache_env
    env.model[field] = "../outside"
    with pytest.raises(ValueError, match="安全的相对路径"):
        remote.prepare_models(env.config, True)
    assert env.calls == []


def test_same_vm_concurrent_preparation_downloads_once(cache_env, monkeypatch):
    """Actual local flock serializes two callers preparing one ComfyUI model."""
    env = cache_env
    started = threading.Event()

    def download(*args, **kwargs):
        started.set()
        time.sleep(0.1)
        return env.download(*args, **kwargs)

    monkeypatch.setattr(remote, "run_model_download_worker", download)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(remote.prepare_models, env.config, True)
        assert started.wait(2)
        second = pool.submit(remote.prepare_models, env.config, True)
        first.result(timeout=5)
        second.result(timeout=5)
    assert len(env.calls) == 1
    assert env.target.read_bytes() == WEIGHTS


@pytest.mark.parametrize("backend", ["modal", "kaggle"])
def test_other_backends_keep_the_existing_strategy(cache_env, monkeypatch, backend):
    """Colab's storage selection must not change Modal Volume or Kaggle Dataset handling."""
    calls = []
    monkeypatch.setattr(
        remote, "prepare_models_legacy",
        lambda config, mount, mode: calls.append((config, mount, mode)),
    )
    remote.prepare_models(cache_env.config, True, backend)
    assert calls == [(cache_env.config, True, "auto")]
    assert cache_env.calls == []


def test_download_mode_ignores_drive_cache(cache_env):
    """download 模式只从 Hugging Face 下载到 VM，不读取已有 Drive 缓存。"""
    env = cache_env
    env.cached.parent.mkdir(parents=True)
    env.cached.write_bytes(WEIGHTS)

    remote.prepare_models(env.config, True, "colab", "download")

    assert env.calls[0][2].is_relative_to(env.comfy)
    assert env.cached.read_bytes() == WEIGHTS
    assert env.target.read_bytes() == WEIGHTS
    assert not env.target.is_symlink()


def test_symlink_mode_links_drive_cache_without_copy(cache_env):
    """symlink 模式直接链接 Drive 缓存，不产生本地副本。"""
    env = cache_env
    env.cached.parent.mkdir(parents=True)
    env.cached.write_bytes(WEIGHTS)

    remote.prepare_models(env.config, True, "colab", "symlink")

    assert env.calls == []
    assert env.target.is_symlink()
    assert env.target.resolve() == env.cached


def test_symlink_mode_downloads_missing_model_to_drive_then_links(cache_env):
    """symlink 模式仍会把缺失模型持久化到 Drive，再建立链接。"""
    env = cache_env

    remote.prepare_models(env.config, True, "colab", "symlink")

    assert env.calls[0][2].is_relative_to(env.drive)
    assert env.cached.read_bytes() == WEIGHTS
    assert env.target.is_symlink()
    assert env.target.resolve() == env.cached


def test_copy_mode_stages_local_copy(cache_env):
    """copy 模式保留原有的 Drive 优先 + VM 本地复制行为。"""
    env = cache_env
    env.cached.parent.mkdir(parents=True)
    env.cached.write_bytes(WEIGHTS)

    remote.prepare_models(env.config, True, "colab", "copy")

    assert env.calls == []
    assert env.target.read_bytes() == WEIGHTS
    assert not env.target.is_symlink()


def test_download_mode_links_manual_model_from_drive(cache_env):
    """download 模式无法下载手动模型时，仍从 Drive 软链保证任务可运行。"""
    env = cache_env
    env.model.pop("repo")
    env.cached.parent.mkdir(parents=True)
    env.cached.write_bytes(WEIGHTS)

    remote.prepare_models(env.config, True, "colab", "download")

    assert env.calls == []
    assert env.target.is_symlink()
    assert env.target.resolve() == env.cached


def test_unknown_model_mode_is_rejected(cache_env):
    """非法模式必须在任何下载或磁盘写入前失败。"""
    with pytest.raises(ValueError, match="未知模型获取模式"):
        remote.prepare_models(cache_env.config, True, "colab", "bogus")
    assert cache_env.calls == []


def test_legacy_symlink_mode_links_drive_cache(cache_env):
    """非 Colab 后端也支持软链接模式。"""
    env = cache_env
    env.cached.parent.mkdir(parents=True)
    env.cached.write_bytes(WEIGHTS)

    remote.prepare_models_legacy(env.config, True, "symlink")

    assert env.calls == []
    assert env.target.is_symlink()
    assert env.target.resolve() == env.cached


def test_legacy_copy_mode_replaces_existing_drive_link(cache_env):
    """非 Colab 后端的 copy 模式会把旧 Drive 链接升级为本地副本。"""
    env = cache_env
    env.cached.parent.mkdir(parents=True)
    env.cached.write_bytes(WEIGHTS)
    env.target.parent.mkdir(parents=True)
    env.target.symlink_to(env.cached)

    remote.prepare_models_legacy(env.config, True, "copy")

    assert env.calls == []
    assert env.target.read_bytes() == WEIGHTS
    assert not env.target.is_symlink()


def test_storage_read_failure_fallback_replaces_old_drive_symlink(cache_env, monkeypatch):
    """Fallback files must replace old Drive links rather than continue reading them."""
    env = cache_env
    env.cached.parent.mkdir(parents=True)
    env.cached.write_bytes(WEIGHTS)
    env.target.parent.mkdir(parents=True)
    env.target.symlink_to(env.cached)
    monkeypatch.setattr(remote, "install_drive_model", lambda *_: (_ for _ in ()).throw(OSError(errno.EIO, "Drive disconnected")))

    remote.prepare_models(env.config, True)

    assert not env.target.is_symlink()
    assert env.target.read_bytes() == WEIGHTS
    assert len(env.calls) == 1


@pytest.fixture
def fake_hub(tmp_path, monkeypatch):
    """Run the real download subprocess against an offline stand-in SDK."""
    package = tmp_path / "sdk" / "huggingface_hub"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text('''
import json, os, ssl
from pathlib import Path
from types import SimpleNamespace
def hf_hub_url(repo, filename): return repo + "/" + filename
def get_hf_file_metadata(url): return SimpleNamespace(size=22)
def hf_hub_download(repo_id, filename, local_dir):
    if repo_id == "error/storage": raise OSError(28, "No space left on device")
    if repo_id == "error/network": raise RuntimeError("HTTP 403 denied")
    if repo_id == "error/tls": raise ssl.SSLError(1, "TLS handshake failed")
    path = Path(local_dir) / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"complete model weights")
    (Path(local_dir) / "cache-settings.json").write_text(json.dumps({
        name: os.environ.get(name) for name in ["HF_HUB_CACHE", "HF_XET_CACHE", "HF_HUB_ENABLE_HF_TRANSFER"]
    }))
    return str(path)
''', encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(package.parent))
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "wrong-vm-cache"))
    monkeypatch.setenv("HF_XET_CACHE", str(tmp_path / "wrong-vm-xet"))
    monkeypatch.setenv("HF_HUB_ENABLE_HF_TRANSFER", "1")
    return tmp_path / "drive-download"


def test_actual_worker_overrides_hf_and_xet_caches_before_import(fake_hub):
    """No notebook's previously imported SDK settings may redirect a Drive download."""
    result = remote.run_model_download_worker("example/models", "nested/weights.safetensors", str(fake_hub))
    assert Path(result["path"]).read_bytes() == WEIGHTS
    settings = json.loads((fake_hub / "cache-settings.json").read_text())
    assert Path(settings["HF_HUB_CACHE"]).is_relative_to(fake_hub)
    assert Path(settings["HF_XET_CACHE"]).is_relative_to(fake_hub)
    assert settings["HF_HUB_ENABLE_HF_TRANSFER"] is None


def test_actual_worker_reports_metadata_and_classifies_errors(fake_hub):
    """Only explicit storage failures receive a filesystem errno for fallback."""
    result = remote.run_model_download_worker("example/models", "weights", str(fake_hub), metadata_only=True)
    assert result["size"] == len(WEIGHTS)
    with pytest.raises(OSError) as caught:
        remote.run_model_download_worker("error/storage", "weights", str(fake_hub))
    assert caught.value.errno == errno.ENOSPC
    with pytest.raises(RuntimeError, match="HTTP 403"):
        remote.run_model_download_worker("error/network", "weights", str(fake_hub))
    with pytest.raises(RuntimeError, match="TLS handshake failed"):
        remote.run_model_download_worker("error/tls", "weights", str(fake_hub))


def test_write_probe_does_not_leave_files(cache_env, monkeypatch):
    """Permission errors are handled with an actual write probe, not os.access guesses."""
    env = cache_env
    assert remote.model_directory_writable(str(env.drive))
    assert list(env.drive.iterdir()) == []
    monkeypatch.setattr(remote.os, "makedirs", lambda *args, **kwargs: (_ for _ in ()).throw(PermissionError(errno.EACCES, "denied")))
    assert not remote.model_directory_writable(str(env.drive))


def test_different_vms_do_not_share_incomplete_download_files(cache_env, monkeypatch):
    """A second VM can finish while an interrupted first VM retains its own partials."""
    env = cache_env
    directories = []

    def download(repo, filename, directory, **kwargs):
        directories.append(Path(directory))
        if len(directories) == 1:
            (Path(directory) / "partial.incomplete").write_bytes(b"partial")
            raise RuntimeError("network interrupted")
        return env.download(repo, filename, directory, **kwargs)

    monkeypatch.setattr(remote, "run_model_download_worker", download)
    monkeypatch.setattr(remote.socket, "gethostname", lambda: "vm-one")
    with pytest.raises(RuntimeError, match="network interrupted"):
        remote.prepare_models(env.config, True)
    monkeypatch.setattr(remote.socket, "gethostname", lambda: "vm-two")

    remote.prepare_models(env.config, True)

    assert directories[0] != directories[1]
    assert (directories[0] / "partial.incomplete").read_bytes() == b"partial"
    assert env.cached.read_bytes() == WEIGHTS
    assert env.target.read_bytes() == WEIGHTS


def test_staging_disk_full_after_space_check_keeps_drive_usable(cache_env, monkeypatch):
    """Concurrent disk usage may invalidate a preflight space check; links remain a fallback."""
    env = cache_env
    env.cached.parent.mkdir(parents=True)
    env.cached.write_bytes(WEIGHTS)

    def disk_full(*args):
        raise OSError(errno.ENOSPC, "local disk became full")

    monkeypatch.setattr(remote, "copy_with_progress", disk_full)
    remote.prepare_models(env.config, True)

    assert env.target.is_symlink()
    assert env.target.read_bytes() == WEIGHTS
    assert env.calls == []


def test_local_space_reserves_room_for_inference_and_outputs(cache_env, monkeypatch):
    """Model bytes alone are insufficient: the reserved space must also remain free."""
    monkeypatch.setattr(remote, "MODEL_LOCAL_RESERVE_BYTES", 2 * 1024**3)
    required = len(WEIGHTS) + remote.MODEL_LOCAL_RESERVE_BYTES
    monkeypatch.setattr(remote.shutil, "disk_usage", lambda _: SimpleNamespace(free=required - 1))
    assert not remote.local_model_has_space(str(cache_env.comfy), len(WEIGHTS))
    monkeypatch.setattr(remote.shutil, "disk_usage", lambda _: SimpleNamespace(free=required))
    assert remote.local_model_has_space(str(cache_env.comfy), len(WEIGHTS))


@pytest.mark.parametrize("backend", ["colab", "modal", "kaggle"])
def test_symlink_mode_replaces_existing_local_copy(cache_env, backend):
    env = cache_env
    env.cached.parent.mkdir(parents=True)
    env.cached.write_bytes(WEIGHTS)
    env.target.parent.mkdir(parents=True)
    env.target.write_bytes(WEIGHTS)

    remote.prepare_models(env.config, True, backend, "symlink")

    assert env.target.is_symlink()
    assert env.target.resolve() == env.cached
    assert env.calls == []


@pytest.mark.parametrize("backend", ["modal", "kaggle"])
@pytest.mark.parametrize("mode", ["auto", "symlink", "copy"])
def test_backend_cache_does_not_require_colab_drive_mount(cache_env, monkeypatch, backend, mode):
    env = cache_env
    env.cached.parent.mkdir(parents=True)
    env.cached.write_bytes(WEIGHTS)
    def no_colab_mount_check():
        raise AssertionError("Volume/Dataset 缓存不应检查 Colab Drive 挂载点")
    monkeypatch.setattr(remote, "drive_is_mounted", no_colab_mount_check)

    remote.prepare_models(env.config, True, backend, mode)

    assert env.calls == []
    assert env.target.read_bytes() == WEIGHTS
    assert env.target.is_symlink() == (mode != "copy")


@pytest.mark.parametrize("backend", ["colab", "modal", "kaggle"])
@pytest.mark.parametrize("failure", ["preflight", "copy_full"])
def test_copy_mode_falls_back_to_cache_link_on_full_vm(cache_env, monkeypatch, backend, failure):
    env = cache_env
    env.cached.parent.mkdir(parents=True)
    env.cached.write_bytes(WEIGHTS)
    if failure == "preflight":
        monkeypatch.setattr(remote.shutil, "disk_usage", lambda _: SimpleNamespace(free=0))
    else:
        def full(*args):
            raise OSError(errno.ENOSPC, "disk full")
        monkeypatch.setattr(remote, "copy_with_progress", full)

    remote.prepare_models(env.config, True, backend, "copy")

    assert env.calls == []
    assert env.target.is_symlink()
    assert env.target.resolve() == env.cached


@pytest.mark.parametrize("mode", ["copy", "download"])
def test_kaggle_dataset_link_switches_to_local_model(cache_env, monkeypatch, tmp_path, mode):
    env = cache_env
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    (dataset / "weights.safetensors").write_bytes(WEIGHTS)
    env.cached.parent.parent.mkdir(parents=True, exist_ok=True)
    env.cached.parent.symlink_to(dataset, target_is_directory=True)
    env.target.parent.mkdir(parents=True)
    env.target.symlink_to(env.cached)
    assert env.target.resolve().is_relative_to(dataset)

    remote.prepare_models(env.config, True, "kaggle", mode)

    assert env.target.read_bytes() == WEIGHTS
    assert not env.target.is_symlink()
    assert len(env.calls) == (1 if mode == "download" else 0)
    assert (dataset / "weights.safetensors").read_bytes() == WEIGHTS


def test_symlink_mode_archives_existing_local_copy_without_download(cache_env):
    env = cache_env
    env.target.parent.mkdir(parents=True)
    env.target.write_bytes(WEIGHTS)

    remote.prepare_models(env.config, True, "colab", "symlink")

    assert env.calls == []
    assert env.cached.read_bytes() == WEIGHTS
    assert env.target.is_symlink()
    assert env.target.resolve() == env.cached


def test_failed_mode_switch_preserves_working_local_model(cache_env, monkeypatch):
    env = cache_env
    env.cached.parent.mkdir(parents=True)
    env.cached.write_bytes(WEIGHTS)
    env.target.parent.mkdir(parents=True)
    env.target.write_bytes(WEIGHTS)
    def cannot_link(*args):
        raise OSError(errno.EPERM, "cannot symlink")
    def cannot_copy(*args):
        raise OSError(errno.ENOSPC, "cannot copy")
    monkeypatch.setattr(remote.os, "symlink", cannot_link)
    monkeypatch.setattr(remote, "copy_with_progress", cannot_copy)

    remote.prepare_models(env.config, True, "colab", "symlink")

    assert env.target.read_bytes() == WEIGHTS
    assert not env.target.is_symlink()
    assert not list(env.target.parent.glob("*.link"))


@pytest.mark.parametrize("backend", ["modal", "kaggle"])
def test_readonly_backend_cache_downloads_missing_model_locally(cache_env, monkeypatch, backend):
    env = cache_env
    monkeypatch.setattr(remote, "model_directory_writable", lambda _: False)

    remote.prepare_models(env.config, True, backend, "auto")

    assert len(env.calls) == 1
    assert env.calls[0][2].is_relative_to(env.comfy)
    assert not env.cached.exists()
    assert env.target.read_bytes() == WEIGHTS


def test_colab_does_not_treat_plain_cache_directory_as_mounted_drive(cache_env, monkeypatch):
    env = cache_env
    env.cached.parent.mkdir(parents=True)
    env.cached.write_bytes(WEIGHTS)
    monkeypatch.setattr(remote, "drive_is_mounted", lambda: False)

    remote.prepare_models(env.config, True, "colab", "symlink")

    assert len(env.calls) == 1
    assert env.calls[0][2].is_relative_to(env.comfy)
    assert env.target.read_bytes() == WEIGHTS
    assert not env.target.is_symlink()
