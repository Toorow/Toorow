-- Migration 010 (Story 5.1, AC5) -- trace continuity across the thread boundary.
--
-- The queue worker runs in a separate daemon thread from the enqueue path, so the
-- in-process OTel span context does not survive. We persist the originating trace_id
-- on the job row at enqueue time; the worker reads it back and emits its pull span
-- under the same trace tree (see core/queue.py enqueue_pull / _execute_job).
--
-- Additive, nullable column -- backward compatible with all existing rows and with
-- tracing disabled (trace_id stays NULL when TRACING_ENABLED=false). No backfill.
ALTER TABLE app.pull_jobs ADD COLUMN IF NOT EXISTS trace_id VARCHAR(64) NULL;
