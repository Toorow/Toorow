# BigQuery connector — rollout notes

## What this connector is for

Someone already has their data in BigQuery. This connector lets them point at
**their own table** and replicate it, rather than asking them to export a CSV or
to register the table in place. The field catalog is therefore not a fixed list:
a BigQuery table's fields ARE its columns, so discovery is the contract.

## The selector

Three choices, in order, each one made from what the credential genuinely returns
— never from an environment variable and never from a string typed blind.

1. **Which table** — `discover_accounts()` returns a walkable tree,
   `project → dataset → table`, in the generic hierarchy `core.account_topology`
   consumes (the shape `google-analytics` returns for account → property).

   Two things this got wrong first, both invisible when the function is called
   directly and only visible through core:

   - It returned `{"topology": ..., "accounts": [...]}`. Core does
     `accounts = discovery_fn(...)` and walks a **list**; a dict flattens to zero
     selectable accounts, so the picker comes back empty with nothing to explain
     why. It returns the list now.
   - It used `client.list_datasets()`, which covers only the client's **default**
     project. One credential commonly reaches several, and the table the person
     wants may be in none of them. Projects are enumerated first now.

   Only a leaf carries an `id`. A project and a dataset are groupings you
   navigate, not things you can pull from, and core stores every `id` it finds as
   a selectable account — so an id on a folder would offer a choice that can only
   fail later at `assert_safe_table_reference`.

2. **Which columns** — `describe_table()` returns the schema **and** a
   `candidates` block narrowing each of the three roles (date / value /
   breakdown) to the columns whose type can actually serve it. Offering every
   column as a date is how someone picks a STRING id and gets an unexplained
   empty pull.

   The catalog walks into STRUCTs (bounded to `_MAX_SCHEMA_DEPTH`). This is not a
   refinement: in a GCP billing export **every** dimension worth breaking cost
   down by is nested — `service.description`, `sku.description`, `project.id` —
   so a top-level-only selector offers cost per day and nothing to split it by,
   and looks entirely healthy doing it. A `RECORD` is listed as a grouping but is
   not selectable; a `REPEATED` field is an array, so neither it nor anything
   under it is offered, because reading a scalar out of one needs an `UNNEST`
   this connector deliberately does not emit.

3. **Which window** — bounded by the requested dates, always.

## Cost

BigQuery bills bytes scanned, so the estimate has to arrive **before** the scan.
`pull()` issues BigQuery's own dry-run job first — which plans the statement
without scanning a byte and is not billed — and reports it as `bytes_estimated`
for the FULL window, alongside `bytes_processed` for what actually ran. Reading
`total_bytes_processed` off the executed job cannot do this: that is the bill,
arriving after it is due.

The window parameter is typed to **match** the column (`_date_param_type`) rather
than cast onto it. A billing export's date column is a TIMESTAMP and the table is
partitioned on it; wrapping the column in a `CAST` to make a STRING comparison
work would discard partition pruning and scan the whole export on every pull.

## Injection surface

`assert_safe_table_reference` requires three bare-identifier parts, and
`quote_column_path` backticks a nested path **segment by segment** — backticking
the whole thing would name one column that happens to contain a dot. Nesting
widens what can be SELECTED without widening what can be INJECTED;
`test_selector.py` pins that in both directions.

## Live verification

Run against real tables on 2026-07-30 with the deployment's own credentials:

| Check | Result |
|---|---|
| `discover_accounts()` across the estate | 10 projects, 74 selectable tables, all three-part ids |
| `describe_table()` on a public table | 12 columns, roles classified |
| `pull(dry_run=True)`, 1 day, real values | 100 rows, 66 non-zero, max 2704 — matches the published figures |
| bytes priced before the scan | estimate returned, equal to the executed scan |
| identifier guard, live | `; DROP`, backtick-escape and two-part refs all refused |
| `pull(dry_run=True)` on the GCP billing export | valid SQL, TIMESTAMP parameter and nested breakdown accepted; 0 rows — the export was enabled recently and has not landed data yet |

Nothing was written by any of the above: `dry_run=True` returns the rows instead
of calling `_insert_raw_rows`, so nothing reaches raw, staging or the marts.

**Still outstanding for AI-13**: the billing export has to actually land rows
before a pull can be ratified against non-empty data. `public_catalog.verification`
stays `blocked` until then — the mechanism is proven live, the payload is not.

## Where the rows land

This connector is the first consumer of `core.raw_landing`, which is what made
`TOOROW_DB_MODE=bigquery` mean something. Before it, the mode was accepted and
not honoured: 66 connectors raised `unsupported db_mode` for anything but DuckDB
and `mirror_sync`'s BigQuery branch logged "deferred (Phase B)" and returned.

BigQuery has two ingestion paths and they are not interchangeable — a **load
job** is batched, unbilled for ingestion and minutes behind; **streaming** is
queryable in seconds and billed per byte. Picking one for the other is either a
real cost or a real staleness, so the mode is explicit: `TOOROW_BQ_WRITE_MODE`
sets the default, a caller may override per landing.

Streaming uses `insert_rows_json` (`tabledata.insertAll`), which ships in
`google-cloud-bigquery`. The newer Storage Write API is cheaper and supports
exactly-once, but needs a dependency that is not taken on and a protobuf
descriptor per table; it slots in as a third mode without callers changing.

Streaming is safe here specifically because the raw zone is append-only (AD-7):
streamed rows cannot be UPDATEd or DELETEd for a while after they arrive, and
nothing ever needs to — the staging model supersedes by `pull_id` with a QUALIFY,
so a re-pull adds rows and the newest wins.

Verified live on 2026-07-30 against real BigQuery, in a throwaway dataset that
deletes itself rather than in `org_toorow_raw`: a load job and a streaming insert
both landed and were read back, 3 rows in and 3 rows out, and the only datasets
left in the project afterwards were the three legitimate ones.

That probe also found the defect worth keeping: `bigquery.Client(project=None)`
silently adopts whatever project the ambient credentials name. The first run
**wrote to one GCP project and cleaned up another**, because the landing read
`GCP_PROJECT` while the probe's own client took the credential's default. The
write succeeded, in the wrong place. `resolve_warehouse_project()` now refuses
rather than guesses.
