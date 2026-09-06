"""Move the two US `raw_proj_*` datasets to EU (AI-314), never losing a row.

Usage (from server/, with ADC on the toorow project):
    TOOROW_GCP_PROJECT=<project> \
        uv run python ../scripts/ops/move_raw_datasets_to_eu.py <dataset_id> [...]
    # Every dataset is named on the command line; running without one refuses.
    # (The 2026-08-31 cutover names live in the story log, not in this file.)

    (An earlier docstring promised stage/cutover phases that were never the
    CLI -- passing 'stage' looked up a dataset named stage and 404'd,
    measured 2026-08-31. Each move() IS staged internally: verified copy
    first, the destructive step only after counts match.)

A dataset cannot change location. For each dataset:
  1. create `<name>__eu_move` in EU, stream every table's rows into it with the
     source schema (WRITE_TRUNCATE), verify row counts table by table;
  2. only then delete the US dataset;
  3. create `<name>` in EU, copy every table from the staging dataset with
     in-region copy jobs, verify counts;
  4. delete the staging dataset.
At every step the rows exist in at least one dataset. Any mismatch aborts
BEFORE the destructive step.
"""

from __future__ import annotations

import datetime as dt
import decimal
import json

# No production identifier lives in the repo (CLAUDE.md): the GCP project comes
# from the environment and every dataset is named on the command line. The two
# datasets this script was written for were moved on 2026-08-31 (AI-314); the
# argument-only form is what a future move must use.
import os
import sys

from google.cloud import bigquery

PROJECT = os.environ.get("TOOROW_GCP_PROJECT") or ""
if not PROJECT:
    sys.exit("refusing: set TOOROW_GCP_PROJECT to the GCP project that owns the datasets")
LOCATION = "EU"
DATASETS: list[str] = []  # filled from argv only; there is no default list
client = bigquery.Client(project=PROJECT)


def _json_safe(value):
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    return value


def _counts(dataset_id: str) -> dict[str, int]:
    out = {}
    for t in client.list_tables(dataset_id):
        out[t.table_id] = int(client.get_table(t.reference).num_rows or 0)
    return out


def _stream_copy(src_ds: str, dst_ds: str) -> None:
    for t in client.list_tables(src_ds):
        src = client.get_table(t.reference)
        rows = [_json_safe(dict(r)) for r in client.list_rows(src)]
        dst_ref = f"{PROJECT}.{dst_ds}.{src.table_id}"
        job = client.load_table_from_json(
            rows,
            dst_ref,
            job_config=bigquery.LoadJobConfig(
                schema=src.schema,
                write_disposition="WRITE_TRUNCATE",
            ),
            location=LOCATION,
        )
        job.result()
        got = int(client.get_table(dst_ref).num_rows or 0)
        print(f"  streamed {src.table_id}: {len(rows)} rows -> {got}", flush=True)
        if got != int(src.num_rows or 0):
            raise SystemExit(f"ABORT: {src.table_id} {src.num_rows} != {got}")


def _copy_jobs(src_ds: str, dst_ds: str) -> None:
    for t in client.list_tables(src_ds):
        job = client.copy_table(
            f"{PROJECT}.{src_ds}.{t.table_id}",
            f"{PROJECT}.{dst_ds}.{t.table_id}",
            location=LOCATION,
            job_config=bigquery.CopyJobConfig(write_disposition="WRITE_TRUNCATE"),
        )
        job.result()
        print(f"  copied {t.table_id}", flush=True)


def move(dataset_id: str) -> None:
    ds = client.get_dataset(dataset_id)
    print(f"== {dataset_id} location={ds.location}", flush=True)
    if ds.location == LOCATION:
        print("  already EU, nothing to do", flush=True)
        return
    before = _counts(dataset_id)
    print("  source:", json.dumps(before), flush=True)

    staging = f"{dataset_id}__eu_move"
    stg = bigquery.Dataset(f"{PROJECT}.{staging}")
    stg.location = LOCATION
    client.create_dataset(stg, exists_ok=True)
    _stream_copy(dataset_id, staging)
    staged = _counts(staging)
    if staged != before:
        raise SystemExit(f"ABORT before delete: staged {staged} != source {before}")
    print("  staging verified identical; deleting US dataset", flush=True)

    client.delete_dataset(f"{PROJECT}.{dataset_id}", delete_contents=True)

    final = bigquery.Dataset(f"{PROJECT}.{dataset_id}")
    final.location = LOCATION
    client.create_dataset(final, exists_ok=True)
    _copy_jobs(staging, dataset_id)
    after = _counts(dataset_id)
    if after != before:
        raise SystemExit(f"KEEP STAGING: final {after} != source {before}")
    print("  final verified identical; dropping staging", flush=True)
    client.delete_dataset(f"{PROJECT}.{staging}", delete_contents=True)
    print(f"  DONE {dataset_id} -> {client.get_dataset(dataset_id).location}", flush=True)


if __name__ == "__main__":
    if not sys.argv[1:]:
        sys.exit(
            "refusing: name the dataset(s) to move on the command line -- there is no default list"
        )
    for d in sys.argv[1:]:
        move(d)
