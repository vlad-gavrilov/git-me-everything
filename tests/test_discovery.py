from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest


@pytest.fixture
def api_server():
    running = []

    def start(responder):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                requests.append({"path": self.path, "headers": dict(self.headers)})
                status, headers, payload = responder(self)
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format, *_args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        running.append((server, thread))
        host, port = server.server_address
        return f"http://{host}:{port}", requests

    yield start

    for server, thread in running:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _profile(module, api_url, provider="gitlab"):
    return module.Profile(
        profile_id="00000000-0000-4000-8000-000000000001",
        name="work",
        provider=provider,
        api_url=api_url,
        root=module.Path("/tmp/mirrors"),
        token_env="GME_TEST_TOKEN",
        run_timeout_s=2,
    )


def test_gitlab_discovers_all_pages_and_marks_archived(mirror_module, api_server):
    def responder(handler):
        parsed = urlsplit(handler.path)
        assert parsed.path == "/api/v4/projects"
        page = parse_qs(parsed.query)["page"][0]
        if page == "1":
            return (
                200,
                {"X-Next-Page": "2"},
                [
                    {
                        "id": 20,
                        "path_with_namespace": "team/zeta",
                        "ssh_url_to_repo": "git@example:team/zeta.git",
                        "http_url_to_repo": "https://example/team/zeta.git",
                        "default_branch": "main",
                        "archived": False,
                    }
                ],
            )
        return (
            200,
            {},
            [
                {
                    "id": 10,
                    "path_with_namespace": "team/alpha",
                    "ssh_url_to_repo": "git@example:team/alpha.git",
                    "http_url_to_repo": "https://example/team/alpha.git",
                    "default_branch": "main",
                    "archived": True,
                }
            ],
        )

    base, requests = api_server(responder)
    profile = _profile(mirror_module, f"{base}/api/v4")

    repos = mirror_module.discover(profile, "secret", threading.Event())

    assert [(repo.namespace, repo.project_id, repo.enabled) for repo in repos] == [
        ("team/alpha", "10", False),
        ("team/zeta", "20", True),
    ]
    assert [parse_qs(urlsplit(item["path"]).query)["page"][0] for item in requests] == [
        "1",
        "2",
    ]
    assert all(item["headers"]["PRIVATE-TOKEN"] == "secret" for item in requests)


def test_github_follows_only_in_prefix_next_links(mirror_module, api_server):
    off_prefix = False

    def responder(handler):
        parsed = urlsplit(handler.path)
        page = parse_qs(parsed.query)["page"][0]
        host, port = handler.server.server_address
        if page == "1":
            prefix = "evil" if off_prefix else "api/v3"
            next_url = f"http://{host}:{port}/{prefix}/user/repos?per_page=100&page=2"
            return (
                200,
                {"Link": f'<{next_url}>; rel="next"'},
                [
                    {
                        "id": 20,
                        "full_name": "team/zeta",
                        "ssh_url": "git@example:team/zeta.git",
                        "clone_url": "https://example/team/zeta.git",
                        "default_branch": "main",
                        "archived": False,
                        "disabled": False,
                    }
                ],
            )
        return (
            200,
            {},
            [
                {
                    "id": 10,
                    "full_name": "team/alpha",
                    "ssh_url": "git@example:team/alpha.git",
                    "clone_url": "https://example/team/alpha.git",
                    "default_branch": "main",
                    "archived": False,
                    "disabled": False,
                }
            ],
        )

    base, requests = api_server(responder)
    profile = _profile(mirror_module, f"{base}/api/v3", "github")

    repos = mirror_module.discover(profile, "secret", threading.Event())

    assert [repo.namespace for repo in repos] == ["team/alpha", "team/zeta"]
    assert all(urlsplit(item["path"]).path.startswith("/api/v3/") for item in requests)

    off_prefix = True
    with pytest.raises(mirror_module.MirrorError) as caught:
        mirror_module.discover(profile, "secret", threading.Event())
    assert caught.value.code == "DISCOVERY_FAILED"

    target_base, redirected_requests = api_server(lambda _handler: (200, {}, []))

    def redirect(_handler):
        return 302, {"Location": f"{target_base}/stolen"}, {}

    redirect_base, _requests = api_server(redirect)
    redirect_profile = _profile(mirror_module, f"{redirect_base}/api/v3", "github")
    with pytest.raises(mirror_module.MirrorError) as caught:
        mirror_module.discover(redirect_profile, "secret", threading.Event())
    assert caught.value.code == "DISCOVERY_FAILED"
    assert redirected_requests == []


