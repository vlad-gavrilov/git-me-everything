# Security and tokens

Git Me Everything accepts tokens only through environment variables. A profile
records `token_env`, such as `GME_GITHUB_TOKEN`, but never the token value.
There is no token CLI argument, token file, `.env` loader, keychain, or
credential-store integration.

The profile file may live in the platform's supported config location, but it
still contains only token environment-variable names. Token values stay
external to Git Me Everything and must never be copied into `profiles.json`.

Public GitHub/GitLab user and group profiles use HTTPS anonymously by default.
In that transport they do not need a token or SSH identity. A configured
optional token only raises API limits and is never used for public Git
operations. Public profiles can explicitly select SSH, which still requires a
usable SSH identity for Git access.

For accessible-account profiles, create a dedicated read-only token outside the
skill:

- GitHub or GitHub Enterprise HTTPS: use fine-grained read-only Metadata and
  Contents permissions. A classic token may need `repo` and `read:org`.
- GitLab HTTPS: use one PAT with `read_api` and `read_repository`.
- GitLab SSH: `read_api` is sufficient for discovery; Git access uses the SSH
  identity instead.

Export the value only for the process or shell that runs the skill:

```sh
GME_GITHUB_TOKEN='secret-from-your-manager' \
  uv run --script scripts/mirror.py list --profile personal
```

For repeated local use, prefer your shell or operating-system secret manager.
For headless use, inject the variable from that environment's secret facility.
Do not place tokens in this repository, command history, profile JSON, or URLs.

For accessible HTTPS profiles, Git receives a URL-scoped Basic authorization
header only in its command environment. The token is never put in a command
argument, origin URL, `.git/config`, profile, report, or temporary file.
Credential helpers and HTTP redirects are disabled for that command. The runtime
removes configured token variables before starting Git or SSH and strips ambient
Git repository variables and configuration. Reports, events, errors, headers,
and URL userinfo are redacted before output or persistence.

Environment variables are still visible to the current process and may be
inherited by unrelated child processes started from the same shell. Limit their
lifetime and scope. Rotate a token in the external secret manager; the profile
does not need to change while the variable name stays the same.

SSH uses non-interactive mode and strict host-key checking. Add trusted provider
host keys through normal SSH administration before an SSH `doctor` or `sync`;
the skill does not modify `known_hosts`. HTTPS remains HTTPS only; plain HTTP is
not supported.
