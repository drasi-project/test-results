# Drasi test results

Durable, public storage for performance and pass/fail results from the Drasi
E2E test framework, plus a static dashboard for spotting trends across runs.

**Dashboard:** https://drasi-project.github.io/test-results/

Test runs happen in [`drasi-project/test-infra`](https://github.com/drasi-project/test-infra).
Those workflows upload detailed artifacts, but GitHub expires them after 90
days, so there is no long-term history. This repo keeps a small summary of
every run forever.

This repo only *stores and visualizes*. It never runs tests.

## What it's for

- Hold the daily scheduled run results in one durable place.
- Visualize them without operating any infrastructure — GitHub Actions builds
  the site, GitHub Pages serves it.
- Compare runs and watch trends: throughput and duration over time, filtered
  by scenario, variant, target, transport, or reaction.

## Layout

```
results/YYYY/MM/DD/<scenario>__<variant>__<run_id>.json   # one file per CI job
scenarios/<scenario>/template.json                        # display metadata
generate-results.py                                       # results/ -> data.generated.js
validate-results.py                                       # schema gate (run in CI)
index.html                                                # the dashboard
tools/make-seed-data.py                                   # sample data generator
```

One file per CI job, and files are never edited after they land. Because each
job writes its own distinct path, two jobs can never touch the same file.

The date leads the path so a date-range query only has to look at the relevant
directories, and so old years could be archived later without disturbing
anything else. Dimensions that will grow over time (index config, query
complexity, workload params) live *inside* the JSON, not in the path — adding
one must not change the path grammar or break existing readers.

### Why the display names are separate

`scenarios/<scenario>/template.json` holds presentation metadata: human-readable
names for scenarios, variants and reactions.

It is kept out of the result files on purpose. Result files are immutable
history; if display names were copied into every one of them, fixing a single
typo would mean rewriting thousands of files. Instead the names are joined in
at build time, and a variant that has been retired from CI still renders
correctly because the template still describes it.

## The record

```json
{
  "schema_version": 1,
  "run": {
    "run_id": "17400011133",
    "run_attempt": 1,
    "workflow": "e2e-building-comfort.yml",
    "trigger": "schedule",
    "runner": "ubuntu-latest",
    "started_at": "2026-07-27T07:00:00Z",
    "finished_at": "2026-07-27T07:04:05Z",
    "url": "https://github.com/drasi-project/test-infra/actions/runs/17400011133"
  },
  "versions": {
    "test_infra_sha": "3f1c9a77d2b4e86051aa77c3d9e2b1f4a6c8d0e2",
    "drasi_server_tag": "v0.1.4",
    "drasi_server_build": "drasi-server 0.1.4"
  },
  "dimensions": {
    "scenario": "building_comfort",
    "variant": "drasi_server_http",
    "target": "drasi_server",
    "transport": "http"
  },
  "params": { "change_count": 100000, "seed": 123456789 },
  "status": "success",
  "determinism": "pass",
  "reactions": [
    {
      "reaction_id": "building-comfort",
      "status": "Stopped",
      "records": 99981,
      "expected_records": 99981,
      "duration_s": 41.2,
      "records_per_sec": 2426.7,
      "determinism": "pass",
      "sha256": "3b930107b012"
    }
  ],
  "totals": { "records": 149841, "duration_s": 52.0, "records_per_sec": 2881.6 }
}
```

Things worth knowing:

- **`reactions` is an array** because a scenario can have several reactions
  with different record counts (`building_comfort` has two). A single
  run-level number would be ambiguous.
- **`status` and `determinism` are independent.** A run can complete
  successfully and still fail determinism.
- **`determinism: "not_applicable"`** is correct and expected for scenarios
  with no determinism handler (`stock_market`). Never report a fabricated
  `pass`.
- **`versions.drasi_server_tag` matters.** Most regressions come from
  drasi-server, not from test-infra. Without it a downward trend can't be
  attributed. The dashboard draws a marker wherever this value changes.
- **`totals.duration_s` is wall clock** (latest end minus earliest start), not
  a sum, so `totals.records_per_sec` is a meaningful aggregate rather than a
  sum of rates.
- **`expected_records`** makes a truncated run detectable. A run that stopped
  early otherwise looks *fast* on a throughput chart.

### Adding fields

`validate-results.py` checks a small core strictly and ignores everything else,
so new fields can be added at any time without breaking older files or older
readers. Put new dimensions in `dimensions`, new workload settings in `params`.

Only bump `schema_version` for a genuinely breaking change — a field removed,
renamed, or given different meaning. Adding an optional field is not breaking.
Readers must treat a missing field as *absent*, never as zero; a zero silently
becomes a fake datapoint on a trend line.

## Ingestion contract for test-infra

A GitHub Action in `test-infra` builds the summary and pushes it here. What it
must do:

**1. Build one record per job.** Source fields from the run's artifacts:

| Record field | Source |
|---|---|
| `reactions[].records` | `PerformanceMetrics.record_count` |
| `reactions[].duration_s` | `PerformanceMetrics.duration_ns / 1e9` |
| `reactions[].records_per_sec` | `PerformanceMetrics.records_per_second` |
| `reactions[].status` | `final_reaction_state__<id>.json` → `.reaction_observer.status` |
| `reactions[].sha256` | `.reaction_observer.logger_results[]` where `logger_name == "DeterminismHash"` → `.summary.sha256` |
| `reactions[].determinism` | `determinism_verdict.json` → `.results[<reaction_id>].passed` |
| `run.*` | GitHub Actions context |
| `versions.drasi_server_tag` | the resolved drasi-server release tag |
| `params.*` | the variant's `config.json` |

Two traps in that mapping:

- **Do not use `result_summary.observer_runtime_s` for duration.** It is a
  human-readable string like `"5.2 minutes"`. Duration must come from
  `PerformanceMetrics.duration_ns`.
- **The two artifacts key reactions differently.** `determinism_verdict.json`
  uses the bare id (`building-comfort`); `PerformanceMetrics.test_run_reaction_id`
  uses the dotted form
  (`drasi_server_dev_repo.building_comfort.test_run_001.building-comfort`).
  Join on the last dotted segment. Records here always use the **bare** id.

**2. Validate before pushing.** Fetch `validate-results.py` from this repo and
run it against the new file. A bad record then fails in test-infra, where the
author sees it, instead of turning this repo red.

**3. Write to the idempotent path:**

```
results/<YYYY>/<MM>/<DD>/<scenario>__<variant>__<run_id>.json
```

using the UTC date of `run.started_at`. **`run_attempt` is deliberately not in
the path** — `run_id` is stable across re-runs, so a re-run overwrites its own
file instead of adding a duplicate datapoint. Skip the commit entirely when
content is unchanged (`git diff --cached --quiet`).

**4. Aggregate, then push once.** Have the per-scenario jobs upload their
summary JSON as a normal artifact, then a single final job (`needs:` all of
them) downloads them and makes one commit.

Distinct file paths prevent *merge* conflicts, but they do not prevent *push*
races: two jobs pushing to `main` at the same moment means the second is
rejected as non-fast-forward regardless of which files it touched. Git rejects
the ref update, not the content. Pushing once per workflow avoids this;
retry a `git pull --rebase` a few times to cover the remaining race between
the two workflows.

**5. Authenticate with a deploy key.** An SSH key with write access to this
repo only, held in test-infra as a secret. Deploy keys don't expire, so CI
can't silently break the way it would when a token lapses.

**6. Never fail the test job because publishing failed.** Run the push step
with `if: always()` and let it exit 0 on error. A lost datapoint is much
better than a red E2E run that people learn to ignore.

## Working on this repo

```bash
python3 validate-results.py          # check every result file
python3 generate-results.py          # build data.generated.js
python3 -m http.server 8000          # then open http://localhost:8000
```

`data.generated.js` is not committed. CI rebuilds it and publishes it as a
Pages artifact, so the full dataset isn't rewritten into git history on every
run.

The files currently under `results/` are **sample data** from
`tools/make-seed-data.py`, so the dashboard has something to show before the
test-infra side is wired up. Delete them once real results arrive.

## Related

- [`drasi-project/test-infra`](https://github.com/drasi-project/test-infra) — the E2E framework and the workflows that produce these results
- [`drasi-project/drasi-server`](https://github.com/drasi-project/drasi-server) — the system under test

## License

Apache-2.0. See [LICENSE](LICENSE).
