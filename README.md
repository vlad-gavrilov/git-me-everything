# Git Me Everything

An installable Agent Skill that mirrors public GitHub/GitLab users and groups
without credentials, or every repository available to an authenticated account.
Synchronization intentionally discards local changes only inside repositories
registered by the skill.

## Install

Install this repository as a skill with your Agent Skills-compatible client,
or point the client at a local checkout:

```sh
npx skills add /absolute/path/to/git-me-everything
```

The runtime requires Python 3.12+, `uv`, and Git. OpenSSH is needed only for
profiles that explicitly select SSH. It is a PEP 723 script, so no package
installation step is needed:

```sh
uv run --script /absolute/path/to/skill/scripts/mirror.py --help
```

## Start safely

Public targets use anonymous HTTPS by default and need no token or SSH key:

```sh
uv run --script scripts/mirror.py profile add \
  --name octocat \
  --provider github \
  --api-url https://api.github.com \
  --root /absolute/path/to/managed-checkouts \
  --public-user octocat

uv run --script scripts/mirror.py doctor --profile octocat
uv run --script scripts/mirror.py sync --profile octocat
```

For every repository available to an account, create a provider token outside
the skill and expose it through an environment variable. New profiles use HTTPS
and reuse that token for Git access:

```sh
export GME_GITHUB_TOKEN='replace-in-your-shell'

uv run --script scripts/mirror.py profile add \
  --name personal \
  --provider github \
  --api-url https://api.github.com \
  --root /absolute/path/to/managed-checkouts \
  --token-env GME_GITHUB_TOKEN

uv run --script scripts/mirror.py doctor --profile personal
uv run --script scripts/mirror.py sync --profile personal
```

Use `--public-group` for a GitHub Organization or GitLab Group. GitLab group
profiles include subgroup projects but not projects merely shared into the
group. Public targets can optionally use `--token-env` to increase API limits;
they still clone anonymously.

The token value is never accepted as a CLI argument or stored in the profile.
Use `--transport ssh` only when SSH is required; existing profiles without a
transport field retain SSH behavior. Review [the security model](references/security.md)
before the first sync and [the mirror policy](references/mirror-policy.md)
before pointing a profile at an existing directory. Full command details are
in [the CLI reference](references/cli.md).

## Configuration and reports

On macOS and Linux, profiles default to
`$XDG_CONFIG_HOME/git-me-everything/profiles.json` or
`~/.config/git-me-everything/profiles.json`. Run reports default to
`$XDG_STATE_HOME/git-me-everything/` or `~/.local/state/git-me-everything/`.
Relative or empty XDG values are ignored. Windows continues to use its native
`platformdirs` locations.

When no valid absolute XDG variable is set, an existing legacy `platformdirs`
location remains in use until the matching new location exists. Valid absolute
XDG paths take precedence, and the tool never merges or automatically moves
data. If both new and legacy locations exist, the new one wins.

Existing macOS users do not need to migrate immediately. To copy old data to
the default XDG locations manually, first stop other Git Me Everything runs,
then use:

```sh
mkdir -p "$HOME/.config/git-me-everything"
cp -p -n "$HOME/Library/Application Support/git-me-everything/profiles.json" \
  "$HOME/.config/git-me-everything/profiles.json"

mkdir -p "$HOME/.local/state/git-me-everything"
if [ -d "$HOME/Library/Application Support/git-me-everything/runs" ] &&
   [ ! -e "$HOME/.local/state/git-me-everything/runs" ]; then
  cp -Rp "$HOME/Library/Application Support/git-me-everything/runs" \
    "$HOME/.local/state/git-me-everything/"
fi

uv run --script scripts/mirror.py profile list
uv run --script scripts/mirror.py report
```

The copy command skips reports when the new `runs` directory already exists;
compare the locations manually before deciding which copy to keep. Keep the
legacy copy until `profile list` and `report` show the expected data; it is not
removed automatically.

## Development

The runtime and its dependency declaration intentionally live in one Python
file. Development tools run ephemerally through `uvx`, so the repository does
not need project metadata or lock files.

```sh
uvx --with-requirements scripts/mirror.py pytest -q
uvx ruff check --line-length 100 --target-version py312 --select E,F,W,I,UP,B .
uvx ruff format --check --line-length 100 --target-version py312 .
uv run --script scripts/mirror.py --help
```

The scenario suite uses local temporary repositories and never needs real
provider credentials. This repository does not claim live-provider or
cross-platform verification until those checks are run explicitly.

## License

MIT. Copyright 2026 Vlad Gavrilov.
