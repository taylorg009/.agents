# Monitors

Durable event-triggered watchers. A monitor fires on a **change** — it watches a
SOURCE, detects a CONDITION, and fires an ACTION (run an agent, run a routine,
notify, or POST a webhook). The daemon owns the loop, so a monitor outlives the
agent that enabled it.

If you want something to fire on a **clock** instead of a change, that is a
[routine](../routines/README.md), not a monitor.

A monitor shipped here is a **system-layer built-in**: it lands at
`~/.agents/.system/monitors/` on every box via the `.system` pull, the CLI
discovers it, and it is **opt-in** — disabled until you run
`agents monitors enable <name>`. A copy under `~/.agents/monitors/` shadows the
built-in, so you can customize one per box without editing this repo.

## What ships here

| Monitor | Source | What it does |
|---|---|---|
| [`pr-merge-on-green`](./pr-merge-on-green.yml) | poll `pr-merge-on-green.sh` (`gh search prs` + `gh pr view --repo`), every 5 min | Rebase-merges this machine's own open PRs once CI is green **and** a non-author verdict is on the same PR (a GitHub APPROVED review or an APPROVE comment; same check as `merge-guard.sh`). Opt-in; disabled until enabled. |

## Using them

### Evaluate a metrics ledger

[`metrics-dashboard.py`](./metrics-dashboard.py) renders a read-only evaluation
dashboard from a JSONL event ledger and a repository manifest. It requires Python
3 and the installed `artifacts` command. It never calls GitHub or starts a server.

```sh
python3 metrics-dashboard.py --ledger /path/to/metrics.jsonl \
  --manifest /path/to/repos.conf --output /path/to/dashboard \
  --title "Monitor evaluation"
```

The dashboard separates observed sweep health, posted comments, full review
completion and reported healing outcomes. It shows 24-hour and seven-day windows;
it does not infer uptime, finding accuracy or verified fixes from these events.
The manifest uses tab-separated repository, integration branch and required-check
columns. Keep operational inputs and generated evidence outside this repository.

Review events may include a complete optional detail envelope: `attempt_id`,
`head_sha` (40 hexadecimal characters), timezone-qualified `started_at` and
`finished_at`, and exactly two `contributors`. Each contributor supplies
`harness`, `family`, nullable `requested_model` and `actual_model`, integer
`exit_code`, timezone-qualified `started_at` and `finished_at`, nonnegative
`duration_s`, boolean `summary_present` and `completed`, nullable `failure_reason`,
and a 64-character hexadecimal `prompt_sha256`. Completion requires a zero exit,
a summary and no failure reason; incomplete contributors require a reason. The
completed count must match `reviewers_ok`. Contributor times must fit the attempt,
which must finish by the event timestamp; duration must match within one second.
Malformed or partial detail envelopes fail the refresh. Legacy events remain valid:
their detail fields are null in evidence and explicitly unknown on the page.

Recent cases and source events retain these details in `evidence.json`. The page
shows the recorded head SHA, attempt, reviewer outcomes and durations. Requested
models never substitute for unknown actual models. These records do not measure
cost or review quality.

Schedule the command through a user-level routine for periodic regeneration. It
is not scheduled by installation. A failed refresh retains the last successful
evidence timestamp and reports the failure instead of presenting old data as new.

### Event-triggered monitors

```bash
agents monitors list                       # see every monitor + its enable state
agents monitors enable pr-merge-on-green    # turn the built-in on (this box)
agents monitors test pr-merge-on-green      # dry-run: evaluate the source once, no action
agents monitors pause pr-merge-on-green      # temporarily stop it
```

`pr-merge-on-green` needs `gh` authenticated on the box. It scopes to your own
GitHub user (`gh search prs --author @me`) and names `--repo` on every per-PR
`gh` call, so it still evaluates when the daemon's cwd is not a git checkout
(RUSH-2848). Enable it on the one box you want to own merges from — enabling it
on several boxes has each daemon race to merge the same PRs.

**Version note:** the system-layer monitors mechanism ships in agents-cli
1.22.36. On an older CLI the built-in is inert (not discovered) — harmless, since
it is opt-in and off until enabled.
