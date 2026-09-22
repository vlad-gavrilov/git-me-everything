#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "requests>=2.32,<3",
#   "platformdirs>=4,<5",
#   "filelock>=3.16,<4",
# ]
# ///
"""Mirror GitHub and GitLab repositories into managed local checkouts."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import threading
import time
import unicodedata
import uuid
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import requests
from filelock import FileLock, Timeout
from platformdirs import user_config_dir, user_state_dir

# Models and constants

TOOL_NAME = "git-me-everything"
TOOL_VERSION = "0.1.0"
SCHEMA_VERSION = 1
DEFAULT_PAGE_LIMIT = 50
MAX_PAGE_LIMIT = 200
SUCCESS_STATUSES = frozenset({"cloned", "updated", "unchanged"})
TOKEN_ENV_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
DISCOVERY_MODES = frozenset({"accessible", "public-user", "public-group"})
TRANSPORTS = frozenset({"https", "ssh"})
PROFILE_FIELDS = frozenset(
    {
        "profile_id",
        "name",
        "provider",
        "api_url",
        "root",
        "token_env",
        "discovery_mode",
        "target",
        "transport",
        "jobs",
        "git_timeout_s",
        "run_timeout_s",
        "ssh_key",
        "ca_bundle",
    }
)
URL_USERINFO_PATTERN = re.compile(r"(?P<scheme>https?://)[^/@\s]+@", re.IGNORECASE)
SECRET_HEADER_NAMES = frozenset({"authorization", "private-token"})
ROOT_MARKER_NAME = ".git-me-everything-root.json"
MANIFEST_NAME = ".git-me-everything-manifest.json"
ROOT_LOCK_NAME = ".git-me-everything.lock"
CAPABILITIES = {
    "history": "complete",
    "branches": "complete",
    "tags": "complete",
    "lfs": "pointers-only",
    "submodules": "not-initialized",
}
WINDOWS_RESERVED_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{number}" for number in range(1, 10)),
        *(f"LPT{number}" for number in range(1, 10)),
    }
)


@dataclass(frozen=True)
class Profile:
    profile_id: str
    name: str
    provider: str
    api_url: str
    root: Path
    token_env: str | None
    jobs: int = 4
    git_timeout_s: float = 300.0
    run_timeout_s: float = 3600.0
    ssh_key: Path | None = None
    ca_bundle: Path | None = None
    discovery_mode: str = "accessible"
    target: str | None = None
    transport: str = "ssh"


@dataclass(frozen=True)
class Repo:
    project_id: str
    namespace: str
    ssh_url: str
    http_url: str | None
    default_branch: str | None
    enabled: bool = True


@dataclass(frozen=True)
class WorkResult:
    profile_id: str
    project_id: str
    namespace: str
    path: str | None
    status: str
    reason: str | None = None
    message: str | None = None


@dataclass(frozen=True)
class PlannedRepository:
    repo: Repo
    relative_path: str
    action: str
    reason: str | None = None


@dataclass(frozen=True)
class RepoPaths:
    root: Path
    work_tree: Path
    git_dir: Path


class MirrorError(Exception):
    """A bounded error that is safe to emit as JSON."""

    def __init__(self, code: str, message: str, *, exit_code: int = 1) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        emit_error("INVALID_ARGUMENT", message)
        raise SystemExit(2)


# Redaction and environment isolation


def redact(value: object, secrets: Sequence[str] = ()) -> object:
    """Return a JSON-compatible value with exact secret strings removed."""

    if isinstance(value, str):
        result = value
        for secret in secrets:
            if secret:
                result = result.replace(secret, "[REDACTED]")
                if not secret.startswith("Authorization: Basic "):
                    for provider in ("github", "gitlab"):
                        result = result.replace(
                            _http_authorization_header(provider, secret), "[REDACTED]"
                        )
        result = URL_USERINFO_PATTERN.sub(r"\g<scheme>[REDACTED]@", result)
        result = re.sub(
            r"(?i)\b(authorization|private-token)\s*[:=]\s*[^\s,;]+",
            r"\1: [REDACTED]",
            result,
        )
        return result
    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]"
            if str(key).lower() in SECRET_HEADER_NAMES
            else redact(item, secrets)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item, secrets) for item in value]
    return value


def _base_child_environment(profiles: Sequence[Profile]) -> dict[str, str]:
    allowed = {
        "COMSPEC",
        "HOME",
        "HOMEDRIVE",
        "HOMEPATH",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "LANG",
        "LOGNAME",
        "NO_PROXY",
        "PATH",
        "PATHEXT",
        "SSH_AGENT_PID",
        "SSH_AUTH_SOCK",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USER",
        "USERNAME",
        "WINDIR",
        "https_proxy",
        "http_proxy",
        "no_proxy",
    }
    token_names = {profile.token_env for profile in profiles if profile.token_env is not None}
    return {
        key: item
        for key, item in os.environ.items()
        if (key in allowed or key.startswith("LC_"))
        and key not in token_names
        and key not in {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"}
        and not key.startswith("GIT_CONFIG_")
    }


def ssh_environment(profiles: Sequence[Profile]) -> dict[str, str]:
    return _base_child_environment(profiles)


def git_environment(
    profiles: Sequence[Profile],
    *,
    ssh_key: Path | None = None,
    http_origin: str | None = None,
    http_token: str | None = None,
    ca_bundle: Path | None = None,
) -> dict[str, str]:
    environment = _base_child_environment(profiles)
    ssh_parts = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
    ]
    if ssh_key is not None:
        ssh_parts.extend(("-i", str(ssh_key), "-o", "IdentitiesOnly=yes"))
    environment.update(
        {
            "GCM_INTERACTIVE": "Never",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_LFS_SKIP_SMUDGE": "1",
            "GIT_SSH_COMMAND": " ".join(shlex.quote(part) for part in ssh_parts),
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    if ca_bundle is not None:
        environment["GIT_SSL_CAINFO"] = str(ca_bundle)
    if http_token is not None:
        if http_origin is None:
            raise MirrorError("PROCESS_INVALID", "HTTPS credentials need a repository origin")
        header = _http_authorization_header(profiles[0].provider, http_token)
        environment.update(
            {
                "GIT_CONFIG_COUNT": "3",
                "GIT_CONFIG_KEY_0": f"http.{http_origin}.extraHeader",
                "GIT_CONFIG_VALUE_0": header,
                "GIT_CONFIG_KEY_1": "http.followRedirects",
                "GIT_CONFIG_VALUE_1": "false",
                "GIT_CONFIG_KEY_2": "credential.helper",
                "GIT_CONFIG_VALUE_2": "",
            }
        )
    return environment


def _http_authorization_header(provider: str, token: str) -> str:
    username = "oauth2" if provider == "gitlab" else "x-access-token"
    encoded = base64.b64encode(f"{username}:{token}".encode()).decode()
    return f"Authorization: Basic {encoded}"


def _redaction_secrets(profile: Profile, token: str | None) -> list[str]:
    if token is None:
        return []
    return [token, _http_authorization_header(profile.provider, token)]


def _repository_git_environment(profile: Profile, repo: Repo) -> dict[str, str]:
    origin = _origin_for(profile, repo)
    http_token = (
        resolve_token(profile)
        if profile.transport == "https" and profile.discovery_mode == "accessible"
        else None
    )
    return git_environment(
        [profile],
        ssh_key=profile.ssh_key if profile.transport == "ssh" else None,
        http_origin=origin if http_token is not None else None,
        http_token=http_token,
        ca_bundle=profile.ca_bundle if profile.transport == "https" else None,
    )


# Configuration


def _uses_xdg_layout() -> bool:
    return os.name != "nt"


def _xdg_base(variable: str, fallback: Path) -> tuple[Path, bool]:
    value = os.environ.get(variable, "")
    candidate = Path(value) if value else None
    if candidate is not None and candidate.is_absolute():
        return candidate, True
    return Path.home() / fallback, False


def _prefer_storage_path(preferred: Path, legacy: Path, explicit: bool) -> Path:
    if explicit:
        return preferred
    try:
        if preferred.exists():
            return preferred
    except OSError:
        return preferred
    try:
        if legacy.exists():
            return legacy
    except OSError:
        pass
    return preferred


def default_config_path() -> Path:
    legacy = Path(user_config_dir(TOOL_NAME)) / "profiles.json"
    if not _uses_xdg_layout():
        return legacy
    base, explicit = _xdg_base("XDG_CONFIG_HOME", Path(".config"))
    preferred = base / TOOL_NAME / "profiles.json"
    if sys.platform.startswith("linux"):
        legacy = Path.home() / ".config" / TOOL_NAME / "profiles.json"
    return _prefer_storage_path(preferred, legacy, explicit)


def default_state_path() -> Path:
    legacy = Path(user_state_dir(TOOL_NAME))
    if not _uses_xdg_layout():
        return legacy
    base, explicit = _xdg_base("XDG_STATE_HOME", Path(".local/state"))
    preferred = base / TOOL_NAME
    if sys.platform.startswith("linux"):
        legacy = Path.home() / ".local" / "state" / TOOL_NAME
    return _prefer_storage_path(preferred, legacy, explicit)


def _profile_to_dict(profile: Profile) -> dict[str, object]:
    return {
        "profile_id": profile.profile_id,
        "name": profile.name,
        "provider": profile.provider,
        "api_url": profile.api_url,
        "root": str(profile.root),
        "token_env": profile.token_env,
        "discovery_mode": profile.discovery_mode,
        "target": profile.target,
        "transport": profile.transport,
        "jobs": profile.jobs,
        "git_timeout_s": profile.git_timeout_s,
        "run_timeout_s": profile.run_timeout_s,
        "ssh_key": str(profile.ssh_key) if profile.ssh_key else None,
        "ca_bundle": str(profile.ca_bundle) if profile.ca_bundle else None,
    }


def _profile_from_dict(raw: object) -> Profile:
    if not isinstance(raw, dict):
        raise MirrorError("CONFIG_INVALID", "Profile entry must be an object", exit_code=2)
    if set(raw) - PROFILE_FIELDS:
        raise MirrorError("CONFIG_INVALID", "Profile entry has unknown fields", exit_code=2)
    try:
        profile = Profile(
            profile_id=str(raw["profile_id"]),
            name=str(raw["name"]),
            provider=str(raw["provider"]),
            api_url=str(raw["api_url"]),
            root=Path(str(raw["root"])),
            token_env=str(raw["token_env"]) if raw.get("token_env") is not None else None,
            jobs=int(raw.get("jobs", 4)),
            git_timeout_s=float(raw.get("git_timeout_s", 300.0)),
            run_timeout_s=float(raw.get("run_timeout_s", 3600.0)),
            ssh_key=Path(str(raw["ssh_key"])) if raw.get("ssh_key") else None,
            ca_bundle=Path(str(raw["ca_bundle"])) if raw.get("ca_bundle") else None,
            discovery_mode=str(raw.get("discovery_mode", "accessible")),
            target=str(raw["target"]) if raw.get("target") is not None else None,
            transport=str(raw.get("transport", "ssh")),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise MirrorError(
            "CONFIG_INVALID",
            "Profile entry has invalid fields",
            exit_code=2,
        ) from exc
    validate_profile(profile)
    return profile


def _paths_overlap(first: Path, second: Path) -> bool:
    try:
        first_resolved = first.resolve(strict=False)
        second_resolved = second.resolve(strict=False)
        return (
            first_resolved == second_resolved
            or first_resolved in second_resolved.parents
            or (second_resolved in first_resolved.parents)
        )
    except (OSError, RuntimeError):
        return False


def validate_profile(profile: Profile, peers: Sequence[Profile] = ()) -> None:
    if not profile.name or profile.name != profile.name.strip():
        raise MirrorError("PROFILE_INVALID", "Profile name must be non-empty", exit_code=2)
    if profile.provider not in {"github", "gitlab"}:
        raise MirrorError("PROFILE_INVALID", "Provider must be github or gitlab", exit_code=2)
    try:
        parsed = urlsplit(profile.api_url)
        _port = parsed.port
    except ValueError as exc:
        raise MirrorError("PROFILE_INVALID", "API URL is malformed", exit_code=2) from exc
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise MirrorError("PROFILE_INVALID", "API URL must be HTTPS without userinfo", exit_code=2)
    if not profile.root.is_absolute():
        raise MirrorError("PROFILE_INVALID", "Mirror root must be an absolute path", exit_code=2)
    if profile.discovery_mode not in DISCOVERY_MODES:
        raise MirrorError("PROFILE_INVALID", "Discovery mode is invalid", exit_code=2)
    if profile.transport not in TRANSPORTS:
        raise MirrorError("PROFILE_INVALID", "Git transport must be https or ssh", exit_code=2)
    if profile.discovery_mode == "accessible" and profile.target is not None:
        raise MirrorError(
            "PROFILE_INVALID", "Accessible profiles cannot have a public target", exit_code=2
        )
    if profile.discovery_mode != "accessible" and (
        not profile.target or profile.target != profile.target.strip()
    ):
        raise MirrorError("PROFILE_INVALID", "Public profiles need a non-empty target", exit_code=2)
    if profile.token_env is None:
        if profile.discovery_mode == "accessible":
            raise MirrorError(
                "PROFILE_INVALID", "Accessible profiles require --token-env", exit_code=2
            )
    elif not TOKEN_ENV_PATTERN.fullmatch(profile.token_env):
        raise MirrorError("PROFILE_INVALID", "Token environment name is invalid", exit_code=2)
    if not 1 <= profile.jobs <= 64:
        raise MirrorError("PROFILE_INVALID", "Jobs must be between 1 and 64", exit_code=2)
    if profile.git_timeout_s <= 0 or profile.run_timeout_s <= 0:
        raise MirrorError("PROFILE_INVALID", "Timeouts must be greater than zero", exit_code=2)
    if profile.transport != "ssh" and profile.ssh_key is not None:
        raise MirrorError("PROFILE_INVALID", "SSH key requires SSH transport", exit_code=2)
    for label, optional_path in (("SSH key", profile.ssh_key), ("CA bundle", profile.ca_bundle)):
        if optional_path is not None and (
            not optional_path.is_absolute() or not optional_path.is_file()
        ):
            raise MirrorError(
                "PROFILE_INVALID",
                f"{label} must be an existing absolute file",
                exit_code=2,
            )
    for peer in peers:
        if peer.profile_id == profile.profile_id:
            raise MirrorError("PROFILE_EXISTS", "Profile id already exists", exit_code=2)
        if peer.name.casefold() == profile.name.casefold():
            raise MirrorError(
                "PROFILE_EXISTS",
                f"Profile already exists: {profile.name}",
                exit_code=2,
            )
        if _paths_overlap(peer.root, profile.root):
            raise MirrorError("ROOT_OVERLAP", "Profile mirror roots must not overlap", exit_code=2)


def load_profiles(path: Path) -> list[Profile]:
    if not path.exists():
        return []
    with FileLock(str(path) + ".lock"):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MirrorError(
                "CONFIG_INVALID",
                "Cannot read profile configuration",
                exit_code=2,
            ) from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
        raise MirrorError("CONFIG_INVALID", "Unsupported profile configuration schema", exit_code=2)
    if set(raw) - {"schema_version", "profiles"}:
        raise MirrorError("CONFIG_INVALID", "Configuration has unknown fields", exit_code=2)
    values = raw.get("profiles")
    if not isinstance(values, list):
        raise MirrorError("CONFIG_INVALID", "Profiles must be a list", exit_code=2)
    profiles = [_profile_from_dict(item) for item in values]
    for index, profile in enumerate(profiles):
        validate_profile(profile, [*profiles[:index], *profiles[index + 1 :]])
    return profiles


def _atomic_write_json(path: Path, payload: object, secrets: Sequence[str] = ()) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    safe_payload = redact(payload, secrets)
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            if os.name != "nt":
                os.chmod(temporary, 0o600)
            json.dump(safe_payload, stream, ensure_ascii=False, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def save_profiles(path: Path, profiles: Sequence[Profile]) -> None:
    for index, profile in enumerate(profiles):
        validate_profile(profile, [*profiles[:index], *profiles[index + 1 :]])
    path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(path) + ".lock"):
        _atomic_write_json(
            path,
            {
                "schema_version": SCHEMA_VERSION,
                "profiles": [_profile_to_dict(profile) for profile in profiles],
            },
        )


def resolve_token(profile: Profile, environ: Mapping[str, str] = os.environ) -> str | None:
    if profile.token_env is None:
        return None
    token = environ.get(profile.token_env, "").strip()
    if not token:
        raise MirrorError(
            "TOKEN_MISSING",
            f"Environment variable is not set: {profile.token_env}",
            exit_code=2,
        )
    return token


# Provider API clients


def _replace_query_value(url: str, key: str, value: str) -> str:
    parsed = urlsplit(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query[key] = value
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))


def _within_api_prefix(url: str, api_url: str) -> bool:
    try:
        candidate = urlsplit(url)
        prefix = urlsplit(api_url)
    except ValueError:
        return False
    prefix_path = prefix.path.rstrip("/")
    return (
        candidate.scheme == prefix.scheme
        and candidate.netloc == prefix.netloc
        and (candidate.path == prefix_path or candidate.path.startswith(prefix_path + "/"))
    )


def _next_link(value: str | None) -> str | None:
    if not value:
        return None
    for section in value.split(","):
        parts = [part.strip() for part in section.split(";")]
        if len(parts) < 2 or 'rel="next"' not in parts[1:]:
            continue
        target = parts[0]
        if target.startswith("<") and target.endswith(">"):
            return target[1:-1]
    return None


class ProviderClient:
    def __init__(self, session: requests.Session | None = None) -> None:
        self.session = session or requests.Session()

    @staticmethod
    def _headers(profile: Profile, token: str | None) -> dict[str, str]:
        if profile.provider == "gitlab":
            headers = {"Accept": "application/json"}
            if token is not None:
                headers["PRIVATE-TOKEN"] = token
            return headers
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _request_json(
        self,
        profile: Profile,
        token: str | None,
        url: str,
        *,
        params: Mapping[str, str] | None,
        cancel: threading.Event,
        deadline: float,
    ) -> tuple[object, Mapping[str, str]]:
        retryable = {408, 429, 500, 502, 503, 504}
        for attempt in range(3):
            if cancel.is_set():
                raise MirrorError("INTERRUPTED", "Discovery interrupted", exit_code=130)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MirrorError("DISCOVERY_TIMEOUT", "Provider discovery timed out")
            try:
                response = self.session.get(
                    url,
                    params=params,
                    headers=self._headers(profile, token),
                    timeout=(min(5.0, remaining), min(30.0, remaining)),
                    verify=str(profile.ca_bundle) if profile.ca_bundle else True,
                    allow_redirects=False,
                )
            except requests.RequestException as exc:
                if attempt == 2:
                    raise MirrorError("PROVIDER_UNAVAILABLE", "Provider request failed") from exc
                if cancel.wait(min(0.05 * (2**attempt), max(0.0, remaining))):
                    raise MirrorError(
                        "INTERRUPTED",
                        "Discovery interrupted",
                        exit_code=130,
                    ) from exc
                continue
            rate_limited = (
                profile.provider == "github"
                and response.status_code == 403
                and response.headers.get("X-RateLimit-Remaining") == "0"
            )
            if (response.status_code in retryable or rate_limited) and attempt < 2:
                delay = self._retry_delay(response.headers, attempt, remaining)
                if cancel.wait(delay):
                    raise MirrorError("INTERRUPTED", "Discovery interrupted", exit_code=130)
                continue
            if not 200 <= response.status_code < 300:
                raise MirrorError(
                    "PROVIDER_HTTP_ERROR",
                    f"Provider returned HTTP {response.status_code}",
                )
            try:
                return response.json(), response.headers
            except requests.exceptions.JSONDecodeError as exc:
                raise MirrorError(
                    "PROVIDER_RESPONSE_INVALID",
                    "Provider returned invalid JSON",
                ) from exc
        raise MirrorError("PROVIDER_UNAVAILABLE", "Provider request failed")

    @staticmethod
    def _retry_delay(headers: Mapping[str, str], attempt: int, remaining: float) -> float:
        retry_after = headers.get("Retry-After", "").strip()
        delay: float | None = None
        if retry_after:
            try:
                delay = float(retry_after)
            except ValueError:
                try:
                    value = parsedate_to_datetime(retry_after)
                    if value.tzinfo is None:
                        value = value.replace(tzinfo=UTC)
                    delay = value.timestamp() - time.time()
                except (TypeError, ValueError, OverflowError):
                    delay = None
        if delay is None:
            reset = headers.get("X-RateLimit-Reset", "").strip()
            try:
                delay = float(reset) - time.time() if reset else None
            except ValueError:
                delay = None
        if delay is None:
            delay = 0.05 * (2**attempt)
        return max(0.0, min(delay, remaining))

    def discover_all(
        self,
        profile: Profile,
        token: str | None,
        cancel: threading.Event,
    ) -> list[Repo]:
        base = profile.api_url.rstrip("/")
        if profile.discovery_mode == "accessible" and profile.provider == "gitlab":
            url = f"{base}/projects"
            params: Mapping[str, str] | None = {
                "membership": "true",
                "order_by": "path",
                "sort": "asc",
                "per_page": "100",
                "page": "1",
            }
        elif profile.discovery_mode == "accessible":
            url = f"{base}/user/repos"
            params = {
                "affiliation": "owner,collaborator,organization_member",
                "sort": "full_name",
                "direction": "asc",
                "per_page": "100",
                "page": "1",
            }
        elif profile.provider == "github" and profile.discovery_mode == "public-user":
            url = f"{base}/users/{quote(profile.target or '', safe='')}/repos"
            params = {
                "type": "owner",
                "sort": "full_name",
                "direction": "asc",
                "per_page": "100",
                "page": "1",
            }
        elif profile.provider == "github":
            url = f"{base}/orgs/{quote(profile.target or '', safe='')}/repos"
            params = {
                "type": "all",
                "sort": "full_name",
                "direction": "asc",
                "per_page": "100",
                "page": "1",
            }
        elif profile.discovery_mode == "public-user":
            url = f"{base}/users/{quote(profile.target or '', safe='')}/projects"
            params = {
                "visibility": "public",
                "order_by": "path",
                "sort": "asc",
                "per_page": "100",
                "page": "1",
            }
        else:
            url = f"{base}/groups/{quote(profile.target or '', safe='')}/projects"
            params = {
                "visibility": "public",
                "include_subgroups": "true",
                "with_shared": "false",
                "order_by": "path",
                "sort": "asc",
                "per_page": "100",
                "page": "1",
            }
        deadline = time.monotonic() + profile.run_timeout_s
        seen_urls: set[str] = set()
        repositories: dict[str, Repo] = {}
        while url:
            effective_url = requests.Request("GET", url, params=params).prepare().url or url
            if effective_url in seen_urls:
                raise MirrorError("PAGINATION_LOOP", "Provider pagination repeated a page")
            seen_urls.add(effective_url)
            payload, headers = self._request_json(
                profile,
                token,
                url,
                params=params,
                cancel=cancel,
                deadline=deadline,
            )
            if not isinstance(payload, list):
                raise MirrorError("PROVIDER_RESPONSE_INVALID", "Repository page must be a list")
            for raw in payload:
                if profile.discovery_mode != "accessible" and not self._is_public_repo(
                    profile, raw
                ):
                    continue
                repo = self._normalize_repo(profile, raw)
                previous = repositories.get(repo.project_id)
                if previous is not None and previous != repo:
                    raise MirrorError("PROVIDER_RESPONSE_INVALID", "Conflicting project identity")
                repositories[repo.project_id] = repo
            params = None
            if profile.provider == "gitlab":
                next_page = headers.get("X-Next-Page", "").strip()
                url = _replace_query_value(effective_url, "page", next_page) if next_page else ""
            else:
                next_url = _next_link(headers.get("Link"))
                if next_url and not _within_api_prefix(next_url, profile.api_url):
                    raise MirrorError(
                        "PAGINATION_ESCAPE",
                        "Provider pagination left the API prefix",
                    )
                url = next_url or ""
        return sorted(
            repositories.values(),
            key=lambda repo: (repo.namespace.casefold(), repo.project_id),
        )

    @staticmethod
    def _is_public_repo(profile: Profile, raw: object) -> bool:
        if not isinstance(raw, Mapping):
            return False
        if profile.provider == "github":
            return raw.get("private") is False or raw.get("visibility") == "public"
        return raw.get("visibility") == "public"

    @staticmethod
    def _normalize_repo(profile: Profile, raw: object) -> Repo:
        if not isinstance(raw, dict):
            raise MirrorError("PROVIDER_RESPONSE_INVALID", "Repository entry must be an object")
        try:
            if profile.provider == "gitlab":
                return Repo(
                    project_id=str(raw["id"]),
                    namespace=str(raw["path_with_namespace"]),
                    ssh_url=str(raw["ssh_url_to_repo"]),
                    http_url=str(raw["http_url_to_repo"]) if raw.get("http_url_to_repo") else None,
                    default_branch=str(raw["default_branch"])
                    if raw.get("default_branch")
                    else None,
                    enabled=not bool(raw.get("archived", False)),
                )
            return Repo(
                project_id=str(raw["id"]),
                namespace=str(raw["full_name"]),
                ssh_url=str(raw["ssh_url"]),
                http_url=str(raw["clone_url"]) if raw.get("clone_url") else None,
                default_branch=str(raw["default_branch"]) if raw.get("default_branch") else None,
                enabled=not bool(raw.get("archived", False) or raw.get("disabled", False)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise MirrorError(
                "PROVIDER_RESPONSE_INVALID",
                "Repository entry has invalid fields",
            ) from exc

    def current_user(self, profile: Profile, token: str) -> dict[str, object]:
        cancel = threading.Event()
        payload, _headers = self._request_json(
            profile,
            token,
            f"{profile.api_url.rstrip('/')}/user",
            params=None,
            cancel=cancel,
            deadline=time.monotonic() + min(profile.run_timeout_s, 30.0),
        )
        if not isinstance(payload, dict):
            raise MirrorError("PROVIDER_RESPONSE_INVALID", "User response must be an object")
        return payload


def discover(profile: Profile, token: str | None, cancel: threading.Event) -> list[Repo]:
    try:
        return ProviderClient().discover_all(profile, token, cancel)
    except MirrorError as exc:
        if exc.exit_code == 130:
            raise
        safe_message = str(redact(exc.message, _redaction_secrets(profile, token)))
        raise MirrorError("DISCOVERY_FAILED", safe_message, exit_code=3) from exc


def discover_and_sync(profile: Profile, git_runner: object) -> list[Repo]:
    token = resolve_token(profile)
    repositories = discover(profile, token, threading.Event())
    for repo in repositories:
        if repo.enabled:
            git_runner.sync(repo)  # type: ignore[attr-defined]
    return repositories


# Managed storage


def _is_reparse_point(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(os.path, "isjunction", None)
    if is_junction is not None and is_junction(path):
        return True
    if os.name == "nt" and path.exists():
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
        return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    return False


def _validate_namespace(namespace: str) -> tuple[str, ...]:
    if not namespace or "\\" in namespace or namespace.startswith("/"):
        raise MirrorError("INVALID_REPOSITORY_PATH", "Repository namespace is invalid")
    parts = tuple(namespace.split("/"))
    for part in parts:
        normalized = unicodedata.normalize("NFC", part)
        stem = normalized.split(".", 1)[0].upper()
        if (
            not normalized
            or normalized in {".", ".."}
            or normalized.endswith((" ", "."))
            or stem in WINDOWS_RESERVED_NAMES
            or any(ord(character) < 32 for character in normalized)
            or ":" in normalized
        ):
            raise MirrorError("INVALID_REPOSITORY_PATH", "Repository namespace is invalid")
    return parts


def _relative_path(repo: Repo) -> str:
    return "/".join(_validate_namespace(repo.namespace))


def _normalized_path_key(relative_path: str) -> str:
    return "/".join(
        unicodedata.normalize("NFC", part).casefold() for part in relative_path.split("/")
    )


def _api_instance(profile: Profile) -> str:
    parsed = urlsplit(profile.api_url.rstrip("/"))
    return urlunsplit(
        (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), "", "")
    )


def repository_key(profile: Profile, repo: Repo) -> str:
    return f"{_api_instance(profile)}::{repo.project_id}"


def _origin_for(profile: Profile, repo: Repo) -> str:
    origin = repo.http_url if profile.transport == "https" else repo.ssh_url
    if not origin:
        raise MirrorError(
            "PROVIDER_RESPONSE_INVALID", "Repository has no URL for selected transport"
        )
    return origin


def _normalize_origin(origin: str) -> str:
    value = origin.strip().rstrip("/")
    if re.match(r"^[^/@\s]+@[^/:\s]+:.+$", value):
        user_host, path = value.split(":", 1)
        return f"{user_host.casefold()}:{path.removesuffix('.git')}"
    try:
        parsed = urlsplit(value)
    except ValueError:
        return value
    if parsed.scheme:
        host = (parsed.hostname or "").casefold()
        try:
            port_number = parsed.port
        except ValueError:
            return value
        port = f":{port_number}" if port_number else ""
        path = parsed.path.removesuffix(".git")
        return urlunsplit((parsed.scheme.casefold(), host + port, path, "", ""))
    try:
        return str(Path(value).expanduser().resolve(strict=False))
    except (OSError, RuntimeError):
        return value


def _validate_origin(origin: str, *, allow_local_transport: bool) -> None:
    if re.match(r"^[^/@\s]+@[^/:\s]+:[^\s]+$", origin):
        return
    try:
        parsed = urlsplit(origin)
        _port = parsed.port
    except ValueError as exc:
        raise MirrorError("ORIGIN_UNSAFE", "Repository origin is malformed") from exc
    if parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password:
        return
    if allow_local_transport and (
        parsed.scheme == "file" or (not parsed.scheme and Path(origin).is_absolute())
    ):
        return
    raise MirrorError("ORIGIN_UNSAFE", "Repository origin uses an unsupported transport")


def _manifest_entry(profile: Profile, repo: Repo) -> dict[str, object]:
    relative_path = _relative_path(repo)
    return {
        "instance": _api_instance(profile),
        "project_id": repo.project_id,
        "namespace": repo.namespace,
        "relative_path": relative_path,
        "origin": _origin_for(profile, repo),
    }


class ManagedRoot:
    def __init__(self, profile: Profile) -> None:
        self.profile = profile
        self.path = profile.root.expanduser().resolve(strict=False)
        self.marker_path = self.path / ROOT_MARKER_NAME
        self.manifest_path = self.path / MANIFEST_NAME
        self.lock_path = self.path / ROOT_LOCK_NAME
        self.manifest_lock = threading.Lock()

    def ensure(self) -> None:
        if self.profile.root.exists() and _is_reparse_point(self.profile.root):
            raise MirrorError("UNSAFE_ROOT", "Mirror root cannot be a link or reparse point")
        self.path.mkdir(parents=True, exist_ok=True)
        if _is_reparse_point(self.path) or not self.path.is_dir():
            raise MirrorError("UNSAFE_ROOT", "Mirror root must be a real directory")
        if self.marker_path.exists():
            marker = _read_json(self.marker_path)
            if marker.get("profile_id") != self.profile.profile_id:
                raise MirrorError(
                    "ROOT_OWNERSHIP_MISMATCH",
                    "Mirror root belongs to another profile",
                )
        else:
            _atomic_write_json(
                self.marker_path,
                {"schema_version": SCHEMA_VERSION, "profile_id": self.profile.profile_id},
            )
        if not self.manifest_path.exists():
            self.save_manifest(self.empty_manifest())

    def empty_manifest(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "profile_id": self.profile.profile_id,
            "instance": _api_instance(self.profile),
            "repositories": {},
        }

    def load_manifest(self) -> dict[str, object]:
        manifest = _read_json(self.manifest_path)
        if (
            manifest.get("schema_version") != SCHEMA_VERSION
            or manifest.get("profile_id") != self.profile.profile_id
            or manifest.get("instance") != _api_instance(self.profile)
            or not isinstance(manifest.get("repositories"), dict)
        ):
            raise MirrorError("MANIFEST_INVALID", "Managed-root manifest is invalid")
        return manifest

    def save_manifest(self, manifest: Mapping[str, object]) -> None:
        _atomic_write_json(self.manifest_path, dict(manifest))

    @contextmanager
    def lock(self):
        lock = FileLock(str(self.lock_path))
        try:
            lock.acquire(timeout=0)
        except Timeout as exc:
            raise MirrorError("ROOT_LOCKED", "Mirror root is already being synchronized") from exc
        try:
            yield
        finally:
            lock.release()

    def path_for(self, repo: Repo) -> Path:
        return self.path.joinpath(*_validate_namespace(repo.namespace))


def build_sync_plan(
    profile: Profile,
    discovered: Sequence[Repo],
    manifest: Mapping[str, object],
    selection: Sequence[str] = (),
) -> list[PlannedRepository]:
    registered_raw = manifest.get("repositories", {})
    if not isinstance(registered_raw, Mapping):
        raise MirrorError("MANIFEST_INVALID", "Manifest repositories must be an object")
    registered = {str(key): value for key, value in registered_raw.items()}
    selectors = set(selection)
    discovered_names = {repo.namespace for repo in discovered}
    unknown = sorted(selectors - discovered_names)
    if unknown:
        raise MirrorError(
            "REPO_NOT_FOUND",
            f"Unknown repository selector: {unknown[0]}",
            exit_code=2,
        )
    selected = [repo for repo in discovered if not selectors or repo.namespace in selectors]
    path_owners: dict[str, str] = {}
    plan: list[PlannedRepository] = []
    discovered_keys: set[str] = set()
    for repo in selected:
        relative_path = _relative_path(repo)
        path_key = _normalized_path_key(relative_path)
        owner = path_owners.setdefault(path_key, repo.project_id)
        if owner != repo.project_id:
            raise MirrorError("PATH_COLLISION", "Repositories resolve to the same local path")
        key = repository_key(profile, repo)
        discovered_keys.add(key)
        entry = registered.get(key)
        if not repo.enabled:
            plan.append(PlannedRepository(repo, relative_path, "skip", "REPO_DISABLED"))
            continue
        if entry is not None:
            if not isinstance(entry, Mapping):
                raise MirrorError("MANIFEST_INVALID", "Repository manifest entry is invalid")
            changed = (
                str(entry.get("namespace")) != repo.namespace
                or str(entry.get("relative_path")) != relative_path
                or _normalize_origin(str(entry.get("origin", "")))
                != _normalize_origin(_origin_for(profile, repo))
            )
            if changed:
                plan.append(PlannedRepository(repo, relative_path, "skip", "REMOTE_PATH_CHANGED"))
                continue
        plan.append(PlannedRepository(repo, relative_path, "sync"))

    if not selectors:
        for key, entry in registered.items():
            if key in discovered_keys:
                continue
            if not isinstance(entry, Mapping):
                raise MirrorError("MANIFEST_INVALID", "Repository manifest entry is invalid")
            missing = Repo(
                project_id=str(entry.get("project_id", "")),
                namespace=str(entry.get("namespace", "")),
                ssh_url=str(entry.get("origin", "")),
                http_url=None,
                default_branch=None,
                enabled=False,
            )
            plan.append(
                PlannedRepository(
                    missing,
                    str(entry.get("relative_path", "")),
                    "skip",
                    "SOURCE_NOT_LISTED",
                )
            )
    return sorted(plan, key=lambda item: (item.relative_path.casefold(), item.repo.project_id))


# Process and Git operations


SAFE_GIT_CONFIG = (
    "-c",
    f"core.hooksPath={os.devnull}",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "submodule.recurse=false",
    "-c",
    "fetch.recurseSubmodules=false",
    "-c",
    "filter.lfs.smudge=",
    "-c",
    "filter.lfs.process=",
    "-c",
    "filter.lfs.required=false",
)
DESTRUCTIVE_GIT_COMMANDS = frozenset({"checkout", "clean", "reset", "update-ref"})


class ProcessRunner:
    def __init__(self, profiles: Sequence[Profile]) -> None:
        self.profiles = tuple(profiles)
        self.calls: list[list[str]] = []
        self.recorded_destructive_calls: list[list[str]] = []
        self._record_lock = threading.Lock()

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        timeout: float | None = None,
        destructive: bool = False,
        environment: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = [str(item) for item in argv]
        if not command or any("\0" in item for item in command):
            raise MirrorError("PROCESS_INVALID", "Invalid process arguments")
        with self._record_lock:
            self.calls.append(command)
            if destructive:
                self.recorded_destructive_calls.append(command)
        ssh_key = self.profiles[0].ssh_key if len(self.profiles) == 1 else None
        child_environment = dict(environment or git_environment(self.profiles, ssh_key=ssh_key))
        secrets = [
            secret
            for profile in self.profiles
            for secret in _redaction_secrets(
                profile,
                os.environ.get(profile.token_env, "").strip() if profile.token_env else None,
            )
        ]
        try:
            completed = subprocess.run(
                command,
                cwd=cwd,
                env=child_environment,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise MirrorError("GIT_TIMEOUT", "Git command timed out") from exc
        except OSError as exc:
            raise MirrorError("GIT_UNAVAILABLE", "Cannot start Git") from exc
        if completed.returncode != 0:
            detail = str(redact((completed.stderr or completed.stdout).strip(), secrets))
            raise MirrorError("GIT_FAILED", detail[:1000] or "Git command failed")
        return completed


def repo_git_argv(paths: RepoPaths, *args: str) -> list[str]:
    return [
        "git",
        f"--git-dir={paths.git_dir}",
        f"--work-tree={paths.work_tree}",
        *SAFE_GIT_CONFIG,
        *args,
    ]


def _git(
    runner: ProcessRunner,
    paths: RepoPaths,
    *args: str,
    timeout: float,
    destructive: bool = False,
    environment: Mapping[str, str] | None = None,
) -> str:
    return runner.run(
        repo_git_argv(paths, *args),
        timeout=timeout,
        destructive=destructive,
        environment=environment,
    ).stdout.strip()


def _validate_path_chain(root: Path, target: Path) -> None:
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise MirrorError("PATH_ESCAPE", "Repository path leaves the managed root") from exc
    current = root
    if _is_reparse_point(current):
        raise MirrorError("PATH_ESCAPE", "Managed root is a link or reparse point")
    for part in relative.parts:
        current = current / part
        if current.exists() or current.is_symlink():
            if _is_reparse_point(current):
                raise MirrorError("PATH_ESCAPE", "Repository path crosses a link or reparse point")
    resolved_root = root.resolve(strict=True)
    resolved_target = target.resolve(strict=False)
    if resolved_target != resolved_root and resolved_root not in resolved_target.parents:
        raise MirrorError("PATH_ESCAPE", "Repository path leaves the managed root")


def _parse_local_config(output: str) -> dict[str, list[str]]:
    chunks = output.split("\0")
    parsed: dict[str, list[str]] = {}
    for index in range(0, len(chunks) - 1, 2):
        key_value = chunks[index + 1]
        key, separator, value = key_value.partition("\n")
        if not separator:
            value = ""
        parsed.setdefault(key.casefold(), []).append(value)
    return parsed


def _inspect_checkout(
    profile: Profile,
    repo: Repo,
    paths: RepoPaths,
    runner: ProcessRunner,
    *,
    allow_local_transport: bool,
    environment: Mapping[str, str] | None = None,
) -> None:
    if not paths.work_tree.is_dir() or _is_reparse_point(paths.work_tree):
        raise MirrorError("UNSAFE_REPOSITORY", "Checkout is not a real directory")
    if not paths.git_dir.is_dir() or _is_reparse_point(paths.git_dir):
        raise MirrorError("UNSAFE_REPOSITORY", "Checkout .git must be an owned directory")
    config_path = paths.git_dir / "config"
    if not config_path.is_file() or _is_reparse_point(config_path):
        raise MirrorError("UNSAFE_REPOSITORY", "Checkout config must be an owned file")
    config_output = runner.run(
        [
            "git",
            f"--git-dir={paths.git_dir}",
            "config",
            "--local",
            "--null",
            "--list",
            "--show-origin",
            "--no-includes",
        ],
        timeout=profile.git_timeout_s,
        environment=environment,
    ).stdout
    config = _parse_local_config(config_output)
    dangerous = []
    for key in config:
        if (
            key == "core.worktree"
            or key in {"core.hookspath", "core.fsmonitor", "extensions.worktreeconfig"}
            or key.startswith("include.")
            or key.startswith("includeif.")
            or (key.startswith("url.") and key.endswith((".insteadof", ".pushinsteadof")))
            or (key.startswith("filter.") and key.endswith((".clean", ".smudge", ".process")))
            or key == "extensions.partialclone"
            or (key.startswith("remote.") and key.endswith(".promisor"))
        ):
            dangerous.append(key)
    if dangerous:
        raise MirrorError("UNSAFE_REPOSITORY", f"Unsafe local Git config: {dangerous[0]}")
    origins = config.get("remote.origin.url", [])
    expected_origin = _origin_for(profile, repo)
    _validate_origin(expected_origin, allow_local_transport=allow_local_transport)
    if len(origins) != 1 or _normalize_origin(origins[0]) != _normalize_origin(expected_origin):
        raise MirrorError("UNSAFE_REPOSITORY", "Checkout origin does not match the provider")
    top_level = _git(
        runner,
        paths,
        "rev-parse",
        "--show-toplevel",
        timeout=profile.git_timeout_s,
        environment=environment,
    )
    absolute_git_dir = _git(
        runner,
        paths,
        "rev-parse",
        "--absolute-git-dir",
        timeout=profile.git_timeout_s,
        environment=environment,
    )
    common_dir = _git(
        runner,
        paths,
        "rev-parse",
        "--path-format=absolute",
        "--git-common-dir",
        timeout=profile.git_timeout_s,
        environment=environment,
    )
    if Path(top_level).resolve() != paths.work_tree.resolve():
        raise MirrorError("UNSAFE_REPOSITORY", "Git top-level path does not match the checkout")
    if Path(absolute_git_dir).resolve() != paths.git_dir.resolve():
        raise MirrorError("UNSAFE_REPOSITORY", "Git directory does not match the checkout")
    if Path(common_dir).resolve() != paths.git_dir.resolve():
        raise MirrorError("UNSAFE_REPOSITORY", "Linked worktrees are not supported")
    unsupported_paths = (
        paths.git_dir / "shallow",
        paths.git_dir / "info" / "sparse-checkout",
        paths.git_dir / "modules",
        paths.git_dir / "worktrees",
        paths.git_dir / "objects" / "info" / "alternates",
        paths.git_dir / "MERGE_HEAD",
        paths.git_dir / "CHERRY_PICK_HEAD",
        paths.git_dir / "REVERT_HEAD",
        paths.git_dir / "BISECT_LOG",
        paths.git_dir / "rebase-apply",
        paths.git_dir / "rebase-merge",
    )
    if any(path.exists() for path in unsupported_paths):
        raise MirrorError("UNSAFE_REPOSITORY", "Checkout uses an unsupported Git state")
    if any(value.lower() == "true" for value in config.get("core.sparsecheckout", [])):
        raise MirrorError("UNSAFE_REPOSITORY", "Sparse checkouts are not supported")


def validate_repository_boundary(
    profile: Profile,
    repo: Repo,
    managed_root: ManagedRoot,
    runner: ProcessRunner,
    *,
    allow_local_transport: bool = False,
    environment: Mapping[str, str] | None = None,
) -> RepoPaths:
    managed_root.ensure()
    manifest = managed_root.load_manifest()
    registered = manifest["repositories"]
    assert isinstance(registered, dict)
    entry = registered.get(repository_key(profile, repo))
    if not isinstance(entry, Mapping):
        raise MirrorError("UNSAFE_REPOSITORY", "Repository is not registered in the manifest")
    expected_entry = _manifest_entry(profile, repo)
    for field in ("instance", "project_id", "namespace", "relative_path"):
        if str(entry.get(field)) != str(expected_entry[field]):
            raise MirrorError("UNSAFE_REPOSITORY", "Repository identity does not match manifest")
    if _normalize_origin(str(entry.get("origin", ""))) != _normalize_origin(
        _origin_for(profile, repo)
    ):
        raise MirrorError("UNSAFE_REPOSITORY", "Repository origin does not match manifest")
    work_tree = managed_root.path_for(repo)
    _validate_path_chain(managed_root.path, work_tree)
    paths = RepoPaths(managed_root.path, work_tree, work_tree / ".git")
    _inspect_checkout(
        profile,
        repo,
        paths,
        runner,
        allow_local_transport=allow_local_transport,
        environment=environment,
    )
    return paths


def _ref_map(
    runner: ProcessRunner,
    paths: RepoPaths,
    timeout: float,
    environment: Mapping[str, str] | None,
) -> dict[str, str]:
    output = _git(
        runner,
        paths,
        "for-each-ref",
        "--format=%(refname) %(objectname)",
        "refs/heads",
        "refs/remotes/origin",
        "refs/tags",
        timeout=timeout,
        environment=environment,
    )
    refs: dict[str, str] = {}
    for line in output.splitlines():
        ref, separator, object_id = line.partition(" ")
        if separator:
            refs[ref] = object_id
    return refs


def _synchronize_checkout(
    profile: Profile,
    repo: Repo,
    paths: RepoPaths,
    runner: ProcessRunner,
    cancel: threading.Event,
    *,
    is_new: bool,
    allow_local_transport: bool,
    environment: Mapping[str, str],
) -> str:
    _inspect_checkout(
        profile,
        repo,
        paths,
        runner,
        allow_local_transport=allow_local_transport,
        environment=environment,
    )
    before_refs = _ref_map(runner, paths, profile.git_timeout_s, environment)
    before_status = _git(
        runner,
        paths,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        timeout=profile.git_timeout_s,
        environment=environment,
    )
    _git(
        runner,
        paths,
        "fetch",
        "--prune",
        "--prune-tags",
        "--force",
        "--no-recurse-submodules",
        "origin",
        "+refs/heads/*:refs/remotes/origin/*",
        "+refs/tags/*:refs/tags/*",
        timeout=profile.git_timeout_s,
        environment=environment,
    )
    if cancel.is_set():
        raise MirrorError("INTERRUPTED", "Synchronization interrupted", exit_code=130)
    _inspect_checkout(
        profile,
        repo,
        paths,
        runner,
        allow_local_transport=allow_local_transport,
        environment=environment,
    )
    fetched_refs = _ref_map(runner, paths, profile.git_timeout_s, environment)
    remote_prefix = "refs/remotes/origin/"
    remote_branches = {
        ref.removeprefix(remote_prefix): object_id
        for ref, object_id in fetched_refs.items()
        if ref.startswith(remote_prefix) and ref != "refs/remotes/origin/HEAD"
    }
    if not remote_branches:
        raise MirrorError("GIT_FAILED", "Remote has no branches")
    target_branch = (
        repo.default_branch if repo.default_branch in remote_branches else min(remote_branches)
    )
    target_oid = remote_branches[target_branch]
    _git(
        runner,
        paths,
        "checkout",
        "--detach",
        "--force",
        target_oid,
        timeout=profile.git_timeout_s,
        destructive=True,
        environment=environment,
    )
    local_branches = {
        ref.removeprefix("refs/heads/") for ref in fetched_refs if ref.startswith("refs/heads/")
    }
    for branch, object_id in sorted(remote_branches.items()):
        _git(
            runner,
            paths,
            "update-ref",
            f"refs/heads/{branch}",
            object_id,
            timeout=profile.git_timeout_s,
            destructive=True,
            environment=environment,
        )
    for branch in sorted(local_branches - remote_branches.keys()):
        _git(
            runner,
            paths,
            "update-ref",
            "-d",
            f"refs/heads/{branch}",
            timeout=profile.git_timeout_s,
            destructive=True,
            environment=environment,
        )
    _git(
        runner,
        paths,
        "checkout",
        "-B",
        target_branch,
        f"refs/remotes/origin/{target_branch}",
        timeout=profile.git_timeout_s,
        destructive=True,
        environment=environment,
    )
    _git(
        runner,
        paths,
        "reset",
        "--hard",
        f"refs/remotes/origin/{target_branch}",
        timeout=profile.git_timeout_s,
        destructive=True,
        environment=environment,
    )
    _git(
        runner,
        paths,
        "clean",
        "-ffdx",
        timeout=profile.git_timeout_s,
        destructive=True,
        environment=environment,
    )
    _git(
        runner,
        paths,
        "branch",
        f"--set-upstream-to=origin/{target_branch}",
        target_branch,
        timeout=profile.git_timeout_s,
        environment=environment,
    )
    after_refs = _ref_map(runner, paths, profile.git_timeout_s, environment)
    if is_new:
        return "cloned"
    return "updated" if before_refs != after_refs or before_status else "unchanged"


def _safe_remove_staging(root: Path, stage: Path) -> None:
    try:
        stage.relative_to(root)
    except ValueError:
        return
    if ".staging-" not in stage.name:
        return
    if stage.is_symlink():
        stage.unlink(missing_ok=True)
    elif _is_reparse_point(stage):
        os.rmdir(stage)
    elif stage.exists():
        shutil.rmtree(stage)


def sync_repository(
    profile: Profile,
    repo: Repo,
    managed_root: ManagedRoot,
    runner: ProcessRunner,
    cancel: threading.Event,
    *,
    allow_local_transport: bool = False,
) -> WorkResult:
    final_path = managed_root.path_for(repo)
    stage: Path | None = None
    try:
        if cancel.is_set():
            raise MirrorError("INTERRUPTED", "Synchronization interrupted", exit_code=130)
        environment = _repository_git_environment(profile, repo)
        key = repository_key(profile, repo)
        with managed_root.manifest_lock:
            manifest = managed_root.load_manifest()
            repositories = manifest["repositories"]
            assert isinstance(repositories, dict)
            is_registered = key in repositories
        if final_path.exists() and not is_registered:
            return WorkResult(
                profile.profile_id,
                repo.project_id,
                repo.namespace,
                str(final_path),
                "skipped",
                "PATH_CONFLICT",
            )
        if is_registered:
            paths = validate_repository_boundary(
                profile,
                repo,
                managed_root,
                runner,
                allow_local_transport=allow_local_transport,
                environment=environment,
            )
            status = _synchronize_checkout(
                profile,
                repo,
                paths,
                runner,
                cancel,
                is_new=False,
                allow_local_transport=allow_local_transport,
                environment=environment,
            )
            return WorkResult(
                profile.profile_id,
                repo.project_id,
                repo.namespace,
                str(final_path),
                status,
            )

        _validate_origin(_origin_for(profile, repo), allow_local_transport=allow_local_transport)
        _validate_path_chain(managed_root.path, final_path.parent)
        final_path.parent.mkdir(parents=True, exist_ok=True)
        _validate_path_chain(managed_root.path, final_path.parent)
        stage = final_path.parent / f".{final_path.name}.staging-{uuid.uuid4().hex}"
        clone_command = [
            "git",
            *SAFE_GIT_CONFIG,
            "clone",
            "--no-checkout",
            "--origin",
            "origin",
            "--",
            _origin_for(profile, repo),
            str(stage),
        ]
        runner.run(
            clone_command,
            cwd=managed_root.path,
            timeout=profile.git_timeout_s,
            environment=environment,
        )
        if cancel.is_set():
            raise MirrorError("INTERRUPTED", "Synchronization interrupted", exit_code=130)
        stage_paths = RepoPaths(managed_root.path, stage, stage / ".git")
        status = _synchronize_checkout(
            profile,
            repo,
            stage_paths,
            runner,
            cancel,
            is_new=True,
            allow_local_transport=allow_local_transport,
            environment=environment,
        )
        with managed_root.manifest_lock:
            if final_path.exists():
                raise MirrorError("PATH_CONFLICT", "Repository path appeared during clone")
            manifest = managed_root.load_manifest()
            repositories = manifest["repositories"]
            assert isinstance(repositories, dict)
            os.replace(stage, final_path)
            stage = None
            repositories[key] = _manifest_entry(profile, repo)
            managed_root.save_manifest(manifest)
        return WorkResult(
            profile.profile_id,
            repo.project_id,
            repo.namespace,
            str(final_path),
            status,
        )
    except MirrorError as exc:
        if exc.exit_code == 130:
            reason = "INTERRUPTED"
        elif exc.code in {
            "PATH_ESCAPE",
            "UNSAFE_REPOSITORY",
            "ORIGIN_UNSAFE",
            "ROOT_OWNERSHIP_MISMATCH",
        }:
            reason = "UNSAFE_REPOSITORY"
        else:
            reason = exc.code
        status = (
            "skipped"
            if reason in {"INTERRUPTED", "UNSAFE_REPOSITORY", "PATH_CONFLICT"}
            else "error"
        )
        return WorkResult(
            profile.profile_id,
            repo.project_id,
            repo.namespace,
            str(final_path),
            status,
            reason,
            exc.message,
        )
    finally:
        if stage is not None:
            _safe_remove_staging(managed_root.path, stage)


def run_probe(argv: Sequence[str]) -> str | None:
    try:
        completed = subprocess.run(
            list(argv),
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = (completed.stdout or completed.stderr).strip()
    return output.splitlines()[0] if output else None


def probe_tool_version(name: str) -> str | None:
    argv = ["ssh", "-V"] if name == "ssh" else [name, "--version"]
    return run_probe(argv)


# Reports


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _bounded(value: object, *, string_limit: int = 2000) -> object:
    if isinstance(value, str):
        return value if len(value) <= string_limit else value[:string_limit] + "…"
    if isinstance(value, Mapping):
        return {
            str(key): _bounded(item, string_limit=string_limit)
            for key, item in list(value.items())[:100]
        }
    if isinstance(value, (list, tuple)):
        return [_bounded(item, string_limit=string_limit) for item in value[:100]]
    return value


def _work_result_dict(result: WorkResult) -> dict[str, object]:
    return {
        "profile_id": result.profile_id,
        "project_id": result.project_id,
        "namespace": result.namespace,
        "path": result.path,
        "status": result.status,
        "reason": result.reason,
        "message": result.message,
    }


def _repository_counts(repositories: Sequence[Mapping[str, object]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in repositories:
        status = str(item.get("status", "unknown"))
        counts[status] = counts.get(status, 0) + 1
    return dict(sorted(counts.items()))


class ReportStore:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir.expanduser().resolve(strict=False)
        self.run_id: str | None = None
        self.run_dir: Path | None = None
        self.report_path: Path | None = None
        self.events_path: Path | None = None

    def start(self, profiles: Sequence[Profile]) -> dict[str, object]:
        self.run_id = str(uuid.uuid4())
        self.run_dir = self.state_dir / "runs" / self.run_id
        self.run_dir.mkdir(parents=True, exist_ok=False)
        self.report_path = self.run_dir / "report.json"
        self.events_path = self.run_dir / "events.jsonl"
        self.events_path.touch(mode=0o600)
        report: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "state": "incomplete",
            "started_at": _now_iso(),
            "finished_at": None,
            "tool": {
                "name": TOOL_NAME,
                "version": TOOL_VERSION,
                "python": sys.version.split()[0],
                "uv": probe_tool_version("uv"),
                "git": probe_tool_version("git"),
                "openssh": probe_tool_version("ssh"),
            },
            "capabilities": dict(CAPABILITIES),
            "profiles": [
                {
                    "profile_id": profile.profile_id,
                    "name": profile.name,
                    "provider": profile.provider,
                    "state": "pending",
                }
                for profile in profiles
            ],
            "repositories": [],
            "counts": {},
        }
        _atomic_write_json(self.report_path, report)
        return report

    def event(self, event: Mapping[str, object], secrets: Sequence[str]) -> None:
        if self.events_path is None:
            raise RuntimeError("ReportStore.start() must be called first")
        safe = _bounded(redact(dict(event), secrets))
        line = json.dumps(safe, ensure_ascii=False, separators=(",", ":"))
        with self.events_path.open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def finish(self, report: Mapping[str, object], secrets: Sequence[str]) -> Path:
        if self.report_path is None:
            raise RuntimeError("ReportStore.start() must be called first")
        final = dict(report)
        final["finished_at"] = final.get("finished_at") or _now_iso()
        repositories = final.get("repositories", [])
        if isinstance(repositories, list):
            repositories.sort(
                key=lambda item: (
                    str(item.get("namespace", "")) if isinstance(item, Mapping) else "",
                    str(item.get("project_id", "")) if isinstance(item, Mapping) else "",
                )
            )
            final["counts"] = _repository_counts(
                [item for item in repositories if isinstance(item, Mapping)]
            )
        safe = _bounded(redact(final, secrets))
        _atomic_write_json(self.report_path, safe)
        return self.report_path.resolve()


def build_summary(report: Mapping[str, object], report_path: Path) -> dict[str, object]:
    repositories = report.get("repositories", [])
    repository_items = (
        [item for item in repositories if isinstance(item, Mapping)]
        if isinstance(repositories, list)
        else []
    )
    profiles = report.get("profiles", [])
    profile_items = (
        [item for item in profiles if isinstance(item, Mapping)]
        if isinstance(profiles, list)
        else []
    )
    problems: list[dict[str, object]] = []
    for item in repository_items:
        if item.get("status") not in SUCCESS_STATUSES:
            problems.append(
                {
                    "profile_id": item.get("profile_id"),
                    "namespace": item.get("namespace"),
                    "status": item.get("status"),
                    "reason": item.get("reason"),
                    "message": _bounded(item.get("message"), string_limit=500),
                }
            )
    for item in profile_items:
        if item.get("state") == "failed":
            problems.append(
                {
                    "profile_id": item.get("profile_id"),
                    "namespace": None,
                    "status": "error",
                    "reason": item.get("reason"),
                    "message": _bounded(item.get("message"), string_limit=500),
                }
            )
    problems.sort(
        key=lambda item: (
            str(item.get("profile_id", "")),
            str(item.get("namespace", "")),
        )
    )
    return {
        "run_id": report.get("run_id"),
        "state": report.get("state"),
        "counts": report.get("counts", {}),
        "requires_attention": report.get("state") != "completed",
        "report_path": str(report_path.resolve()),
        "problems": problems[:20],
        "problems_truncated": len(problems) > 20,
        "problems_total": len(problems),
    }


def compute_exit_code(report: Mapping[str, object]) -> int:
    state = report.get("state")
    if state == "completed":
        return 0
    if state == "partial":
        return 1
    if state == "interrupted":
        return 130
    return 3


def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MirrorError("REPORT_INVALID", f"Cannot read report: {path.name}") from exc
    if not isinstance(raw, dict):
        raise MirrorError("REPORT_INVALID", f"Report is not an object: {path.name}")
    return raw


def _select_report(state_dir: Path, run_id: str | None) -> dict[str, Any]:
    runs_dir = state_dir / "runs"
    if run_id is not None:
        try:
            normalized = str(uuid.UUID(run_id))
        except ValueError as exc:
            raise MirrorError("INVALID_RUN_ID", "Run id must be a UUID", exit_code=2) from exc
        path = runs_dir / normalized / "report.json"
        if not path.is_file():
            raise MirrorError("REPORT_NOT_FOUND", f"Unknown run: {normalized}", exit_code=2)
        return _read_json(path)

    candidates: list[tuple[str, str, Path]] = []
    if runs_dir.is_dir():
        for path in runs_dir.glob("*/report.json"):
            report = _read_json(path)
            candidates.append(
                (
                    str(report.get("started_at", "")),
                    str(report.get("run_id", path.parent.name)),
                    path,
                )
            )
    if not candidates:
        raise MirrorError("REPORT_NOT_FOUND", "No reports found", exit_code=2)
    return _read_json(max(candidates)[2])


def _validate_pagination(offset: int, limit: int) -> None:
    if offset < 0:
        raise MirrorError("INVALID_OFFSET", "Offset must be zero or greater", exit_code=2)
    if not 1 <= limit <= MAX_PAGE_LIMIT:
        raise MirrorError(
            "INVALID_LIMIT",
            f"Limit must be between 1 and {MAX_PAGE_LIMIT}",
            exit_code=2,
        )


def paginate(items: Sequence[dict[str, Any]], offset: int, limit: int) -> dict[str, Any]:
    _validate_pagination(offset, limit)
    return {
        "offset": offset,
        "limit": limit,
        "total": len(items),
        "items": list(items[offset : offset + limit]),
    }


def report_view(
    state_dir: Path,
    *,
    run_id: str | None,
    errors_only: bool,
    offset: int,
    limit: int,
) -> dict[str, Any]:
    report = _select_report(state_dir, run_id)
    repositories = report.get("repositories", [])
    if not isinstance(repositories, list):
        raise MirrorError("REPORT_INVALID", "Report repositories must be a list")
    items = [item for item in repositories if isinstance(item, dict)]
    if errors_only:
        items = [item for item in items if item.get("status") not in SUCCESS_STATUSES]
    items.sort(key=lambda item: (str(item.get("namespace", "")), str(item.get("project_id", ""))))
    page = paginate(items, offset, limit)
    return {"run_id": report.get("run_id"), **page}


# Synchronization service


def _skip_result(profile: Profile, item: PlannedRepository) -> WorkResult:
    return WorkResult(
        profile.profile_id,
        item.repo.project_id,
        item.repo.namespace,
        str(profile.root / Path(item.relative_path)) if item.relative_path else None,
        "skipped",
        item.reason,
    )


def sync_repositories(
    profile: Profile,
    discovered: Sequence[Repo],
    *,
    selection: Sequence[str] = (),
    runner: ProcessRunner | None = None,
    cancel: threading.Event | None = None,
    allow_local_transport: bool = False,
) -> list[WorkResult]:
    managed_root = ManagedRoot(profile)
    managed_root.ensure()
    process_runner = runner or ProcessRunner([profile])
    cancellation = cancel or threading.Event()
    with managed_root.lock():
        managed_root.ensure()
        manifest = managed_root.load_manifest()
        plan = build_sync_plan(profile, discovered, manifest, selection)
        sync_items = [item for item in plan if item.action == "sync"]

        def run_item(item: PlannedRepository) -> WorkResult:
            return sync_repository(
                profile,
                item.repo,
                managed_root,
                process_runner,
                cancellation,
                allow_local_transport=allow_local_transport,
            )

        if profile.jobs == 1 or len(sync_items) < 2:
            synchronized = [run_item(item) for item in sync_items]
        else:
            with ThreadPoolExecutor(max_workers=profile.jobs) as executor:
                synchronized = list(executor.map(run_item, sync_items))
        synchronized_iter = iter(synchronized)
        return [
            _skip_result(profile, item) if item.action == "skip" else next(synchronized_iter)
            for item in plan
        ]


def sync_profiles(
    profiles: Sequence[Profile],
    state_dir: Path,
    *,
    repo_selection: Sequence[str] = (),
    cancel: threading.Event | None = None,
    allow_local_transport: bool = False,
) -> tuple[dict[str, object], int]:
    cancellation = cancel or threading.Event()
    store = ReportStore(state_dir)
    report = store.start(profiles)
    profile_rows = report["profiles"]
    repository_rows = report["repositories"]
    assert isinstance(profile_rows, list)
    assert isinstance(repository_rows, list)
    secrets: list[str] = []
    discovery_successes = 0
    interrupted = False
    for profile, profile_row in zip(profiles, profile_rows, strict=True):
        assert isinstance(profile_row, dict)
        if cancellation.is_set():
            interrupted = True
            profile_row["state"] = "not_started"
            profile_row["reason"] = "INTERRUPTED"
            break
        try:
            token = resolve_token(profile)
            secrets.extend(_redaction_secrets(profile, token))
            store.event(
                {
                    "at": _now_iso(),
                    "event": "discovery_started",
                    "profile_id": profile.profile_id,
                },
                secrets,
            )
            repositories = discover(profile, token, cancellation)
            discovery_successes += 1
            profile_row["state"] = "discovered"
            profile_row["repository_count"] = len(repositories)
            store.event(
                {
                    "at": _now_iso(),
                    "event": "discovery_completed",
                    "profile_id": profile.profile_id,
                    "repository_count": len(repositories),
                },
                secrets,
            )
            results = sync_repositories(
                profile,
                repositories,
                selection=repo_selection,
                cancel=cancellation,
                allow_local_transport=allow_local_transport,
            )
            for result in results:
                row = _work_result_dict(result)
                repository_rows.append(row)
                store.event(
                    {"at": _now_iso(), "event": "repository_result", **row},
                    secrets,
                )
                if result.reason == "INTERRUPTED":
                    interrupted = True
            profile_row["state"] = "interrupted" if interrupted else "completed"
            if interrupted:
                break
        except MirrorError as exc:
            if exc.exit_code == 130:
                interrupted = True
                profile_row["state"] = "interrupted"
            else:
                profile_row["state"] = "failed"
            profile_row["reason"] = exc.code
            profile_row["message"] = exc.message
            store.event(
                {
                    "at": _now_iso(),
                    "event": "profile_failed",
                    "profile_id": profile.profile_id,
                    "reason": exc.code,
                    "message": exc.message,
                },
                secrets,
            )
            if interrupted:
                break

    repository_mappings = [item for item in repository_rows if isinstance(item, Mapping)]
    report["counts"] = _repository_counts(repository_mappings)
    attention = any(item.get("status") not in SUCCESS_STATUSES for item in repository_mappings)
    failed_profiles = any(
        isinstance(item, Mapping) and item.get("state") == "failed" for item in profile_rows
    )
    if interrupted:
        report["state"] = "interrupted"
    elif discovery_successes == 0 and profiles:
        report["state"] = "incomplete"
    elif attention or failed_profiles:
        report["state"] = "partial"
    else:
        report["state"] = "completed"
    report_path = store.finish(report, secrets)
    final_report = _read_json(report_path)
    summary = build_summary(final_report, report_path)
    return summary, compute_exit_code(final_report)


# Diagnostics


def doctor(
    profile: Profile,
    *,
    client: ProviderClient | None = None,
    runner: ProcessRunner | None = None,
) -> dict[str, object]:
    validate_profile(profile)
    token = resolve_token(profile)
    provider_client = client or ProviderClient()
    process_runner = runner or ProcessRunner([profile])
    result: dict[str, object] = {
        "profile_id": profile.profile_id,
        "profile": profile.name,
        "provider": profile.provider,
        "ok": False,
        "tools": {
            "uv": probe_tool_version("uv"),
            "git": probe_tool_version("git"),
        },
        "discovery_mode": profile.discovery_mode,
        "target": profile.target,
        "transport": profile.transport,
        "authentication": "token" if token is not None else "anonymous",
        "tls": {
            "verification": "custom-ca" if profile.ca_bundle else "system-default",
            "proxy_from_environment": bool(requests.utils.get_environ_proxies(profile.api_url)),
        },
    }
    if profile.transport == "ssh":
        result["tools"]["ssh"] = probe_tool_version("ssh")  # type: ignore[index]
    managed_root = ManagedRoot(profile)
    if managed_root.marker_path.exists() or managed_root.manifest_path.exists():
        try:
            managed_root.load_manifest()
            result["manifest"] = "readable"
        except MirrorError as exc:
            result["manifest"] = "invalid"
            result["error"] = {"code": exc.code, "message": exc.message}
            return result
    else:
        result["manifest"] = "not_initialized"
    try:
        identity = (
            provider_client.current_user(profile, token)
            if profile.discovery_mode == "accessible" and token is not None
            else None
        )
        repositories = provider_client.discover_all(profile, token, threading.Event())
        result["api"] = {
            "identity_available": bool(identity),
            "repository_count": len(repositories),
        }
        probe_repo = next((repo for repo in repositories if repo.enabled), None)
        if probe_repo is not None:
            origin = _origin_for(profile, probe_repo)
            _validate_origin(origin, allow_local_transport=False)
            process_runner.run(
                [
                    "git",
                    *SAFE_GIT_CONFIG,
                    "ls-remote",
                    "--heads",
                    "--tags",
                    "--",
                    origin,
                ],
                timeout=min(profile.git_timeout_s, 30.0),
                environment=_repository_git_environment(profile, probe_repo),
            )
            result["git_probe"] = {
                "scope": "one_repository",
                "namespace": probe_repo.namespace,
                "transport": profile.transport,
                "ok": True,
            }
        else:
            result["git_probe"] = {
                "scope": "no_repository_available",
                "namespace": None,
                "transport": profile.transport,
                "ok": None,
            }
    except MirrorError as exc:
        result["error"] = {
            "code": exc.code,
            "message": redact(exc.message, _redaction_secrets(profile, token)),
        }
        return result
    result["ok"] = all(result["tools"].values())
    return result


# CLI


def _json_dump(payload: object, *, stream: Any = sys.stdout) -> None:
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), file=stream)


def emit_error(code: str, message: str) -> None:
    _json_dump({"error": {"code": code, "message": message[:500]}}, stream=sys.stderr)


def _profile_index(profiles: Sequence[Profile], selector: str) -> int:
    matches = [
        index
        for index, profile in enumerate(profiles)
        if profile.name == selector or profile.profile_id == selector
    ]
    if len(matches) != 1:
        raise MirrorError("PROFILE_NOT_FOUND", f"Unknown profile: {selector}", exit_code=2)
    return matches[0]


def _optional_path(value: Path | None) -> Path | None:
    return value.expanduser().resolve(strict=False) if value is not None else None


def _transport_change_is_safe(profile: Profile) -> bool:
    managed_root = ManagedRoot(profile)
    if not managed_root.manifest_path.exists():
        return True
    manifest = managed_root.load_manifest()
    repositories = manifest.get("repositories")
    return isinstance(repositories, Mapping) and not repositories


def _cmd_profile(args: argparse.Namespace) -> int:
    profiles = load_profiles(args.config)
    if args.profile_command == "list":
        _json_dump({"profiles": [_profile_to_dict(profile) for profile in profiles]})
        return 0

    if args.profile_command == "add":
        discovery_mode = "accessible"
        target: str | None = None
        if args.public_user is not None:
            discovery_mode = "public-user"
            target = args.public_user
        elif args.public_group is not None:
            discovery_mode = "public-group"
            target = args.public_group
        profile = Profile(
            profile_id=str(uuid.uuid4()),
            name=args.name,
            provider=args.provider,
            api_url=args.api_url.rstrip("/"),
            root=args.root.expanduser().resolve(strict=False),
            token_env=args.token_env,
            jobs=getattr(args, "jobs", None) or 4,
            git_timeout_s=getattr(args, "git_timeout", None) or 300.0,
            run_timeout_s=getattr(args, "run_timeout", None) or 3600.0,
            ssh_key=_optional_path(getattr(args, "ssh_key", None)),
            ca_bundle=_optional_path(getattr(args, "ca_bundle", None)),
            discovery_mode=discovery_mode,
            target=target,
            transport=args.transport,
        )
        validate_profile(profile, profiles)
        save_profiles(args.config, [*profiles, profile])
        _json_dump({"profile": _profile_to_dict(profile)})
        return 0

    index = _profile_index(profiles, args.profile)
    current = profiles[index]
    if args.transport is not None and args.transport != current.transport:
        if not _transport_change_is_safe(current):
            raise MirrorError(
                "TRANSPORT_CHANGE_UNSAFE",
                "Transport cannot change after repositories are registered",
                exit_code=2,
            )
    changes: dict[str, object] = {}
    for argument, field in (
        ("token_env", "token_env"),
        ("jobs", "jobs"),
        ("git_timeout", "git_timeout_s"),
        ("run_timeout", "run_timeout_s"),
    ):
        value = getattr(args, argument, None)
        if value is not None:
            changes[field] = value
    if args.clear_token_env:
        changes["token_env"] = None
    if args.transport is not None:
        changes["transport"] = args.transport
        if args.transport == "https":
            changes["ssh_key"] = None
    for argument in ("ssh_key", "ca_bundle"):
        value = getattr(args, argument, None)
        if value is not None:
            changes[argument] = _optional_path(value)
    updated = replace(current, **changes)
    peers = [profile for position, profile in enumerate(profiles) if position != index]
    validate_profile(updated, peers)
    profiles[index] = updated
    save_profiles(args.config, profiles)
    _json_dump({"profile": _profile_to_dict(updated)})
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    profiles = load_profiles(args.config)
    profile = profiles[_profile_index(profiles, args.profile)]
    payload = doctor(profile)
    _json_dump(payload)
    return 0 if payload.get("ok") else 1


def _cmd_list(args: argparse.Namespace) -> int:
    _validate_pagination(args.offset, args.limit)
    profiles = load_profiles(args.config)
    profile = profiles[_profile_index(profiles, args.profile)]
    token = resolve_token(profile)
    repositories = discover(profile, token, threading.Event())
    registered: set[str] = set()
    managed_root = ManagedRoot(profile)
    if managed_root.manifest_path.is_file():
        manifest = managed_root.load_manifest()
        raw_registered = manifest.get("repositories", {})
        if isinstance(raw_registered, Mapping):
            registered = {str(key) for key in raw_registered}
    items = [
        {
            "project_id": repo.project_id,
            "namespace": repo.namespace,
            "default_branch": repo.default_branch,
            "enabled": repo.enabled,
            "managed": repository_key(profile, repo) in registered,
        }
        for repo in repositories
    ]
    _json_dump({"profile_id": profile.profile_id, **paginate(items, args.offset, args.limit)})
    return 0


def _cmd_sync(args: argparse.Namespace) -> int:
    if not args.profile and not args.all_profiles:
        raise MirrorError(
            "SYNC_SCOPE_REQUIRED",
            "Sync requires --profile or --all-profiles",
            exit_code=2,
        )
    if args.profile and args.all_profiles:
        raise MirrorError(
            "INVALID_SYNC_SCOPE",
            "--profile and --all-profiles are mutually exclusive",
            exit_code=2,
        )
    selected_count = len(args.profile or [])
    if args.repo and (args.all_profiles or selected_count != 1):
        raise MirrorError(
            "INVALID_REPO_SCOPE",
            "--repo requires exactly one selected profile",
            exit_code=2,
        )
    profiles = load_profiles(args.config)
    if args.all_profiles:
        selected = profiles
    else:
        selected = []
        seen: set[str] = set()
        for selector in args.profile:
            profile = profiles[_profile_index(profiles, selector)]
            if profile.profile_id not in seen:
                selected.append(profile)
                seen.add(profile.profile_id)
    if not selected:
        raise MirrorError("PROFILE_NOT_FOUND", "No profiles selected", exit_code=2)
    overrides = {
        "jobs": args.jobs,
        "git_timeout_s": args.git_timeout,
        "run_timeout_s": args.run_timeout,
    }
    effective: list[Profile] = []
    for profile in selected:
        changes = {key: value for key, value in overrides.items() if value is not None}
        candidate = replace(profile, **changes)
        validate_profile(candidate)
        effective.append(candidate)
    summary, exit_code = sync_profiles(
        effective,
        args.state_dir,
        repo_selection=args.repo,
    )
    _json_dump(summary)
    return exit_code


def _cmd_report(args: argparse.Namespace) -> int:
    payload = report_view(
        args.state_dir,
        run_id=args.run,
        errors_only=args.errors_only,
        offset=args.offset,
        limit=args.limit,
    )
    _json_dump(payload)
    return 0


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return parsed


def _add_profile_tuning(parser: argparse.ArgumentParser, *, update: bool = False) -> None:
    default = None if update else argparse.SUPPRESS
    parser.add_argument("--jobs", type=_positive_int, default=default)
    parser.add_argument("--git-timeout", type=_positive_float, default=default)
    parser.add_argument("--run-timeout", type=_positive_float, default=default)
    parser.add_argument("--ssh-key", type=Path, default=default)
    parser.add_argument("--ca-bundle", type=Path, default=default)


def build_parser() -> JsonArgumentParser:
    parser = JsonArgumentParser(prog=TOOL_NAME, description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--state-dir", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)

    profile = commands.add_parser("profile", help="Manage provider profiles")
    profile_commands = profile.add_subparsers(dest="profile_command", required=True)
    profile_add = profile_commands.add_parser("add")
    profile_add.add_argument("--name", required=True)
    profile_add.add_argument("--provider", choices=("gitlab", "github"), required=True)
    profile_add.add_argument("--api-url", required=True)
    profile_add.add_argument("--root", type=Path, required=True)
    public_target = profile_add.add_mutually_exclusive_group()
    public_target.add_argument("--public-user")
    public_target.add_argument("--public-group")
    profile_add.add_argument("--token-env")
    profile_add.add_argument("--transport", choices=tuple(sorted(TRANSPORTS)), default="https")
    _add_profile_tuning(profile_add)
    profile_list = profile_commands.add_parser("list")
    profile_update = profile_commands.add_parser("update")
    profile_update.add_argument("--profile", required=True)
    token_update = profile_update.add_mutually_exclusive_group()
    token_update.add_argument("--token-env")
    token_update.add_argument("--clear-token-env", action="store_true")
    profile_update.add_argument("--transport", choices=tuple(sorted(TRANSPORTS)))
    _add_profile_tuning(profile_update, update=True)
    profile_add.set_defaults(handler=_cmd_profile)
    profile_list.set_defaults(handler=_cmd_profile)
    profile_update.set_defaults(handler=_cmd_profile)

    doctor = commands.add_parser("doctor", help="Run read-only diagnostics")
    doctor.add_argument("--profile", required=True)
    doctor.set_defaults(handler=_cmd_doctor)

    list_command = commands.add_parser("list", help="List discovered repositories")
    list_command.add_argument("--profile", required=True)
    list_command.add_argument("--offset", type=int, default=0)
    list_command.add_argument("--limit", type=int, default=DEFAULT_PAGE_LIMIT)
    list_command.set_defaults(handler=_cmd_list)

    sync = commands.add_parser("sync", help="Synchronize managed repository mirrors")
    sync.add_argument("--profile", action="append")
    sync.add_argument("--all-profiles", action="store_true")
    sync.add_argument("--repo", action="append", default=[])
    sync.add_argument("--jobs", type=_positive_int)
    sync.add_argument("--git-timeout", type=_positive_float)
    sync.add_argument("--run-timeout", type=_positive_float)
    sync.set_defaults(handler=_cmd_sync)

    report = commands.add_parser("report", help="Read a saved synchronization report")
    report.add_argument("--run")
    report.add_argument("--errors-only", action="store_true")
    report.add_argument("--offset", type=int, default=0)
    report.add_argument("--limit", type=int, default=DEFAULT_PAGE_LIMIT)
    report.set_defaults(handler=_cmd_report)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        if args.config is None:
            args.config = default_config_path()
        if args.state_dir is None:
            args.state_dir = default_state_path()
        return int(args.handler(args))
    except MirrorError as exc:
        emit_error(exc.code, exc.message)
        return exc.exit_code
    except KeyboardInterrupt:
        emit_error("INTERRUPTED", "Operation interrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
