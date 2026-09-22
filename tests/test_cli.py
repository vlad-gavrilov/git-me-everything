from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_help_has_no_config_side_effect(run_cli, tmp_path):
    result = run_cli("--help")

    assert result.returncode == 0
    assert not (tmp_path / "profiles.json").exists()


@pytest.mark.parametrize(
    ("environment_name", "default_function", "legacy_function", "suffix", "legacy_kind"),
    [
        (
            "XDG_CONFIG_HOME",
            "default_config_path",
            "user_config_dir",
            Path("git-me-everything/profiles.json"),
            "file",
        ),
        (
            "XDG_STATE_HOME",
            "default_state_path",
            "user_state_dir",
            Path("git-me-everything"),
            "directory",
        ),
    ],
)
def test_default_storage_paths_prefer_explicit_xdg_then_existing_legacy(
    mirror_module,
    monkeypatch,
    tmp_path,
    environment_name,
    default_function,
    legacy_function,
    suffix,
    legacy_kind,
):
    xdg_home = tmp_path / "xdg"
    legacy_home = tmp_path / "legacy"
    legacy_target = legacy_home / "profiles.json" if legacy_kind == "file" else legacy_home
    monkeypatch.setattr(mirror_module, "_uses_xdg_layout", lambda: True)
    monkeypatch.setattr(mirror_module.sys, "platform", "darwin")
    monkeypatch.setattr(mirror_module, legacy_function, lambda _name: str(legacy_home))
    monkeypatch.setenv(environment_name, str(xdg_home))

    resolver = getattr(mirror_module, default_function)
    if legacy_kind == "file":
        legacy_target.parent.mkdir(parents=True)
        legacy_target.write_text("legacy")
    else:
        legacy_target.mkdir(parents=True)
    assert resolver() == xdg_home / suffix

    if legacy_kind == "file":
        legacy_target.unlink()
        legacy_target.parent.rmdir()
    else:
        legacy_target.rmdir()

    monkeypatch.delenv(environment_name)
    home = tmp_path / "home"
    monkeypatch.setattr(mirror_module.Path, "home", classmethod(lambda _cls: home))
    monkeypatch.setenv("HOME", str(home))
    fallback = (
        home / ".config" / suffix
        if environment_name == "XDG_CONFIG_HOME"
        else home / ".local" / "state" / suffix
    )
    assert resolver() == fallback

    if legacy_kind == "file":
        legacy_target.parent.mkdir(parents=True)
        legacy_target.write_text("{}")
    else:
        legacy_target.mkdir(parents=True)
    assert resolver() == legacy_target


@pytest.mark.parametrize(
    (
        "environment_name",
        "default_function",
        "legacy_function",
        "suffix",
        "legacy_kind",
    ),
    [
        (
            "XDG_CONFIG_HOME",
            "default_config_path",
            "user_config_dir",
            Path(".config/git-me-everything/profiles.json"),
            "file",
        ),
        (
            "XDG_STATE_HOME",
            "default_state_path",
            "user_state_dir",
            Path(".local/state/git-me-everything"),
            "directory",
        ),
    ],
)
def test_new_xdg_path_wins_over_legacy_and_relative_xdg_is_ignored(
    mirror_module,
    monkeypatch,
    tmp_path,
    environment_name,
    default_function,
    legacy_function,
    suffix,
    legacy_kind,
):
    home = tmp_path / "home"
    preferred = home / suffix
    legacy_root = tmp_path / "legacy"
    legacy = legacy_root / "profiles.json" if legacy_kind == "file" else legacy_root
    if legacy_kind == "file":
        preferred.parent.mkdir(parents=True)
        preferred.write_text("{}")
        legacy.parent.mkdir(parents=True)
        legacy.write_text("{}")
    else:
        preferred.mkdir(parents=True)
        legacy.mkdir(parents=True)
    monkeypatch.setattr(mirror_module, "_uses_xdg_layout", lambda: True)
    monkeypatch.setattr(mirror_module.sys, "platform", "darwin")
    monkeypatch.setattr(mirror_module.Path, "home", classmethod(lambda _cls: home))
    monkeypatch.setattr(mirror_module, legacy_function, lambda _name: str(legacy_root))
    monkeypatch.setenv(environment_name, "relative/config")

    assert getattr(mirror_module, default_function)() == preferred


