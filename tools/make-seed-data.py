#!/usr/bin/env python3
"""One-off generator for the sample result files under results/.

NOT part of CI, and NOT how real data arrives. Real records are produced by a
GitHub Action in drasi-project/test-infra and pushed here (see README.md).
This script only exists so the dashboard has a realistic history to render
before that action is wired up. Safe to delete once real data accumulates.
"""

import json
import math
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "results"

random.seed(20260817)

VARIANTS = [
    {
        "scenario": "building_comfort",
        "variant": "drasi_server_http",
        "target": "drasi_server",
        "transport": "http",
        "workflow": "e2e-building-comfort.yml",
        "reactions": [("building-comfort", 99981), ("building-comfort-floor-agg", 49860)],
        "base_rps": 2430.0,
        "determinism": True,
    },
    {
        "scenario": "building_comfort",
        "variant": "drasi_server_grpc",
        "target": "drasi_server",
        "transport": "grpc",
        "workflow": "e2e-building-comfort.yml",
        "reactions": [("building-comfort", 99981), ("building-comfort-floor-agg", 49860)],
        "base_rps": 2810.0,
        "determinism": True,
    },
    {
        "scenario": "building_comfort",
        "variant": "drasi_lib",
        "target": "drasi_lib",
        "transport": "in_process",
        "workflow": "e2e-building-comfort.yml",
        "reactions": [("building-comfort", 95000), ("building-comfort-floor-agg", 45000)],
        "base_rps": 5120.0,
        "determinism": True,
    },
    {
        "scenario": "stock_market",
        "variant": "drasi_server_http_grpc_join",
        "target": "drasi_server",
        "transport": "http_grpc",
        "workflow": "e2e-stock-market-join.yml",
        "reactions": [("watchlist-prices", 75000)],
        "base_rps": 1840.0,
        # stock_market has no Sha256Determinism handler upstream.
        "determinism": False,
    },
]

# (first_day_index, tag, sha). A real throughput regression lands with v0.1.6
# so the dashboard's version markers have something meaningful to explain.
RELEASES = [
    (0, "v0.1.4", "3f1c9a77d2b4e86051aa77c3d9e2b1f4a6c8d0e2"),
    (9, "v0.1.5", "7dc6281f4a93c05e1b8d47f2a0c96e3b5d81f4a7"),
    (17, "v0.1.6", "b24e9f01c7a5d38e62f4901ab3c7d5e8f2016b93"),
]

DAYS = 24
START = datetime(2026, 7, 25, 7, 0, 0, tzinfo=timezone.utc)


def release_for(day):
    chosen = RELEASES[0]
    for rel in RELEASES:
        if day >= rel[0]:
            chosen = rel
    return chosen


def rps_factor(spec, day):
    """Throughput multiplier: a v0.1.6 regression on drasi_server, plus drift."""
    factor = 1.0
    if day >= 17 and spec["target"] == "drasi_server":
        factor *= 0.78
    factor *= 1.0 + 0.004 * min(day, 16)
    return factor


def build(day, spec, run_counter):
    day_start = START + timedelta(days=day)
    if spec["scenario"] == "stock_market":
        day_start += timedelta(minutes=15)

    _, tag, sha = release_for(day)
    run_id = str(17400000000 + run_counter * 1237)

    # A hard failure and a determinism failure, on separate days.
    failed = day == 12 and spec["variant"] == "drasi_server_grpc"
    det_failed = day == 20 and spec["variant"] == "drasi_lib"

    jitter = random.uniform(-0.035, 0.035)
    rps = spec["base_rps"] * rps_factor(spec, day) * (1 + jitter)

    reactions = []
    total_records = 0
    max_duration = 0.0

    for reaction_id, expected in spec["reactions"]:
        if failed:
            reactions.append(
                {
                    "reaction_id": reaction_id,
                    "status": "Error",
                    "records": 21447,
                    "expected_records": expected,
                    "duration_s": None,
                    "records_per_sec": None,
                    "determinism": "not_applicable",
                    "sha256": None,
                    "error_message": "reaction handler stopped responding after 21447 records",
                }
            )
            continue

        # The aggregate reaction emits fewer records but shares the pipeline.
        share = 1.0 if "floor-agg" not in reaction_id else 0.62
        reaction_rps = rps * share
        duration = round(expected / reaction_rps, 2)
        total_records += expected
        max_duration = max(max_duration, duration)

        if not spec["determinism"]:
            det, sha256 = "not_applicable", None
        elif det_failed and reaction_id == "building-comfort":
            det, sha256 = "fail", "9c02f7ab41de"
        else:
            det = "pass"
            sha256 = "3b930107b012" if "floor-agg" not in reaction_id else "a71f4c8d9e02"

        reactions.append(
            {
                "reaction_id": reaction_id,
                "status": "Stopped",
                "records": expected,
                "expected_records": expected,
                "duration_s": duration,
                "records_per_sec": round(reaction_rps, 1),
                "determinism": det,
                "sha256": sha256,
            }
        )

    if failed:
        status, determinism, totals = "failure", "not_applicable", {}
        finished = day_start + timedelta(minutes=31)
    else:
        status = "success"
        if not spec["determinism"]:
            determinism = "not_applicable"
        elif det_failed:
            determinism = "fail"
        else:
            determinism = "pass"
        totals = {
            "records": total_records,
            "duration_s": round(max_duration, 2),
            "records_per_sec": round(total_records / max_duration, 1),
        }
        finished = day_start + timedelta(seconds=math.ceil(max_duration) + 180)

    return {
        "schema_version": 1,
        "run": {
            "run_id": run_id,
            "run_attempt": 1,
            "workflow": spec["workflow"],
            "trigger": "schedule",
            "runner": "ubuntu-latest",
            "started_at": day_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "finished_at": finished.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "url": f"https://github.com/drasi-project/test-infra/actions/runs/{run_id}",
        },
        "versions": {
            "test_infra_sha": sha,
            "drasi_server_tag": None if spec["target"] == "drasi_lib" else tag,
            "drasi_server_build": None if spec["target"] == "drasi_lib" else f"drasi-server {tag[1:]}",
        },
        "dimensions": {
            "scenario": spec["scenario"],
            "variant": spec["variant"],
            "target": spec["target"],
            "transport": spec["transport"],
        },
        "params": {"change_count": 100000, "seed": 123456789},
        "status": status,
        "determinism": determinism,
        "reactions": reactions,
        "totals": totals,
    }


def main():
    counter = 0
    written = 0
    for day in range(DAYS):
        for spec in VARIANTS:
            counter += 1
            # A couple of gaps, because real CI history has them.
            if day == 6 and spec["scenario"] == "stock_market":
                continue
            record = build(day, spec, counter)
            started = record["run"]["started_at"]
            out = (
                RESULTS
                / started[0:4]
                / started[5:7]
                / started[8:10]
                / f"{spec['scenario']}__{spec['variant']}__{record['run']['run_id']}.json"
            )
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(record, indent=2) + "\n")
            written += 1
    print(f"wrote {written} seed result files")


if __name__ == "__main__":
    main()
