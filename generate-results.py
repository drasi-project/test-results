#!/usr/bin/env python3
"""Compile every result file into data.generated.js for the dashboard.

Unlike ClickBench's generate-results.sh (which keeps only the latest run per
system, because it compares systems at one instant), this keeps FULL history:
the whole point here is the trend over time.

The output is NOT committed. It is built inside the Pages workflow and shipped
as the Pages artifact. Committing a file that is rewritten on every run would
add far more git history than the underlying data itself.

Usage:
    python3 generate-results.py [-o data.generated.js]
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "results"
SCENARIOS = ROOT / "scenarios"


def load_templates():
    """Presentation metadata, joined at build time.

    Kept out of the result files on purpose: with ten years of history,
    denormalised display names would mean rewriting thousands of files to fix
    a single typo.
    """
    templates = {}
    if not SCENARIOS.is_dir():
        return templates
    for path in sorted(SCENARIOS.glob("*/template.json")):
        try:
            templates[path.parent.name] = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            print(f"warning: skipping {path}: {exc}", file=sys.stderr)
    return templates


def flatten(record, source):
    """One run record -> one row per reaction, plus a run-level row.

    Storage grain (one file per CI job) deliberately differs from chart grain
    (one point per series). Reactions within a run have different record
    counts, so they must stay separable.
    """
    run = record.get("run") or {}
    dims = record.get("dimensions") or {}
    versions = record.get("versions") or {}

    base = {
        "run_id": run.get("run_id"),
        "attempt": run.get("run_attempt"),
        "ts": run.get("started_at"),
        "workflow": run.get("workflow"),
        "trigger": run.get("trigger"),
        "runner": run.get("runner"),
        "url": run.get("url"),
        "scenario": dims.get("scenario"),
        "variant": dims.get("variant"),
        "target": dims.get("target"),
        "transport": dims.get("transport"),
        "server_version": versions.get("drasi_server_version"),
        "server_tag": versions.get("drasi_server_tag"),
        "infra_sha": versions.get("test_infra_sha"),
        "status": record.get("status"),
        "determinism": record.get("determinism"),
        "source": source,
    }

    rows = []
    totals = record.get("totals") or {}
    run_row = dict(base)
    run_row.update(
        {
            "reaction": None,
            "records": totals.get("records"),
            "duration_s": totals.get("duration_s"),
            "records_per_sec": totals.get("records_per_sec"),
        }
    )
    rows.append(run_row)

    for reaction in record.get("reactions") or []:
        if not isinstance(reaction, dict):
            continue
        row = dict(base)
        row.update(
            {
                "reaction": reaction.get("reaction_id"),
                "records": reaction.get("records"),
                "expected_records": reaction.get("expected_records"),
                "duration_s": reaction.get("duration_s"),
                "records_per_sec": reaction.get("records_per_sec"),
                "determinism": reaction.get("determinism", record.get("determinism")),
                "sha256": reaction.get("sha256"),
                "error_message": reaction.get("error_message"),
            }
        )
        rows.append(row)
    return rows


def main():
    parser = argparse.ArgumentParser(description="Build data.generated.js from results/.")
    parser.add_argument("-o", "--output", default="data.generated.js")
    args = parser.parse_args()

    if not RESULTS.is_dir():
        print("error: results/ not found", file=sys.stderr)
        return 1

    rows = []
    files = 0
    skipped = 0
    for path in sorted(RESULTS.rglob("*.json")):
        try:
            record = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            print(f"warning: skipping {path}: {exc}", file=sys.stderr)
            skipped += 1
            continue
        if not isinstance(record, dict):
            skipped += 1
            continue
        files += 1
        rows.extend(flatten(record, str(path.relative_to(ROOT))))

    # Chronological, so the site can render without re-sorting.
    rows.sort(key=lambda r: (r.get("ts") or "", r.get("scenario") or "", r.get("variant") or "", r.get("reaction") or ""))

    # Drop null values: at this row count it is a big saving for free, and the
    # site already treats missing as absent rather than zero.
    compact = [{k: v for k, v in row.items() if v is not None} for row in rows]

    templates = load_templates()
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as handle:
        handle.write("// Generated by generate-results.py. Do not edit, do not commit.\n")
        handle.write(f"const meta = {json.dumps(templates, sort_keys=True)};\n")
        handle.write("const data = [\n")
        for i, row in enumerate(compact):
            handle.write(json.dumps(row, sort_keys=True))
            handle.write(",\n" if i < len(compact) - 1 else "\n")
        handle.write("];\n")

    size_kb = out.stat().st_size / 1024
    print(f"{files} result file(s) -> {len(compact)} row(s), {size_kb:.1f} KB -> {out}")
    if skipped:
        print(f"skipped {skipped} unreadable file(s)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
