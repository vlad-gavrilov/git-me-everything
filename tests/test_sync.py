from __future__ import annotations

import json
import threading


def _profile(module, root):
    return module.Profile(
        profile_id="00000000-0000-4000-8000-000000000001",
        name="work",
        provider="github",
        api_url="https://github.example/api/v3",
        root=root,
        token_env="GME_TEST_TOKEN",
    )


def _repo(module, remote, namespace="team/project", project_id="10"):
    return module.Repo(
        project_id=project_id,
        namespace=namespace,
        ssh_url=str(remote),
        http_url=None,
        default_branch="main",
    )


def test_local_remote_sync_is_idempotent(mirror_module, make_remote, git_cmd, tmp_path):
    source, remote = make_remote()
    profile = _profile(mirror_module, tmp_path / "mirrors")
    repo = _repo(mirror_module, remote)
    runner = mirror_module.ProcessRunner([profile])

    first = mirror_module.sync_repositories(
        profile,
        [repo],
        runner=runner,
        allow_local_transport=True,
    )

    checkout = profile.root / "team" / "project"
    assert [item.status for item in first] == ["cloned"]
    assert (checkout / "README.md").read_text() == "initial\n"
    manifest = json.loads((profile.root / mirror_module.MANIFEST_NAME).read_text())
    assert next(iter(manifest["repositories"].values()))["project_id"] == "10"

    (checkout / "discard-me.txt").write_text("local data")
    git_cmd(source, "checkout", "-b", "feature")
    (source / "feature.txt").write_text("feature\n")
    git_cmd(source, "add", "feature.txt")
    git_cmd(source, "commit", "-m", "feature")
    git_cmd(source, "tag", "v1")
    git_cmd(source, "push", "origin", "--all")
    git_cmd(source, "push", "origin", "--tags")

    second = mirror_module.sync_repositories(
        profile,
        [repo],
        runner=runner,
        allow_local_transport=True,
    )

    assert [item.status for item in second] == ["updated"]
    assert not (checkout / "discard-me.txt").exists()
    assert git_cmd(checkout, "rev-parse", "refs/heads/feature") == git_cmd(
        checkout,
        "rev-parse",
        "refs/remotes/origin/feature",
    )
    assert git_cmd(checkout, "rev-parse", "refs/tags/v1")

    third = mirror_module.sync_repositories(
        profile,
        [repo],
        runner=runner,
        allow_local_transport=True,
    )
    assert [item.status for item in third] == ["unchanged"]
    assert runner.recorded_destructive_calls
    for command in runner.recorded_destructive_calls:
        git_dir = next(
            argument.removeprefix("--git-dir=")
            for argument in command
            if argument.startswith("--git-dir=")
        )
        work_tree = next(
            argument.removeprefix("--work-tree=")
            for argument in command
            if argument.startswith("--work-tree=")
        )
        assert mirror_module.Path(git_dir).is_absolute()
        assert mirror_module.Path(work_tree).is_absolute()
        assert profile.root in mirror_module.Path(work_tree).parents


def test_identity_reconciliation_preserves_registered_path(
    mirror_module,
    make_remote,
    tmp_path,
):
    _source, remote = make_remote()
    _other_source, other_remote = make_remote("other")
    profile = _profile(mirror_module, tmp_path / "mirrors")
    repo = _repo(mirror_module, remote)
    mirror_module.sync_repositories(profile, [repo], allow_local_transport=True)
    old_path = profile.root / "team" / "project"

    missing = mirror_module.sync_repositories(
        profile,
        [],
        allow_local_transport=True,
    )
    moved = mirror_module.sync_repositories(
        profile,
        [_repo(mirror_module, remote, "renamed/project")],
        allow_local_transport=True,
    )
    changed_origin = mirror_module.sync_repositories(
        profile,
        [_repo(mirror_module, other_remote)],
        allow_local_transport=True,
    )

    assert [(item.status, item.reason) for item in missing] == [("skipped", "SOURCE_NOT_LISTED")]
    assert [(item.status, item.reason) for item in moved] == [("skipped", "REMOTE_PATH_CHANGED")]
    assert [(item.status, item.reason) for item in changed_origin] == [
        ("skipped", "REMOTE_PATH_CHANGED")
    ]
    assert old_path.is_dir()
    assert not (profile.root / "renamed" / "project").exists()
    assert len(list(profile.root.rglob(".git"))) == 1


def test_unknown_exact_repo_selector_fails_before_mutation(
    mirror_module,
    make_remote,
    tmp_path,
):
    _source, remote = make_remote()
    profile = _profile(mirror_module, tmp_path / "mirrors")
    repo = _repo(mirror_module, remote)
    runner = mirror_module.ProcessRunner([profile])

    try:
        mirror_module.sync_repositories(
            profile,
            [repo],
            selection=["team/absent"],
            runner=runner,
            allow_local_transport=True,
        )
    except mirror_module.MirrorError as exc:
        assert exc.code == "REPO_NOT_FOUND"
        assert exc.exit_code == 2
    else:
        raise AssertionError("unknown selector was accepted")
    assert runner.calls == []


def test_report_states_exit_codes_and_summary_contract(
    mirror_module,
    make_remote,
    monkeypatch,
    tmp_path,
):
    _source, remote = make_remote()
    profile = _profile(mirror_module, tmp_path / "mirrors")
    repo = _repo(mirror_module, remote)
    state_dir = tmp_path / "state"
    monkeypatch.setenv(profile.token_env, "secret")
    monkeypatch.setattr(mirror_module, "discover", lambda *_args: [repo])

    completed, completed_exit = mirror_module.sync_profiles(
        [profile],
        state_dir,
        allow_local_transport=True,
    )

    assert completed_exit == 0
    assert completed["state"] == "completed"
    assert completed["requires_attention"] is False
    assert completed["counts"] == {"cloned": 1}
    assert mirror_module.Path(completed["report_path"]).is_absolute()
    assert len(json.dumps(completed, separators=(",", ":")).splitlines()) == 1
    report = json.loads(mirror_module.Path(completed["report_path"]).read_text())
    required = {
        "schema_version",
        "run_id",
        "state",
        "started_at",
        "finished_at",
        "tool",
        "capabilities",
        "profiles",
        "repositories",
        "counts",
    }
    assert required <= report.keys()
    assert report["capabilities"] == {
        "history": "complete",
        "branches": "complete",
        "tags": "complete",
        "lfs": "pointers-only",
        "submodules": "not-initialized",
    }

    monkeypatch.setattr(mirror_module, "discover", lambda *_args: [])
    partial, partial_exit = mirror_module.sync_profiles(
        [profile],
        state_dir,
        allow_local_transport=True,
    )
    assert partial_exit == 1
    assert partial["state"] == "partial"
    assert partial["requires_attention"] is True
    assert partial["problems"][0]["reason"] == "SOURCE_NOT_LISTED"
    assert len(partial["problems"]) <= 20
    assert "problems_truncated" in partial

    def fail_discovery(*_args):
        raise mirror_module.MirrorError("DISCOVERY_FAILED", "provider failed", exit_code=3)

    monkeypatch.setattr(mirror_module, "discover", fail_discovery)
    incomplete, incomplete_exit = mirror_module.sync_profiles([profile], state_dir)
    assert incomplete_exit == 3
    assert incomplete["state"] == "incomplete"

    cancel = threading.Event()
    cancel.set()
    interrupted, interrupted_exit = mirror_module.sync_profiles(
        [profile],
        state_dir,
        cancel=cancel,
    )
    assert interrupted_exit == 130
    assert interrupted["state"] == "interrupted"
