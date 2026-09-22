# CLI reference

Use the absolute script path when invoking Git Me Everything from an agent:

```sh
uv run --script /absolute/path/to/scripts/mirror.py [GLOBAL_OPTIONS] COMMAND
```

Global options:

- `--config PATH`: profile JSON; defaults to the user config location below
- `--state-dir PATH`: run reports; defaults to the user state location below

Default location precedence:

1. Explicit `--config` or `--state-dir` arguments.
2. On Windows, the native `platformdirs` config and state locations.
3. On macOS/Linux, an absolute `XDG_CONFIG_HOME` or `XDG_STATE_HOME` value.
4. On macOS/Linux, the new XDG target when it exists or no legacy data exists:
   `~/.config/git-me-everything/profiles.json` and
   `~/.local/state/git-me-everything/`.
5. An existing legacy `platformdirs` target when the matching new XDG target
   does not exist.

Relative and empty XDG values are ignored. Valid absolute XDG paths take
precedence over legacy data. When both new and legacy targets exist, the new
XDG location wins. Explicit CLI paths are resolved before any default-location
checks. The tool never merges or automatically moves config or report data.

## Profiles

```text
profile add --name NAME --provider gitlab|github --api-url URL --root ABSOLUTE_PATH
            [--public-user LOGIN | --public-group NAMESPACE] [--token-env VAR]
            [--transport https|ssh] [--jobs N] [--git-timeout S] [--run-timeout S]
            [--ssh-key PATH] [--ca-bundle PATH]
profile list
profile update --profile NAME_OR_ID [--token-env VAR | --clear-token-env]
               [--transport https|ssh] [--jobs N]
               [--git-timeout S] [--run-timeout S] [--ssh-key PATH]
               [--ca-bundle PATH]
```

Without `--public-user` or `--public-group`, a profile discovers every
repository accessible to its token and requires `--token-env`. A public user
profile discovers only that user's public personal repositories. A public group
is a GitHub Organization or GitLab Group; GitLab includes subgroups and excludes
projects merely shared into the group. Public profiles may omit `--token-env`.

New profiles default to HTTPS. HTTPS public mirrors use no credentials. HTTPS
accessible mirrors use the configured token for API and Git; SSH accessible
mirrors use the token only for API plus SSH authentication. `--ssh-key` requires
`--transport ssh`. Configuration stores only the token variable name, never its
value. Provider, API URL, root, discovery scope, target, and profile identity
are immutable. Transport can change only before any repository is registered.
Changing to HTTPS removes any configured `ssh_key` from the profile; it does
not modify the key file itself.

For manual macOS migration steps, see the configuration and reports section in
the README. Keep the legacy copy until the new config and reports are verified.

## Read-only commands

```text
doctor --profile NAME_OR_ID
list --profile NAME_OR_ID [--offset N] [--limit N]
report [--run UUID] [--errors-only] [--offset N] [--limit N]
```

`doctor` checks the profile, required credentials, tools for the selected
transport, API access, manifest, and at most one repository remote. `list`
completes provider discovery and shows whether each repository is already
managed. `report` reads a saved atomic run report. Pagination defaults to 50
items and accepts limits from 1 through 200.

## Synchronization

```text
sync (--profile NAME_OR_ID ... | --all-profiles)
     [--repo NAMESPACE/PATH ...]
     [--jobs N] [--git-timeout S] [--run-timeout S]
```

Scope is always explicit. `--repo` requires exactly one selected profile and
uses exact namespace paths. Tuning options apply to that run without rewriting
the profile.

All successful command output is compact JSON on stdout. Errors are one JSON
object on stderr. A sync summary includes its run id, state, counts, report
path, attention flag, and at most 20 problems.

Exit codes:

- `0`: success
- `1`: repository error or result requiring attention
- `2`: invalid arguments, configuration, or missing token
- `3`: discovery failed for every selected profile
- `130`: interrupted
