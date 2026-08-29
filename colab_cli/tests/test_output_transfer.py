"""Regression tests for Colab workflow output transfer."""

import base64
import hashlib
import json
import zipfile
from pathlib import Path

import pytest

import colab_transport.constants as transport_constants
import remote_workflow
import run as runner


class FakeContentsClient:
    """Minimal in-memory implementation of the Colab Contents client."""

    def __init__(self, remote_files, fail_download=False):
        self.remote_files = remote_files
        self.fail_download = fail_download
        self.uploads = []

    def download(self, remote_path, local_path):
        """Copy one in-memory remote object to disk."""
        if self.fail_download:
            raise FileNotFoundError(remote_path)
        Path(local_path).write_bytes(self.remote_files[remote_path])

    def upload(self, local_path, remote_path):
        """Record a handshake upload."""
        assert Path(local_path).is_file()
        self.uploads.append(remote_path)


@pytest.fixture
def archive_transfer(tmp_path):
    """Create a valid ZIP, manifest, and remote chunks."""
    archive_path = tmp_path / "source.zip"
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("video/result.mp4", b"video-bytes" * 10_000)

    archive_bytes = archive_path.read_bytes()
    chunks = [
        archive_bytes[offset:offset + 4096]
        for offset in range(0, len(archive_bytes), 4096)
    ]
    archive_sha256 = hashlib.sha256(archive_bytes).hexdigest()
    manifest = {
        "version": 1,
        "archive_name": "output.zip",
        "archive_size": len(archive_bytes),
        "archive_sha256": archive_sha256,
        "chunk_size": 4096,
        "parts": [
            {
                "name": f"part-{index:05d}.bin",
                "size": len(chunk),
                "sha256": hashlib.sha256(chunk).hexdigest(),
            }
            for index, chunk in enumerate(chunks)
        ],
    }
    remote_files = {
        f"{runner.REMOTE_TRANSFER_DIR}/manifest.json": json.dumps(
            manifest
        ).encode(),
        **{
            f"{runner.REMOTE_TRANSFER_DIR}/part-{index:05d}.bin": chunk
            for index, chunk in enumerate(chunks)
        },
    }
    ready = {
        "version": 1,
        "size": len(archive_bytes),
        "sha256": archive_sha256,
        "parts": len(chunks),
    }
    return archive_bytes, remote_files, ready


def feed_stdout_fallback(receiver, archive_bytes, fragment_size=113):
    """Feed a complete stdout fallback at arbitrary message boundaries."""
    metadata = json.dumps({
        "size": len(archive_bytes),
        "sha256": hashlib.sha256(archive_bytes).hexdigest(),
    })
    chunks = [
        archive_bytes[offset:offset + 777]
        for offset in range(0, len(archive_bytes), 777)
    ]
    protocol_lines = [
        f"{runner.OUTPUT_STREAM_PREFIX}BEGIN:{metadata}",
        *[
            f"{runner.OUTPUT_STREAM_PREFIX}CHUNK:{index}:"
            f"{base64.b64encode(chunk).decode()}"
            for index, chunk in enumerate(chunks)
        ],
        f"{runner.OUTPUT_STREAM_PREFIX}END:{len(chunks)}",
    ]
    protocol = "\n".join(protocol_lines) + "\n"
    for offset in range(0, len(protocol), fragment_size):
        receiver.feed_stdout(protocol[offset:offset + fragment_size])


def test_live_contents_download_and_ack(tmp_path, archive_transfer):
    """READY downloads all chunks before acknowledging remote completion."""
    archive_bytes, remote_files, ready = archive_transfer
    target = tmp_path / "received.zip"
    contents_client = FakeContentsClient(remote_files)
    receiver = runner.OutputStreamReceiver(target, contents_client)

    receiver.feed_stdout(
        f"{runner.OUTPUT_STREAM_PREFIX}READY:{json.dumps(ready)}\n"
    )

    assert receiver.finalize()
    assert target.read_bytes() == archive_bytes
    assert runner.REMOTE_TRANSFER_ACK in contents_client.uploads


def test_stdout_fallback_after_contents_failure(
    tmp_path,
    archive_transfer,
    monkeypatch,
):
    """A Contents API failure requests and accepts the stdout fallback."""
    archive_bytes, _, ready = archive_transfer
    target = tmp_path / "fallback.zip"
    contents_client = FakeContentsClient({}, fail_download=True)
    receiver = runner.OutputStreamReceiver(target, contents_client)
    monkeypatch.setattr(transport_constants, "DOWNLOAD_RETRY_DELAY_SECONDS", 0)

    receiver.feed_stdout(
        f"{runner.OUTPUT_STREAM_PREFIX}READY:{json.dumps(ready)}\n"
    )
    feed_stdout_fallback(receiver, archive_bytes)

    assert receiver.finalize()
    assert target.read_bytes() == archive_bytes
    assert runner.REMOTE_TRANSFER_STREAM_REQUEST in contents_client.uploads


def test_duplicate_stdout_fallback_is_ignored(tmp_path, archive_transfer):
    """A delayed fallback cannot overwrite an already verified local archive."""
    archive_bytes, remote_files, ready = archive_transfer
    target = tmp_path / "received.zip"
    receiver = runner.OutputStreamReceiver(
        target,
        FakeContentsClient(remote_files),
    )
    receiver.feed_stdout(
        f"{runner.OUTPUT_STREAM_PREFIX}READY:{json.dumps(ready)}\n"
    )

    feed_stdout_fallback(receiver, archive_bytes, fragment_size=91)

    assert receiver.finalize()
    assert target.read_bytes() == archive_bytes


def test_remote_ack_skips_stdout_fallback(tmp_path, monkeypatch):
    """The remote task exits without Base64 streaming after local ACK."""
    ack_path = tmp_path / "download.ack"
    ack_path.write_text("ok", encoding="utf-8")
    fallback_calls = []
    monkeypatch.setattr(remote_workflow, "OUTPUT_TRANSFER_ACK", str(ack_path))
    monkeypatch.setattr(
        remote_workflow,
        "OUTPUT_TRANSFER_ACTIVE",
        str(tmp_path / "download.active"),
    )
    monkeypatch.setattr(
        remote_workflow,
        "OUTPUT_TRANSFER_STREAM_REQUEST",
        str(tmp_path / "stream.request"),
    )
    monkeypatch.setattr(
        remote_workflow,
        "stream_output_archive",
        lambda *args: fallback_calls.append(args),
    )

    remote_workflow.wait_for_output_transfer_client(
        "/tmp/output.zip",
        10,
        "a" * 64,
        1,
    )

    assert fallback_calls == []
