# Git Me Everything work report

Use this template for a concise user-facing summary. Respond in the user's
language, keep only applicable sections, and fill fields from command output or
verified profile and report data. Do not infer missing values or include token
values, authorization headers, or other credentials.

**Operation:** [profile add/update, doctor, list, sync, or report]  
**Outcome:** [observed state or result] — [one-sentence summary]  
**Scope:** [selected profile(s), provider, and target when confirmed]

## Results

Summarize the operation using observed values. For sync, list each status and
its exact count from `counts`. If useful, show discovered repository counts from
the saved report separately; do not confuse them with sync results.

| Status | Count |
| --- | ---: |
| [status from counts] | [count] |

## Attention

Omit this section when there are no problems. Otherwise list each available
repository or profile, status, reason, and reported message. If the summary says
problems are truncated, state how many are shown out of the total and use the
`report --run RUN_ID --errors-only` command with pagination to inspect additional
repository entries. Profile-level failures are not returned by that command;
inspect the `profiles` array in the saved JSON report for those.

- [repository or profile] — [status] ([reason]): [reported message]

## Run details

Include for sync when available: run ID, start and finish timestamps, and the
saved JSON report path if useful in this private context. Omit unneeded local
absolute paths from reports intended for a public audience.

## Sync effect

Include for sync: checkouts that passed preflight and were synchronized are
force-aligned; local commits, changes, untracked files, and ignored files inside
them may be discarded. Do not imply skipped repositories were synchronized or
claim files were actually removed unless the run provides evidence.

**Next step:** [only a concrete action supported by the result; omit if none]
