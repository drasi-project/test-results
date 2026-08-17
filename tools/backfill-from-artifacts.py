#!/usr/bin/env python3
"""Build summary records from real test-infra run artifacts.

This is a backfill tool: it turns artifacts that already exist in
drasi-project/test-infra into the summary records stored under results/.

It doubles as the reference implementation of the emitter that test-infra
needs. The `summarize_job` function below is the part worth porting: given one
job's artifact directory plus its GitHub metadata, it produces exactly the
record this repo expects.

Usage:
    # download artifacts first (they expire after 90 days)
    gh run download <run_id> --repo drasi-project/test-infra --dir /tmp/arts/<run_id>

    python3 tools/backfill-from-artifacts.py /tmp/arts/<run_id> [...]

Each positional argument is a directory named after the run id, containing one
subdirectory per uploaded artifact (`<scenario>-<variant>`).
"""

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "results"
REPO = "drasi-project/test-infra"

# Transport and target are derived from the variant name, which is the CI
# directory name in test-infra and the only place these are spelled out.
VARIANT_FACETS = {
    "drasi_server_http": ("drasi_server", "http"),
    "drasi_server_grpc": ("drasi_server", "grpc"),
    "drasi_server_http_grpc_join": ("drasi_server", "http_grpc"),
    "drasi_lib": ("drasi_lib", "in_process"),
}

WORKFLOW_FOR_SCENARIO = {
    "building_comfort": "e2e-building-comfort.yml",
    "stock_market": "e2e-stock-market-join.yml",
}

VERSION_RE = re.compile(r"^\s*Version:\s*(\S+)\s*$", re.MULTILINE)


