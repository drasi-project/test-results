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
tools/backfill-from-artifacts.py                          # artifacts -> result records
```

One file per CI job, and files are never edited after they land. Because each
job writes its own distinct path, two jobs can never touch the same file.

The date leads the path so a date-range query only has to look at the relevant
directories, and so old years could be archived later without disturbing
anything else. Dimensions that will grow over time (index config, query
complexity, workload params) live *inside* the JSON, not in the path — adding
one must not change the path grammar or break existing readers.

Display names live in `scenarios/<scenario>/template.json` and are joined in at
build time, so they aren't copied into every result file.

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

**1. Build one record per job.** Any file under `results/` is a working example
of the shape. Source fields from the run's artifacts:

| Record field | Source |
|---|---|
| `reactions[].records` | `PerformanceMetrics.record_count` |
| `reactions[].duration_s` | `PerformanceMetrics.duration_ns / 1e9` |
| `reactions[].records_per_sec` | `PerformanceMetrics.records_per_second` |
| `reactions[].status` | `final_reaction_state__<id>.json` → `.reaction_observer.status` |
| `reactions[].sha256` | `.reaction_observer.logger_results[]` where `logger_name == "DeterminismHash"` → `.summary.sha256` |
| `reactions[].determinism` | `determinism_verdict.json` → `.results[<reaction_id>].passed` |
| `run.*` | GitHub Actions context |
| `versions.drasi_server_version` | `$server_version` in `run_test_ci.sh` — the same value the step summary prints as "drasi-server binary" |
| `versions.drasi_server_tag` | `$drasi_version` in `run_test_ci.sh` — the requested release tag |
| `params.*` | the variant's `config.json` |

Five traps in that mapping:

- **Do not use `result_summary.observer_runtime_s` for duration.** It is a
  human-readable string like `"5.2 minutes"`. Duration must come from
  `PerformanceMetrics.duration_ns`.
- **The two artifacts key reactions differently.** `determinism_verdict.json`
  uses the bare id (`building-comfort`); `PerformanceMetrics.test_run_reaction_id`
  uses the dotted form
  (`drasi_server_dev_repo.building_comfort.test_run_001.building-comfort`).
  Join on the last dotted segment. Records here always use the **bare** id.
- **Record the binary version, not just the tag.** The tag is normally the
  moving pointer `latest`, which never changes, so a dashboard keyed on it
  would never show a version boundary. `run_test_ci.sh` already computes both
  for the step summary; reuse them. `$server_version` looks like
  `drasi-server 0.2.1` — store the version part (`0.2.1`). It falls back to
  the literal string `unknown` when `--version` fails, so handle that.
- **Emit `determinism: "not_applicable"`** for scenarios with no determinism
  handler (`stock_market`). Never report a fabricated `pass` — the validator
  rejects a run-level `pass` that contradicts a failing reaction.
- **`totals.duration_s` is wall clock** (latest end minus earliest start), not
  a sum of per-reaction durations, or `totals.records_per_sec` becomes a
  meaningless sum of rates.

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

**5. Authenticate with a GitHub App.** `GITHUB_TOKEN` cannot be used: it is
scoped to the repository running the workflow, so a workflow in test-infra has
no access here even though both repos are in the same org.

A GitHub App is used rather than a deploy key because of the org ruleset (see
below): an App is referenced in a ruleset bypass list by its own id, so the
exemption covers exactly one writer. Deploy keys are per repository and cannot
be singled out that way at org level.

Setup, once:

1. **Create the App** — org Settings → Developer settings → GitHub Apps → New.
   Name it something like `drasi-test-results-writer`. Uncheck WebHook →
   Active. Under Repository permissions set **Contents: Read and write**
   (nothing else). Where can this App be installed: *Only on this account*.
2. **Generate a private key** on the App's page and download the `.pem`.
3. **Install the App** — App page → Install App → drasi-project → *Only select
   repositories* → `test-results`.
4. **Add the credentials to `test-infra`** (Settings → Secrets and variables →
   Actions). The App ID is a **variable**, `TEST_RESULTS_APP_ID` — it is a
   public identifier, not a secret. The private key is a **secret**,
   `TEST_RESULTS_APP_PRIVATE_KEY`, holding the whole `.pem` including the
   `-----BEGIN...` and `-----END...` lines.
5. **Add the App to the ruleset bypass list** — org Settings → Repository →
   Rulesets → `drasi-org-main` → Bypass list → Add bypass → select the App.
   Without this the push is rejected: that ruleset protects the default branch
   of every repo in the org, requires a pull request with an approving review
   plus a DCO check, and ships with `OrganizationAdmin` as its only bypass
   actor.

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

### Backfilling from existing runs

Until the test-infra push step exists, results can be backfilled from artifacts
that are still on GitHub. Artifacts expire after 90 days, so this only reaches
back that far.

```bash
gh run download <run_id> --repo drasi-project/test-infra --dir /tmp/arts/<run_id>
python3 tools/backfill-from-artifacts.py /tmp/arts/<run_id>
```

`summarize_job` in that script is the reference implementation of the emitter
described above — it already handles the reaction-id join, the wall-clock
totals and the `not_applicable` determinism case, so port it rather than
rewriting the mapping from scratch. Use `--dry-run` to preview.

## Related

- [`drasi-project/test-infra`](https://github.com/drasi-project/test-infra) — the E2E framework and the workflows that produce these results
- [`drasi-project/drasi-server`](https://github.com/drasi-project/drasi-server) — the system under test

## License

Apache-2.0. See [LICENSE](LICENSE).
