"""把云端/本地运行产出的 changes.diff 安全地应用到本地仓库的新分支。"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..common.errors import KaggleToolsError

GIT_TIMEOUT_SECONDS = 120
EMPTY_DIFF_MARKERS = ("", "(无改动)")


class ApplyError(KaggleToolsError):
    """应用 diff 失败, 仓库保持原样。"""


@dataclass(frozen=True)
class DiffSummary:
    """diff 概要。"""

    files: tuple[str, ...]
    added: int
    removed: int

    def describe(self) -> str:
        lines = [f"{len(self.files)} 个文件, +{self.added} / -{self.removed} 行"]
        lines += [f"  {name}" for name in self.files]
        return "\n".join(lines)


def is_empty_diff(text: str) -> bool:
    return text.strip() in EMPTY_DIFF_MARKERS


def summarize_diff(text: str) -> DiffSummary:
    """统计 unified diff 涉及的文件与增删行数。"""
    files: list[str] = []
    added = removed = 0
    previous_old = ""
    for line in text.splitlines():
        if line.startswith("--- "):
            previous_old = line[4:].strip()
        elif line.startswith("+++ "):
            new = line[4:].strip()
            path = new if new != "/dev/null" else previous_old
            name = path[2:] if path[:2] in ("a/", "b/") else path
            if new == "/dev/null":
                name += " (删除)"
            elif previous_old == "/dev/null":
                name += " (新增)"
            files.append(name)
        elif line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return DiffSummary(tuple(files), added, removed)


def find_diff(run_dir: Path) -> Path | None:
    """在一次运行的产物目录里找 changes.diff(下载包可能多一层目录)。"""
    direct = run_dir / "changes.diff"
    if direct.is_file():
        return direct
    found = sorted(run_dir.rglob("changes.diff"))
    return found[0] if found else None


def git(repo: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess:
    """在 repo 里执行 git, 带超时; check 时失败抛 ApplyError。"""
    try:
        completed = subprocess.run(
            ["git", *arguments], cwd=repo, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=GIT_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as error:
        raise ApplyError("找不到 git 命令, 请先安装 Git 并加入 PATH") from error
    except subprocess.TimeoutExpired as error:
        raise ApplyError(f"git {' '.join(arguments)} 超时") from error
    if check and completed.returncode != 0:
        raise ApplyError(
            f"git {' '.join(arguments)} 失败: {(completed.stderr or completed.stdout).strip()}"
        )
    return completed


def apply_diff(repo: Path, diff_path: Path, branch: str | None = None,
               commit: bool = True, allow_dirty: bool = False) -> tuple[str, DiffSummary]:
    """在 repo 新建分支并应用 diff, 返回(分支名, 概要)。

    顺序: 校验仓库 -> git apply --check(失败时什么都不改) -> 新建分支 -> git apply -> 可选提交。
    """
    repo = repo.resolve()
    text = diff_path.read_text(encoding="utf-8")
    if is_empty_diff(text):
        raise ApplyError(f"{diff_path} 里没有改动, 无需应用")
    summary = summarize_diff(text)

    if git(repo, "rev-parse", "--is-inside-work-tree", check=False).returncode != 0:
        raise ApplyError(f"{repo} 不是 git 仓库")
    top = Path(git(repo, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
    if top != repo:
        raise ApplyError(f"请指向仓库根目录 {top}(diff 路径相对仓库根)")
    if not allow_dirty and git(repo, "status", "--porcelain").stdout.strip():
        raise ApplyError("工作区有未提交的改动, 请先提交或暂存; 确需继续可加 --allow-dirty")

    check = git(repo, "apply", "--check", str(diff_path), check=False)
    if check.returncode != 0:
        raise ApplyError(
            "diff 无法干净应用(仓库可能已经变化), 未做任何修改:\n"
            + (check.stderr or check.stdout).strip()
        )

    branch = branch or f"kcloud/{datetime.now():%Y%m%d-%H%M%S}"
    if git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}",
           check=False).returncode == 0:
        raise ApplyError(f"分支 {branch} 已存在, 请用 --branch 换个名字")
    git(repo, "switch", "-c", branch)
    git(repo, "apply", str(diff_path))
    if commit:
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", f"kcloud: apply {diff_path.name}\n\nsource: {diff_path}")
    return branch, summary
