#!/usr/bin/env python3
"""Validate every result file under results/.

This script IS the schema contract. test-infra fetches and runs it against a
freshly-built summary before pushing, so malformed records fail where the
author can see them rather than landing on main. CI here re-runs it as a
backstop.

Design rules:
  * A small CORE of fields is checked strictly.
  * Unknown fields are always allowed, everywhere. New dimensions and params
    must be addable without a schema_version bump and without this validator
    rejecting them.
  * Errors block; warnings are reported but exit 0.

Usage:
    python3 validate-results.py [path ...]      # default: results/
    python3 validate-results.py --strict        # warnings become errors
"""

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent

SCHEMA_VERSIONS = {1}
STATUSES = {"success", "failure", "timeout"}
DETERMINISM = {"pass", "fail", "not_applicable"}
REACTION_STATUSES = {"Running", "Paused", "Stopped", "Error"}
TRIGGERS = {"schedule", "workflow_dispatch", "push", "pull_request"}

NAME_RE = re.compile(r"^[a-z0-9_]+$")
RUN_ID_RE = re.compile(r"^[0-9]+$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
# results/YYYY/MM/DD/<scenario>__<variant>__<run_id>.json
FILENAME_RE = re.compile(r"^([a-z0-9_]+)__([a-z0-9_]+)__([0-9]+)\.json$")


class Report:
    def __init__(self):
        self.errors = []
        self.warnings = []

    def error(self, path, msg):
        self.errors.append(f"{path}: {msg}")

    def warn(self, path, msg):
        self.warnings.append(f"{path}: {msg}")


def is_num(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def check_ts(rep, path, value, field):
    if not isinstance(value, str) or not TS_RE.match(value):
        rep.error(path, f"{field} must be UTC 'YYYY-MM-DDTHH:MM:SSZ', got {value!r}")
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        rep.error(path, f"{field} is not a real timestamp: {value!r}")
        return None


def validate_run(rep, path, run, expected_run_id):
    if not isinstance(run, dict):
        rep.error(path, "run must be an object")
        return None

    run_id = run.get("run_id")
    if not isinstance(run_id, str) or not RUN_ID_RE.match(run_id):
        rep.error(path, f"run.run_id must be a numeric string, got {run_id!r}")
    elif expected_run_id is not None and run_id != expected_run_id:
        rep.error(path, f"run.run_id {run_id!r} disagrees with filename {expected_run_id!r}")

    attempt = run.get("run_attempt")
    if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
        rep.error(path, f"run.run_attempt must be an integer >= 1, got {attempt!r}")

    for field in ("workflow", "runner"):
        if not isinstance(run.get(field), str) or not run[field].strip():
            rep.error(path, f"run.{field} must be a non-empty string")

    trigger = run.get("trigger")
    if trigger is not None and trigger not in TRIGGERS:
        rep.warn(path, f"run.trigger {trigger!r} not one of {sorted(TRIGGERS)}")

    started = check_ts(rep, path, run.get("started_at"), "run.started_at")
    finished_raw = run.get("finished_at")
    if finished_raw is not None:
        finished = check_ts(rep, path, finished_raw, "run.finished_at")
        if started and finished and finished < started:
            rep.error(path, "run.finished_at is before run.started_at")
    return started


def validate_reactions(rep, path, reactions, run_determinism):
    if not isinstance(reactions, list):
        rep.error(path, "reactions must be an array")
        return
    if not reactions:
        rep.warn(path, "reactions is empty")

    seen = set()
    any_fail = False
    for i, reaction in enumerate(reactions):
        label = f"reactions[{i}]"
        if not isinstance(reaction, dict):
            rep.error(path, f"{label} must be an object")
            continue

        rid = reaction.get("reaction_id")
        if not isinstance(rid, str) or not rid.strip():
            rep.error(path, f"{label}.reaction_id must be a non-empty string")
        else:
            if rid in seen:
                rep.error(path, f"duplicate reaction_id {rid!r}")
            seen.add(rid)
            label = f"reactions[{rid}]"

        if reaction.get("status") not in REACTION_STATUSES:
            rep.error(
                path,
                f"{label}.status must be one of {sorted(REACTION_STATUSES)}, got {reaction.get('status')!r}",
            )

        for field in ("records", "expected_records"):
            value = reaction.get(field)
            if value is None:
                continue
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                rep.error(path, f"{label}.{field} must be a non-negative integer, got {value!r}")

        for field in ("duration_s", "records_per_sec"):
            value = reaction.get(field)
            if value is None:
                continue
            if not is_num(value) or value < 0:
                rep.error(path, f"{label}.{field} must be a non-negative number, got {value!r}")

        det = reaction.get("determinism")
        if det is not None and det not in DETERMINISM:
            rep.error(path, f"{label}.determinism must be one of {sorted(DETERMINISM)}, got {det!r}")
        if det == "fail":
            any_fail = True

        # Cross-check the derived rate, which is the field most likely to be
        # miscomputed by a hand-written emitter.
        records = reaction.get("records")
        duration = reaction.get("duration_s")
        rate = reaction.get("records_per_sec")
        if is_num(rate) and is_num(duration) and isinstance(records, int) and duration > 0:
            expected = records / duration
            if expected > 0 and abs(expected - rate) / expected > 0.02:
                rep.warn(
                    path,
                    f"{label}.records_per_sec {rate} disagrees with records/duration_s "
                    f"({expected:.1f}) by more than 2%",
                )

        records = reaction.get("records")
        expected_records = reaction.get("expected_records")
        if isinstance(records, int) and isinstance(expected_records, int):
            if reaction.get("status") == "Stopped" and records < expected_records:
                rep.warn(
                    path,
                    f"{label} stopped with {records} of {expected_records} expected records "
                    "(truncated run: throughput will look better than it was)",
                )

    if any_fail and run_determinism == "pass":
        rep.error(path, "run determinism is 'pass' but a reaction reports 'fail'")


def validate_file(rep, path, rel):
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        rep.error(rel, f"unreadable JSON: {exc}")
        return
    if not isinstance(data, dict):
        rep.error(rel, "top level must be an object")
        return

    parts = rel.parts
    expected_scenario = expected_variant = expected_run_id = None
    if len(parts) == 5 and parts[0] == "results":
        _, year, month, day, filename = parts
        match = FILENAME_RE.match(filename)
        if not match:
            rep.error(rel, "filename must be <scenario>__<variant>__<run_id>.json")
        else:
            expected_scenario, expected_variant, expected_run_id = match.groups()
        for value, field, width in ((year, "year", 4), (month, "month", 2), (day, "day", 2)):
            if not (value.isdigit() and len(value) == width):
                rep.error(rel, f"path {field} segment {value!r} is malformed")
    else:
        rep.error(rel, "results files must live at results/YYYY/MM/DD/<file>.json")

    version = data.get("schema_version")
    if version not in SCHEMA_VERSIONS:
        rep.error(rel, f"schema_version must be one of {sorted(SCHEMA_VERSIONS)}, got {version!r}")
        return

    started = validate_run(rep, rel, data.get("run"), expected_run_id)

    # The date directory must agree with the run timestamp, or the file is
    # invisible to any date-range query.
    if started and len(parts) == 5:
        actual = f"{parts[1]}/{parts[2]}/{parts[3]}"
        expected = started.strftime("%Y/%m/%d")
        if actual != expected:
            rep.error(rel, f"path date {actual} disagrees with run.started_at date {expected}")

    versions = data.get("versions")
    if not isinstance(versions, dict):
        rep.error(rel, "versions must be an object")
    else:
        sha = versions.get("test_infra_sha")
        if not isinstance(sha, str) or not SHA_RE.match(sha):
            rep.error(rel, f"versions.test_infra_sha must be a full 40-char hex sha, got {sha!r}")

    dims = data.get("dimensions")
    if not isinstance(dims, dict):
        rep.error(rel, "dimensions must be an object")
    else:
        for field in ("scenario", "variant", "target", "transport"):
            value = dims.get(field)
            if value is None:
                if field in ("scenario", "variant"):
                    rep.error(rel, f"dimensions.{field} is required")
                else:
                    rep.warn(rel, f"dimensions.{field} is missing (charts can't facet on it)")
            elif not isinstance(value, str) or not NAME_RE.match(value):
                rep.error(rel, f"dimensions.{field} must match [a-z0-9_]+, got {value!r}")

        if expected_scenario and dims.get("scenario") not in (None, expected_scenario):
            rep.error(rel, f"dimensions.scenario disagrees with filename {expected_scenario!r}")
        if expected_variant and dims.get("variant") not in (None, expected_variant):
            rep.error(rel, f"dimensions.variant disagrees with filename {expected_variant!r}")

        scenario = dims.get("scenario")
        variant = dims.get("variant")
        if isinstance(scenario, str) and isinstance(variant, str):
            template = ROOT / "scenarios" / scenario / "template.json"
            if not template.exists():
                rep.warn(rel, f"no scenarios/{scenario}/template.json (site will fall back to raw names)")
            else:
                try:
                    meta = json.loads(template.read_text())
                    if variant not in meta.get("variants", {}):
                        rep.warn(rel, f"variant {variant!r} not described in scenarios/{scenario}/template.json")
                except (OSError, json.JSONDecodeError) as exc:
                    rep.error(rel, f"scenarios/{scenario}/template.json is unreadable: {exc}")

    if "params" in data and not isinstance(data["params"], dict):
        rep.error(rel, "params must be an object when present")

    status = data.get("status")
    if status not in STATUSES:
        rep.error(rel, f"status must be one of {sorted(STATUSES)}, got {status!r}")

    determinism = data.get("determinism")
    if determinism not in DETERMINISM:
        rep.error(rel, f"determinism must be one of {sorted(DETERMINISM)}, got {determinism!r}")

    validate_reactions(rep, rel, data.get("reactions"), determinism)

    totals = data.get("totals")
    if totals is not None and not isinstance(totals, dict):
        rep.error(rel, "totals must be an object when present")
    elif isinstance(totals, dict) and totals:
        for field in ("records",):
            value = totals.get(field)
            if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
                rep.error(rel, f"totals.{field} must be a non-negative integer, got {value!r}")
        for field in ("duration_s", "records_per_sec"):
            value = totals.get(field)
            if value is not None and (not is_num(value) or value < 0):
                rep.error(rel, f"totals.{field} must be a non-negative number, got {value!r}")
    elif status == "success" and not totals:
        rep.warn(rel, "successful run has no totals (it will not appear on the run-level trend chart)")


def collect(targets):
    files = []
    for target in targets:
        path = Path(target)
        if path.is_dir():
            files.extend(sorted(path.rglob("*.json")))
        elif path.is_file():
            files.append(path)
    return files


def main():
    parser = argparse.ArgumentParser(description="Validate Drasi E2E summary result files.")
    parser.add_argument("paths", nargs="*", default=["results"])
    parser.add_argument("--strict", action="store_true", help="treat warnings as errors")
    args = parser.parse_args()

    files = collect(args.paths or ["results"])
    rep = Report()
    for path in files:
        try:
            rel = path.resolve().relative_to(ROOT)
        except ValueError:
            rel = path
        validate_file(rep, path, rel)

    for warning in rep.warnings:
        print(f"warning: {warning}")
    for error in rep.errors:
        print(f"error: {error}", file=sys.stderr)

    print(f"\nchecked {len(files)} file(s): {len(rep.errors)} error(s), {len(rep.warnings)} warning(s)")

    if rep.errors or (args.strict and rep.warnings):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
