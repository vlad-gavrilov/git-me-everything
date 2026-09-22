---
name: git-me-everything
description: Use when the user wants to inventory, back up, mirror, or force-synchronize all accessible GitHub, GitHub Enterprise, or GitLab repositories into managed local checkouts.
---

# Git Me Everything

Use the PEP 723 script at `scripts/mirror.py`. Treat synchronization as
destructive: it removes local changes and untracked files, but only after the
managed-root marker, manifest identity, origin, Git configuration, and canonical
paths pass preflight checks.

Before the first sync:

1. Read [the security model](references/security.md).
2. Ask for the provider, API URL, absolute managed root, discovery scope, and
   transport. Public user or group profiles can use HTTPS without credentials;
   accessible-account profiles need a token environment-variable name. Never
   ask the user to paste a token value.
3. Confirm that the chosen root is dedicated to generated checkouts.
4. Add the profile, run `doctor`, then use `list` if the user wants to inspect
   discovery before mutation.

Run commands with:

```sh
uv run --script /absolute/path/to/scripts/mirror.py COMMAND
```

Map requests to commands:

- configure a profile: `profile add` or `profile update`
- inspect configured accounts: `profile list`
- diagnose credentials, tools, API access, and one remote: `doctor`
- inventory accessible repositories: `list`
- synchronize: `sync --profile NAME` or explicit `sync --all-profiles`
- inspect a prior run: `report`

When summarizing a synchronization run for the user, follow the
[work-report template](assets/work-report-template.md). Use only details
confirmed by command output or the saved report.

Never infer sync scope. A repository selector may be used only with exactly one
profile. Do not weaken a safety rejection or manually delete an unmanaged path;
report the machine-readable reason and leave the path untouched.

Use [the CLI reference](references/cli.md) for exact arguments and exit codes.
Use [the mirror policy](references/mirror-policy.md) to explain discovery,
identity, destructive behavior, unsupported repository states, and reports.