def gh_json(*args):
    result = subprocess.run(["gh", *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


def run_metadata(run_id):
    data = gh_json(
        "run", "view", str(run_id), "--repo", REPO,
        "--json", "createdAt,startedAt,updatedAt,event,headSha,attempt,jobs,workflowName",
    )
    jobs = {}
    for job in data.get("jobs", []):
        # Job names look like "building_comfort / drasi_server_http".
        name = job.get("name", "")
        if "/" in name:
            key = name.split("/")[-1].strip()
            jobs[key] = job
    data["_jobs_by_variant"] = jobs
    return data


def read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return None


def find_perf_metrics(artifact_dir):
    """Map bare reaction id -> PerformanceMetrics payload.

    PerformanceMetrics keys reactions by the dotted test_run_reaction_id
    (`<repo>.<test>.<run>.<reaction>`), while determinism_verdict.json and the
    reaction-state filenames use the bare id. Join on the last dotted segment.
    """
    metrics = {}
    for path in artifact_dir.rglob("performance_metrics/*.json"):
        payload = read_json(path)
        if not payload:
            continue
        dotted = payload.get("test_run_reaction_id", "")
        bare = dotted.rsplit(".", 1)[-1] if dotted else None
        if not bare:
            continue
        # Several files can exist if a run was retried; keep the newest.
        previous = metrics.get(bare)
        if previous is None or payload.get("timestamp", "") > previous.get("timestamp", ""):
            metrics[bare] = payload
    return metrics


def find_determinism(artifact_dir):
    """Return the per-reaction verdict map, or None if the scenario has none.

    None and {} mean different things: None means this scenario has no
    determinism handler at all (stock_market), which must be reported as
    not_applicable rather than as a pass.
    """
    candidates = sorted(artifact_dir.rglob("determinism_verdict.json"))
    for path in candidates:
        payload = read_json(path)
        if payload and isinstance(payload.get("results"), dict):
            return payload["results"]
    return None


def find_server_version(artifact_dir):
    """drasi-server prints a banner with its version at startup.

    Recorded because the release tag is normally the moving pointer `latest`,
    which never changes and so cannot identify a build.
    """
    log = artifact_dir / "logs" / "drasi-server.log"
    if not log.exists():
        return None
    try:
        text = log.read_text(errors="replace")
    except OSError:
        return None
    match = VERSION_RE.search(text)
    return match.group(1) if match else None


def summarize_job(artifact_dir, scenario, variant, run_meta):
    """Build one summary record from one job's artifacts. <- port this."""
    job = run_meta["_jobs_by_variant"].get(variant, {})
    target, transport = VARIANT_FACETS.get(variant, (None, None))

    perf = find_perf_metrics(artifact_dir)
    verdicts = find_determinism(artifact_dir)
    scenario_has_determinism = verdicts is not None

    reactions = []
    total_records = 0
    earliest_start = None
    latest_end = None

    for state_path in sorted(artifact_dir.glob("final_reaction_state__*.json")):
        reaction_id = state_path.name[len("final_reaction_state__"):-len(".json")]
        state = read_json(state_path) or {}
        observer = state.get("reaction_observer") or {}
        summary = observer.get("result_summary") or {}

        entry = {
            "reaction_id": reaction_id,
            "status": observer.get("status") or "Error",
        }

        records = summary.get("reaction_invocation_count")
        if isinstance(records, int):
            entry["records"] = records
            total_records += records

        metrics = perf.get(reaction_id)
        if metrics:
            duration_ns = metrics.get("duration_ns")
            if isinstance(duration_ns, int) and duration_ns > 0:
                # Never parse result_summary.observer_runtime_s: it is a human
                # string like "33.0 seconds".
                entry["duration_s"] = round(duration_ns / 1e9, 2)
            rate = metrics.get("records_per_second")
            if isinstance(rate, (int, float)):
                entry["records_per_sec"] = round(float(rate), 1)
            if isinstance(metrics.get("record_count"), int):
                entry["records"] = metrics["record_count"]
            start_ns, end_ns = metrics.get("start_time_ns"), metrics.get("end_time_ns")
            if isinstance(start_ns, int):
                earliest_start = start_ns if earliest_start is None else min(earliest_start, start_ns)
            if isinstance(end_ns, int):
                latest_end = end_ns if latest_end is None else max(latest_end, end_ns)

        for logger in observer.get("logger_results") or []:
            if logger.get("logger_name") == "DeterminismHash":
                sha = (logger.get("summary") or {}).get("sha256")
                if sha:
                    entry["sha256"] = sha

        if not scenario_has_determinism:
            entry["determinism"] = "not_applicable"
        else:
            verdict = verdicts.get(reaction_id)
            if verdict is None:
                entry["determinism"] = "not_applicable"
            else:
                entry["determinism"] = "pass" if verdict.get("passed") else "fail"

        if observer.get("error_message"):
            entry["error_message"] = observer["error_message"]

        reactions.append(entry)

    if not scenario_has_determinism:
        determinism = "not_applicable"
    elif any(r.get("determinism") == "fail" for r in reactions):
        determinism = "fail"
    elif any(r.get("determinism") == "pass" for r in reactions):
        determinism = "pass"
    else:
        determinism = "not_applicable"

    conclusion = job.get("conclusion")
    if conclusion == "success":
        status = "success"
    elif conclusion in ("cancelled", "timed_out"):
        status = "timeout"
    else:
        status = "failure"

    totals = {}
    if total_records:
        totals["records"] = total_records
    if earliest_start is not None and latest_end is not None and latest_end > earliest_start:
        # Wall clock across all reactions, not a sum of per-reaction durations,
        # so the aggregate rate stays meaningful.
        wall = (latest_end - earliest_start) / 1e9
        totals["duration_s"] = round(wall, 2)
        if total_records:
            totals["records_per_sec"] = round(total_records / wall, 1)

    started = job.get("startedAt") or run_meta.get("startedAt") or run_meta.get("createdAt")
    completed = job.get("completedAt") or run_meta.get("updatedAt")

    record = {
        "schema_version": 1,
        "run": {
            "run_id": str(run_meta["_run_id"]),
            "run_attempt": run_meta.get("attempt") or 1,
            "workflow": WORKFLOW_FOR_SCENARIO.get(scenario, run_meta.get("workflowName", "")),
            "trigger": "workflow_dispatch" if run_meta.get("event") == "workflow_dispatch" else run_meta.get("event", "schedule"),
            "runner": "ubuntu-latest",
            "started_at": started,
            "finished_at": completed,
            "url": f"https://github.com/{REPO}/actions/runs/{run_meta['_run_id']}",
        },
        "versions": {"test_infra_sha": run_meta.get("headSha", "")},
        "dimensions": {"scenario": scenario, "variant": variant},
        "status": status,
        "determinism": determinism,
        "reactions": reactions,
    }

    if target:
        record["dimensions"]["target"] = target
        record["dimensions"]["transport"] = transport

    server_version = find_server_version(artifact_dir)
    if server_version:
        record["versions"]["drasi_server_version"] = server_version
        # The tag actually requested. Normally the moving pointer `latest`.
        record["versions"]["drasi_server_tag"] = "latest"

    if totals:
        record["totals"] = totals

    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run_dirs", nargs="+", help="directories named <run_id> holding downloaded artifacts")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    written = 0
    for run_dir_name in args.run_dirs:
        run_dir = Path(run_dir_name)
        if not run_dir.is_dir():
            print(f"skip: {run_dir} is not a directory", file=sys.stderr)
            continue

        run_id = run_dir.name
        try:
            meta = run_metadata(run_id)
        except RuntimeError as exc:
            print(f"skip {run_id}: {exc}", file=sys.stderr)
            continue
        meta["_run_id"] = run_id

        for artifact_dir in sorted(p for p in run_dir.iterdir() if p.is_dir()):
            # Artifact names are "<scenario>-<variant>".
            name = artifact_dir.name
            if "-" not in name:
                continue
            scenario, variant = name.split("-", 1)

            record = summarize_job(artifact_dir, scenario, variant, meta)
            started = record["run"]["started_at"]
            if not started:
                print(f"skip {name}: no start time", file=sys.stderr)
                continue

            stamp = datetime.strptime(started, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            out = (
                RESULTS
                / f"{stamp.year:04d}" / f"{stamp.month:02d}" / f"{stamp.day:02d}"
                / f"{scenario}__{variant}__{run_id}.json"
            )
            if args.dry_run:
                print(f"would write {out.relative_to(ROOT)}")
                print(json.dumps(record, indent=2))
                continue

            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(record, indent=2) + "\n")
            written += 1
            print(f"wrote {out.relative_to(ROOT)}")

    if not args.dry_run:
        print(f"\n{written} record(s) written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
