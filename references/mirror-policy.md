# Mirror policy

Git Me Everything first reads every API page for a selected profile. No Git
mutation begins from a partial provider response.

Repository identity is the provider API instance plus the provider project id.
The managed-root marker binds a root to one profile, and the manifest binds an
identity to its namespace, local path, and origin. The skill preserves an
existing directory it did not register. Renames, origin changes, missing source
entries, and disabled repositories are reported instead of silently moved or
deleted.

Each profile chooses HTTPS or SSH when it is created and can change it before
the first repository is registered. The selected origin is part of manifest
identity, so active mirrors do not change transport automatically. New profiles
use HTTPS. Public user/group HTTPS mirrors are
anonymous; accessible HTTPS mirrors use their API token only in the Git process
environment. SSH remains available when explicitly selected.

New repositories are cloned into a uniquely named staging directory and
published with an atomic rename. Failed or interrupted staging is removed.

## Destructive boundary

For a registered checkout, synchronization force-aligns local branches and tags
with the remote, chooses the provider default branch when available, resets the
work tree, and runs `git clean -ffdx`. Local commits, changes, ignored files,
and untracked files inside that checkout are disposable.

Before those commands, the skill verifies:

- root ownership and manifest identity
- canonical path containment without symlink or junction escapes
- an owned `.git` directory and regular configuration file
- exact top-level, Git directory, common directory, and origin
- absence of local worktree overrides, includes, URL rewrites, executable
  filters, linked worktrees, sparse/shallow/partial state, initialized
  submodules, object alternates, and in-progress operations

Unsafe or unmanaged paths are preserved and returned as bounded JSON results.
Repository commands use explicit absolute Git and work-tree paths, isolated Git
configuration, disabled hooks/filters/submodule recursion, and no terminal
prompts.

The mirror contains complete Git history, branches, and tags. Git LFS objects
are not downloaded; pointer files remain. Submodules are not initialized.

Every sync creates a redacted event stream and an atomic final report with
states `completed`, `partial`, `incomplete`, or `interrupted`.
