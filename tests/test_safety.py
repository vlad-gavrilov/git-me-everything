from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from filelock import FileLock


def _profile(module, root: Path, token_env: str = "GME_WORK_TOKEN"):
    return module.Profile(
        profile_id="00000000-0000-4000-8000-000000000001",
        name="work",
        provider="github",
        api_url="https://github.example/api/v3",
        root=root,
        token_env=token_env,
    )


def test_redaction_removes_token_headers_and_url_userinfo(mirror_module):
    secret = "unique-secret-that-must-not-survive"
    payload = {
        "Authorization": f"Bearer {secret}",
        "nested": [
            f"request failed for https://alice:{secret}@github.example/team/repo.git",
            {"Private-Token": secret, "safe": "visible"},
        ],
    }

    redacted = mirror_module.redact(payload, [secret])
    encoded = json.dumps(redacted)

    assert secret not in encoded
    assert "alice:" not in encoded
    assert "Authorization" in encoded
    assert "visible" in encoded


def test_child_environments_strip_tokens_and_git_context(mirror_module, monkeypatch, tmp_path):
    profile = _profile(mirror_module, tmp_path)
    monkeypatch.setenv(profile.token_env, "do-not-inherit")
    monkeypatch.setenv("SECOND_PROFILE_TOKEN", "also-private")
    monkeypatch.setenv("GIT_DIR", "/outside/repository")
    monkeypatch.setenv("GIT_WORK_TREE", "/outside/worktree")
    monkeypatch.setenv("GIT_INDEX_FILE", "/outside/index")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.worktree")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "/outside")
    second = _profile(mirror_module, tmp_path / "second", "SECOND_PROFILE_TOKEN")

    git_env = mirror_module.git_environment([profile, second])
    ssh_env = mirror_module.ssh_environment([profile, second])

    for child_env in (git_env, ssh_env):
        assert profile.token_env not in child_env
        assert second.token_env not in child_env
        assert "GIT_DIR" not in child_env
        assert "GIT_WORK_TREE" not in child_env
        assert "GIT_INDEX_FILE" not in child_env
        assert not any(
            key.startswith("GIT_CONFIG_")
            for key in child_env
            if key not in {"GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM"}
        )
    assert git_env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert git_env["GIT_CONFIG_NOSYSTEM"] == "1"


@pytest.mark.parametrize(
    ("transport", "discovery_mode", "expected_origin", "expects_http_credentials"),
    [
        ("https", "public-user", "https://gitlab.example/team/project.git", False),
        ("https", "accessible", "https://gitlab.example/team/project.git", True),
        ("ssh", "accessible", "git@gitlab.example:team/project.git", False),
    ],
)
def test_git_transport_keeps_credentials_command_scoped_and_redacted(
    mirror_module,
    monkeypatch,
    tmp_path,
    transport,
    discovery_mode,
    expected_origin,
    expects_http_credentials,
):
    secret = "unique-secret-that-must-not-persist"
    profile = mirror_module.Profile(
        profile_id="00000000-0000-4000-8000-000000000001",
        name="work",
        provider="gitlab",
        api_url="https://gitlab.example/api/v4",
        root=tmp_path / "mirrors",
        token_env="GME_WORK_TOKEN",
        discovery_mode=discovery_mode,
        target="team" if discovery_mode == "public-user" else None,
        transport=transport,
    )
    repo = mirror_module.Repo(
        project_id="10",
        namespace="team/project",
        ssh_url="git@gitlab.example:team/project.git",
        http_url="https://gitlab.example/team/project.git",
        default_branch="main",
    )
    config = tmp_path / "profiles.json"
    state_dir = tmp_path / "state"
    mirror_module.save_profiles(config, [profile])
    monkeypatch.setenv(profile.token_env, secret)
    monkeypatch.setattr(mirror_module, "discover", lambda *_args: [repo])
    monkeypatch.setattr(mirror_module, "probe_tool_version", lambda _name: "version")
    calls = []

    def fail_git(argv, **kwargs):
        environment = kwargs["env"]
        calls.append((list(argv), environment))
        return SimpleNamespace(
            returncode=1,
            stdout="",
            stderr=f"Git rejected {secret} {environment.get('GIT_CONFIG_VALUE_0', '')}",
        )

    monkeypatch.setattr(mirror_module.subprocess, "run", fail_git)

    summary, exit_code = mirror_module.sync_profiles([profile], state_dir)

    assert exit_code == 1
    assert summary["counts"] == {"error": 1}
    assert len(calls) == 1
    argv, environment = calls[0]
    command = json.dumps(argv)
    header = environment.get("GIT_CONFIG_VALUE_0", "")
    assert expected_origin in argv
    assert secret not in command
    if header:
        assert header not in command
    assert profile.token_env not in environment
    if expects_http_credentials:
        assert environment["GIT_CONFIG_KEY_0"].endswith(".extraHeader")
        assert header.startswith("Authorization: Basic ")
        assert secret not in header
        assert environment["GIT_CONFIG_VALUE_1"] == "false"
        assert environment["GIT_CONFIG_VALUE_2"] == ""
    else:
        assert "GIT_CONFIG_COUNT" not in environment
    if transport == "ssh":
        assert "GIT_SSH_COMMAND" in environment

    artifacts = ""
    for path in tmp_path.rglob("*"):
        if path.is_file() and not path.name.endswith(".lock"):
            artifacts += path.read_text(errors="replace")
    assert secret not in artifacts
    if header:
        assert header not in artifacts


