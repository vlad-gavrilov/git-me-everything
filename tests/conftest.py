from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "mirror.py"


@pytest.fixture(scope="session")
def mirror_module():
    spec = importlib.util.spec_from_file_location("git_me_everything_script", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def run_cli(tmp_path):
    def run(*args: str, env: dict[str, str] | None = None):
        command = [
            sys.executable,
            str(SCRIPT),
            "--config",
            str(tmp_path / "profiles.json"),
            "--state-dir",
            str(tmp_path / "state"),
            *args,
        ]
        child_env = os.environ.copy()
        child_env.update(env or {})
        return subprocess.run(
            command,
            cwd=tmp_path,
            text=True,
            capture_output=True,
            env=child_env,
            check=False,
        )

    return run


def json_output(completed: subprocess.CompletedProcess[str]) -> object:
    return json.loads(completed.stdout or completed.stderr)


@pytest.fixture
def git_cmd():
    def run(cwd: Path, *args: str) -> str:
        completed = subprocess.run(
            ["git", *args],
            cwd=cwd,
            text=True,
            capture_output=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        return completed.stdout.strip()

    return run


@pytest.fixture
def make_remote(tmp_path, git_cmd):
    created = 0

    def make(name: str = "remote"):
        nonlocal created
        created += 1
        source = tmp_path / f"{name}-source-{created}"
        remote = tmp_path / f"{name}-{created}.git"
        source.mkdir()
        git_cmd(source, "init", "-b", "main")
        git_cmd(source, "config", "user.name", "Test User")
        git_cmd(source, "config", "user.email", "test@example.invalid")
        (source / "README.md").write_text("initial\n")
        git_cmd(source, "add", "README.md")
        git_cmd(source, "commit", "-m", "initial")
        git_cmd(tmp_path, "clone", "--bare", str(source), str(remote))
        git_cmd(source, "remote", "add", "origin", str(remote))
        return source, remote

    return make