@pytest.mark.parametrize(
    ("provider", "discovery_mode", "target", "path", "expected_query"),
    [
        (
            "github",
            "public-user",
            "octocat",
            "/api/v3/users/octocat/repos",
            {"type": "owner"},
        ),
        (
            "github",
            "public-group",
            "octo-org",
            "/api/v3/orgs/octo-org/repos",
            {"type": "all"},
        ),
        (
            "gitlab",
            "public-user",
            "alice",
            "/api/v4/users/alice/projects",
            {"visibility": "public"},
        ),
        (
            "gitlab",
            "public-group",
            "team",
            "/api/v4/groups/team/projects",
            {"visibility": "public", "include_subgroups": "true", "with_shared": "false"},
        ),
    ],
)
def test_public_targets_are_anonymous_and_exclude_private_repositories(
    mirror_module,
    api_server,
    provider,
    discovery_mode,
    target,
    path,
    expected_query,
):
    def project(project_id, namespace, *, public):
        if provider == "github":
            return {
                "id": project_id,
                "full_name": namespace,
                "ssh_url": f"git@example:{namespace}.git",
                "clone_url": f"https://example/{namespace}.git",
                "default_branch": "main",
                "private": not public,
                "archived": False,
                "disabled": False,
            }
        return {
            "id": project_id,
            "path_with_namespace": namespace,
            "ssh_url_to_repo": f"git@example:{namespace}.git",
            "http_url_to_repo": f"https://example/{namespace}.git",
            "default_branch": "main",
            "visibility": "public" if public else "private",
            "archived": False,
        }

    def responder(handler):
        parsed = urlsplit(handler.path)
        assert parsed.path == path
        query = parse_qs(parsed.query)
        for key, value in expected_query.items():
            assert query[key] == [value]
        return (
            200,
            {},
            [
                project(1, "team/public", public=True),
                project(2, "team/private", public=False),
            ],
        )

    base, requests = api_server(responder)
    profile = mirror_module.Profile(
        profile_id="00000000-0000-4000-8000-000000000001",
        name="public",
        provider=provider,
        api_url=f"{base}/api/v{'3' if provider == 'github' else '4'}",
        root=mirror_module.Path("/tmp/mirrors"),
        token_env=None,
        discovery_mode=discovery_mode,
        target=target,
        transport="https",
        run_timeout_s=2,
    )

    repositories = mirror_module.discover(profile, None, threading.Event())

    assert [repo.namespace for repo in repositories] == ["team/public"]
    assert "Authorization" not in requests[0]["headers"]
    assert "PRIVATE-TOKEN" not in requests[0]["headers"]


def test_later_page_failure_runs_no_git_mutations(
    mirror_module,
    api_server,
    monkeypatch,
):
    def responder(handler):
        page = parse_qs(urlsplit(handler.path).query)["page"][0]
        if page == "1":
            return (
                200,
                {"X-Next-Page": "2"},
                [
                    {
                        "id": 10,
                        "path_with_namespace": "team/alpha",
                        "ssh_url_to_repo": "git@example:team/alpha.git",
                        "http_url_to_repo": "https://example/team/alpha.git",
                        "default_branch": "main",
                        "archived": False,
                    }
                ],
            )
        return 503, {"Retry-After": "0"}, {"message": "temporary failure"}

    class RecordingGit:
        def __init__(self):
            self.calls = []

        def sync(self, repo):
            self.calls.append(repo)

    base, requests = api_server(responder)
    profile = _profile(mirror_module, f"{base}/api/v4")
    monkeypatch.setenv(profile.token_env, "secret")
    recording_git = RecordingGit()

    with pytest.raises(mirror_module.MirrorError) as caught:
        mirror_module.discover_and_sync(profile, recording_git)

    assert caught.value.code == "DISCOVERY_FAILED"
    assert recording_git.calls == []
    assert len(requests) == 4