def _repo(module, remote, namespace="team/project"):
    return module.Repo(
        project_id="10",
        namespace=namespace,
        ssh_url=str(remote),
        http_url=None,
        default_branch="main",
    )


def test_unmanaged_path_and_origin_mismatch_are_preserved(
    mirror_module,
    make_remote,
    git_cmd,
    tmp_path,
):
    _source, remote = make_remote()
    _other_source, other_remote = make_remote("other")
    profile = _profile(mirror_module, tmp_path / "mirrors")
    repo = _repo(mirror_module, remote)
    unmanaged = profile.root / "team" / "project"
    unmanaged.mkdir(parents=True)
    sentinel = unmanaged / "keep.txt"
    sentinel.write_text("keep")

    conflict = mirror_module.sync_repositories(
        profile,
        [repo],
        allow_local_transport=True,
    )

    assert [(item.status, item.reason) for item in conflict] == [("skipped", "PATH_CONFLICT")]
    assert sentinel.read_text() == "keep"

    sentinel.unlink()
    unmanaged.rmdir()
    result = mirror_module.sync_repositories(profile, [repo], allow_local_transport=True)
    assert result[0].status == "cloned"
    git_cmd(unmanaged, "remote", "set-url", "origin", str(other_remote))

    mismatch = mirror_module.sync_repositories(
        profile,
        [repo],
        allow_local_transport=True,
    )
    assert [(item.status, item.reason) for item in mismatch] == [("skipped", "UNSAFE_REPOSITORY")]
    assert git_cmd(unmanaged, "remote", "get-url", "origin") == str(other_remote)


def test_symlink_escape_is_preserved(mirror_module, make_remote, tmp_path):
    _source, remote = make_remote()
    profile = _profile(mirror_module, tmp_path / "mirrors")
    managed_root = mirror_module.ManagedRoot(profile)
    managed_root.ensure()
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "must-survive.txt"
    sentinel.write_text("keep")
    (profile.root / "team").symlink_to(outside, target_is_directory=True)

    result = mirror_module.sync_repositories(
        profile,
        [_repo(mirror_module, remote)],
        allow_local_transport=True,
    )

    assert [(item.status, item.reason) for item in result] == [("skipped", "UNSAFE_REPOSITORY")]
    assert sentinel.read_text() == "keep"
    assert not (outside / "project").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression")