@pytest.mark.parametrize(
    ("environment_name", "default_function", "legacy_function", "legacy_base", "suffix"),
    [
        (
            "XDG_CONFIG_HOME",
            "default_config_path",
            "user_config_dir",
            Path("relative/config/git-me-everything"),
            Path(".config/git-me-everything/profiles.json"),
        ),
        (
            "XDG_STATE_HOME",
            "default_state_path",
            "user_state_dir",
            Path("relative/state/git-me-everything"),
            Path(".local/state/git-me-everything"),
        ),
    ],
)
def test_linux_ignores_relative_xdg_values_from_platformdirs(
    mirror_module,
    monkeypatch,
    tmp_path,
    environment_name,
    default_function,
    legacy_function,
    legacy_base,
    suffix,
):
    monkeypatch.setattr(mirror_module, "_uses_xdg_layout", lambda: True)
    monkeypatch.setattr(mirror_module.sys, "platform", "linux")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(mirror_module, legacy_function, lambda _name: str(legacy_base))
    home = tmp_path / "home"
    monkeypatch.setattr(mirror_module.Path, "home", classmethod(lambda _cls: home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv(environment_name, "relative/value")
    legacy_target = tmp_path / legacy_base
    if environment_name == "XDG_CONFIG_HOME":
        legacy_target.mkdir(parents=True)
        (legacy_target / "profiles.json").write_text("{}")
    else:
        legacy_target.mkdir(parents=True)

    assert getattr(mirror_module, default_function)() == tmp_path / "home" / suffix


def test_windows_keeps_platformdirs_defaults(mirror_module, monkeypatch, tmp_path):
    native_config = tmp_path / "native-config"
    native_state = tmp_path / "native-state"
    monkeypatch.setattr(mirror_module, "_uses_xdg_layout", lambda: False)
    monkeypatch.setattr(mirror_module, "user_config_dir", lambda _name: str(native_config))
    monkeypatch.setattr(mirror_module, "user_state_dir", lambda _name: str(native_state))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "ignored-config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "ignored-state"))

    assert mirror_module.default_config_path() == native_config / "profiles.json"
    assert mirror_module.default_state_path() == native_state


def test_explicit_storage_paths_do_not_resolve_defaults(mirror_module, monkeypatch, tmp_path):
    config = tmp_path / "profiles.json"
    state = tmp_path / "state"

    def inaccessible_default():
        raise PermissionError("inaccessible default storage")

    monkeypatch.setattr(mirror_module, "default_config_path", inaccessible_default)
    monkeypatch.setattr(mirror_module, "default_state_path", inaccessible_default)

    result = mirror_module.main(
        [
            "--config",
            str(config),
            "--state-dir",
            str(state),
            "profile",
            "list",
        ]
    )

    assert result == 0
    assert not config.exists()
    assert not state.exists()


def test_sync_requires_explicit_scope(run_cli):
    result = run_cli("sync")

    assert result.returncode == 2
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "SYNC_SCOPE_REQUIRED"


def test_repo_selector_requires_one_profile(run_cli):
    result = run_cli("sync", "--all-profiles", "--repo", "team/project")

    assert result.returncode == 2
    assert json.loads(result.stderr)["error"]["code"] == "INVALID_REPO_SCOPE"


def test_ssh_version_probe_uses_capital_v(mirror_module, monkeypatch):
    seen = []
    monkeypatch.setattr(
        mirror_module,
        "run_probe",
        lambda argv: seen.append(argv) or "OpenSSH_9.9",
    )

    assert mirror_module.probe_tool_version("ssh") == "OpenSSH_9.9"
    assert seen == [["ssh", "-V"]]


def test_report_filters_and_paginates_latest_run(run_cli, tmp_path):
    runs = tmp_path / "state" / "runs"
    old = runs / "00000000-0000-4000-8000-000000000001"
    latest = runs / "00000000-0000-4000-8000-000000000002"
    old.mkdir(parents=True)
    latest.mkdir()
    (old / "report.json").write_text(
        json.dumps(
            {
                "run_id": old.name,
                "started_at": "2026-09-21T10:00:00Z",
                "repositories": [{"namespace": "old/repo", "status": "error"}],
            }
        )
    )
    (latest / "report.json").write_text(
        json.dumps(
            {
                "run_id": latest.name,
                "started_at": "2026-09-21T11:00:00Z",
                "repositories": [
                    {"namespace": "b/failure", "status": "error", "reason": "GIT_FAILED"},
                    {"namespace": "a/ok", "status": "unchanged", "reason": None},
                    {
                        "namespace": "c/missing",
                        "status": "skipped",
                        "reason": "SOURCE_NOT_LISTED",
                    },
                ],
            }
        )
    )

    result = run_cli("report", "--errors-only", "--offset", "1", "--limit", "1")

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["run_id"] == latest.name
    assert payload["offset"] == 1
    assert payload["limit"] == 1
    assert payload["total"] == 2
    assert payload["items"] == [
        {"namespace": "c/missing", "status": "skipped", "reason": "SOURCE_NOT_LISTED"}
    ]


def test_profile_persists_only_token_environment_name(
    mirror_module,
    run_cli,
    tmp_path,
):
    secret = "unique-secret-that-must-not-be-written"

    result = run_cli(
        "profile",
        "add",
        "--name",
        "work",
        "--provider",
        "github",
        "--api-url",
        "https://github.example/api/v3",
        "--root",
        str(tmp_path / "mirrors"),
        "--token-env",
        "GME_WORK_TOKEN",
        env={"GME_WORK_TOKEN": secret},
    )

    assert result.returncode == 0, result.stderr
    raw = (tmp_path / "profiles.json").read_text()
    payload = json.loads(raw)
    assert payload["profiles"][0]["token_env"] == "GME_WORK_TOKEN"
    assert secret not in raw
    assert "token_value" not in raw

    profiles = mirror_module.load_profiles(tmp_path / "profiles.json")
    nested_config = tmp_path / "new-config-directory" / "profiles.json"
    mirror_module.save_profiles(nested_config, profiles)
    assert nested_config.is_file()

    payload["profiles"][0]["token_value"] = secret
    (tmp_path / "profiles.json").write_text(json.dumps(payload))
    with pytest.raises(mirror_module.MirrorError) as caught:
        mirror_module.load_profiles(tmp_path / "profiles.json")
    assert caught.value.code == "CONFIG_INVALID"


def test_public_profile_needs_no_token_and_legacy_profile_keeps_ssh(
    mirror_module,
    run_cli,
    tmp_path,
):
    result = run_cli(
        "profile",
        "add",
        "--name",
        "octocat",
        "--provider",
        "github",
        "--api-url",
        "https://api.github.com",
        "--root",
        str(tmp_path / "mirrors"),
        "--public-user",
        "octocat",
    )

    assert result.returncode == 0, result.stderr
    profile = json.loads(result.stdout)["profile"]
    assert profile["token_env"] is None
    assert profile["discovery_mode"] == "public-user"
    assert profile["target"] == "octocat"
    assert profile["transport"] == "https"

    switched = run_cli("profile", "update", "--profile", "octocat", "--transport", "ssh")
    assert switched.returncode == 0, switched.stderr

    configured = mirror_module.load_profiles(tmp_path / "profiles.json")[0]
    root = mirror_module.ManagedRoot(configured)
    root.ensure()
    manifest = root.load_manifest()
    manifest["repositories"] = {"octocat/hello-world": {}}
    root.save_manifest(manifest)
    blocked_switch = run_cli("profile", "update", "--profile", "octocat", "--transport", "https")
    assert blocked_switch.returncode == 2
    assert json.loads(blocked_switch.stderr)["error"]["code"] == "TRANSPORT_CHANGE_UNSAFE"

    ssh_key = tmp_path / "id_ed25519"
    ssh_key.write_text("test key")
    ssh_profile = run_cli(
        "profile",
        "add",
        "--name",
        "ssh-ready",
        "--provider",
        "github",
        "--api-url",
        "https://api.github.com",
        "--root",
        str(tmp_path / "ssh-ready"),
        "--public-user",
        "octocat",
        "--transport",
        "ssh",
        "--ssh-key",
        str(ssh_key),
    )
    assert ssh_profile.returncode == 0, ssh_profile.stderr
    switched_to_https = run_cli(
        "profile", "update", "--profile", "ssh-ready", "--transport", "https"
    )
    assert switched_to_https.returncode == 0, switched_to_https.stderr
    assert json.loads(switched_to_https.stdout)["profile"]["ssh_key"] is None

    with_token = run_cli(
        "profile",
        "update",
        "--profile",
        "octocat",
        "--token-env",
        "GME_OCTOCAT_TOKEN",
    )
    assert with_token.returncode == 0, with_token.stderr
    cleared = run_cli("profile", "update", "--profile", "octocat", "--clear-token-env")
    assert cleared.returncode == 0, cleared.stderr
    assert json.loads(cleared.stdout)["profile"]["token_env"] is None

    missing_token = run_cli(
        "profile",
        "add",
        "--name",
        "private",
        "--provider",
        "github",
        "--api-url",
        "https://api.github.com",
        "--root",
        str(tmp_path / "private"),
    )
    assert missing_token.returncode == 2
    assert json.loads(missing_token.stderr)["error"]["code"] == "PROFILE_INVALID"

    legacy = {
        "schema_version": 1,
        "profiles": [
            {
                "profile_id": "00000000-0000-4000-8000-000000000001",
                "name": "legacy",
                "provider": "gitlab",
                "api_url": "https://gitlab.example/api/v4",
                "root": str(tmp_path / "legacy"),
                "token_env": "GME_LEGACY_TOKEN",
                "jobs": 4,
                "git_timeout_s": 300,
                "run_timeout_s": 3600,
                "ssh_key": None,
                "ca_bundle": None,
            }
        ],
    }
    legacy_path = tmp_path / "legacy.json"
    legacy_path.write_text(json.dumps(legacy))
    loaded = mirror_module.load_profiles(legacy_path)[0]
    assert loaded.discovery_mode == "accessible"
    assert loaded.target is None
    assert loaded.transport == "ssh"


def test_doctor_labels_single_repository_probe(mirror_module, monkeypatch, tmp_path):
    profile = mirror_module.Profile(
        profile_id="00000000-0000-4000-8000-000000000001",
        name="work",
        provider="github",
        api_url="https://github.example/api/v3",
        root=tmp_path / "mirrors",
        token_env="GME_WORK_TOKEN",
    )
    repo = mirror_module.Repo(
        project_id="10",
        namespace="team/project",
        ssh_url="git@github.example:team/project.git",
        http_url="https://github.example/team/project.git",
        default_branch="main",
    )
    monkeypatch.setenv(profile.token_env, "secret")
    monkeypatch.setattr(mirror_module, "probe_tool_version", lambda name: f"{name}-version")

    class Client:
        def current_user(self, _profile, _token):
            return {"login": "user"}

        def discover_all(self, _profile, _token, _cancel):
            return [repo]

    class Runner:
        def __init__(self):
            self.calls = []

        def run(self, argv, **_kwargs):
            self.calls.append(argv)
            return SimpleNamespace(stdout="ok\n", stderr="", returncode=0)

    runner = Runner()
    result = mirror_module.doctor(profile, client=Client(), runner=runner)

    assert result["ok"] is True
    assert result["git_probe"]["scope"] == "one_repository"
    assert result["git_probe"]["transport"] == "ssh"
    assert result["tools"]["ssh"] == "ssh-version"
    assert len(runner.calls) == 1
    assert "ls-remote" in runner.calls[0]


def test_doctor_https_public_profile_does_not_require_ssh(mirror_module, monkeypatch, tmp_path):
    profile = mirror_module.Profile(
        profile_id="00000000-0000-4000-8000-000000000001",
        name="public",
        provider="github",
        api_url="https://api.github.com",
        root=tmp_path / "mirrors",
        token_env=None,
        discovery_mode="public-user",
        target="octocat",
        transport="https",
    )
    repo = mirror_module.Repo(
        project_id="10",
        namespace="octocat/project",
        ssh_url="git@github.com:octocat/project.git",
        http_url="https://github.com/octocat/project.git",
        default_branch="main",
    )
    monkeypatch.setattr(
        mirror_module,
        "probe_tool_version",
        lambda name: None if name == "ssh" else f"{name}-version",
    )

    class Client:
        def current_user(self, *_args):
            raise AssertionError("public discovery must not call /user")

        def discover_all(self, _profile, _token, _cancel):
            return [repo]

    class Runner:
        def run(self, *_args, **_kwargs):
            return SimpleNamespace(stdout="ok\n", stderr="", returncode=0)

    result = mirror_module.doctor(profile, client=Client(), runner=Runner())

    assert result["ok"] is True
    assert result["authentication"] == "anonymous"
    assert "ssh" not in result["tools"]
    assert result["git_probe"]["transport"] == "https"