def test_junction_escape_is_preserved(mirror_module, make_remote, tmp_path):
    _source, remote = make_remote()
    profile = _profile(mirror_module, tmp_path / "mirrors")
    managed_root = mirror_module.ManagedRoot(profile)
    managed_root.ensure()
    outside = tmp_path / "outside"
    outside.mkdir()
    os.system(f'cmd /c mklink /J "{profile.root / "team"}" "{outside}"')

    result = mirror_module.sync_repositories(
        profile,
        [_repo(mirror_module, remote)],
        allow_local_transport=True,
    )

    assert result[0].reason == "UNSAFE_REPOSITORY"
    assert not (outside / "project").exists()


def test_external_core_worktree_is_preserved(
    mirror_module,
    make_remote,
    git_cmd,
    tmp_path,
):
    _source, remote = make_remote()
    profile = _profile(mirror_module, tmp_path / "mirrors")
    repo = _repo(mirror_module, remote)
    mirror_module.sync_repositories(profile, [repo], allow_local_transport=True)
    checkout = profile.root / "team" / "project"
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "must-survive.txt"
    sentinel.write_text("keep")
    git_cmd(checkout, "config", "core.worktree", str(outside))
    runner = mirror_module.ProcessRunner([profile])
    managed_root = mirror_module.ManagedRoot(profile)

    result = mirror_module.sync_repository(
        profile,
        repo,
        managed_root,
        runner,
        threading.Event(),
        allow_local_transport=True,
    )

    assert result.reason == "UNSAFE_REPOSITORY"
    assert sentinel.read_text() == "keep"
    assert runner.recorded_destructive_calls == []


def test_root_lock_blocks_a_second_sync(mirror_module, make_remote, tmp_path):
    _source, remote = make_remote()
    profile = _profile(mirror_module, tmp_path / "mirrors")
    managed_root = mirror_module.ManagedRoot(profile)
    managed_root.ensure()

    with FileLock(str(managed_root.lock_path)):
        with pytest.raises(mirror_module.MirrorError) as caught:
            mirror_module.sync_repositories(
                profile,
                [_repo(mirror_module, remote)],
                allow_local_transport=True,
            )

    assert caught.value.code == "ROOT_LOCKED"


def test_cancelled_clone_removes_staging(mirror_module, make_remote, tmp_path):
    _source, remote = make_remote()
    profile = _profile(mirror_module, tmp_path / "mirrors")
    repo = _repo(mirror_module, remote)
    cancel = threading.Event()

    class CancelAfterClone(mirror_module.ProcessRunner):
        def run(self, argv, **kwargs):
            completed = super().run(argv, **kwargs)
            if "clone" in argv:
                cancel.set()
            return completed

    runner = CancelAfterClone([profile])
    result = mirror_module.sync_repositories(
        profile,
        [repo],
        runner=runner,
        cancel=cancel,
        allow_local_transport=True,
    )

    assert [(item.status, item.reason) for item in result] == [("skipped", "INTERRUPTED")]
    assert not (profile.root / "team" / "project").exists()
    assert not list(profile.root.rglob("*.staging-*"))


def test_token_never_reaches_sync_error_artifacts(
    mirror_module,
    monkeypatch,
    capsys,
    tmp_path,
):
    secret = "unique-secret-from-provider-error"
    profile = _profile(mirror_module, tmp_path / "mirrors")
    config = tmp_path / "profiles.json"
    state_dir = tmp_path / "state"
    mirror_module.save_profiles(config, [profile])
    monkeypatch.setenv(profile.token_env, secret)

    def fail_discovery(*_args):
        raise mirror_module.MirrorError(
            "DISCOVERY_FAILED",
            f"request failed for https://alice:{secret}@github.example/api",
            exit_code=3,
        )

    monkeypatch.setattr(mirror_module, "discover", fail_discovery)

    exit_code = mirror_module.main(
        [
            "--config",
            str(config),
            "--state-dir",
            str(state_dir),
            "sync",
            "--profile",
            profile.name,
        ]
    )

    captured = capsys.readouterr()
    artifacts = captured.out + captured.err
    for path in tmp_path.rglob("*"):
        if path.is_file() and not path.name.endswith(".lock"):
            artifacts += path.read_text(errors="replace")
    assert exit_code == 3
    assert secret not in artifacts
    assert "alice:" not in artifacts
