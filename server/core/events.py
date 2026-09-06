"""Facts published to Pub/Sub, and the outbox that guarantees they leave.

Story 56.7 (AD-36). Three components carry work in this product and they answer
different questions: Cloud Scheduler is the clock, Cloud Tasks carries an ORDER
to one addressee, and Pub/Sub carries a FACT to N subscribers. This module is the
third one.

WHY A FACT IS NOT AN ORDER. Everything downstream of a pull -- verification,
context seed, mirror sync -- is an inline best-effort hook inside the worker
today, each failure collapsing into one WARNING line on a log stream shared with
two other loops. That is unreadable by construction, and it means a new consumer
cannot be added without editing the producer. A published fact inverts both: each
subscriber gets its own invocation, status and retry, and arrives later without
the producer knowing.

WHY THE OUTBOX, AND WHY IT WAS ALREADY THERE. `app.operation_outbox` (migration
060) has carried `state`, `attempts`, `locked_by`, `retry_at` and `delivered_at`
since the day it was created -- the full shape of a drainable outbox. Nothing
ever drained it: `dispatch_outbox_event` had no production caller. The row is
written in the SAME transaction as the mutation it describes, which is the only
way a fact cannot disagree with the state it reports: either both commit or
neither does. Publishing straight from the mutation would break exactly that.

NO PUB/SUB CONFIGURED IS NOT AN ERROR. A deployment without `PUBSUB_TOPIC` --
every dev machine, and this repo's tests -- logs the fact and marks it delivered.
The alternative, failing, would make the outbox grow forever on machines that
have no subscriber anyway. What it must NEVER do is silently pretend a configured
topic accepted a message it refused; that path raises.
"""

from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger(__name__)

#: Facts this product publishes. Named here so a subscriber can be written
#: against a list rather than against a grep of the producer.
FACT_PULL_LANDED = "pull.landed"
FACT_DATASTREAM_PUBLISHED = "datastream.published"


def _topic() -> str:
    return os.environ.get("PUBSUB_TOPIC", "").strip()


def publish_fact(event_type: str, payload: dict, *, idempotency_key: str = "") -> bool:
    """Publish one fact. Returns True when it left, False when there is nowhere to send it.

    Raises only when a topic IS configured and the publish fails -- the caller
    (the outbox drainer) then leaves the row pending for another attempt.
    """
    topic = _topic()
    if not topic:
        logger.info(
            "events: fact_not_published (no PUBSUB_TOPIC) type=%s key=%s",
            event_type,
            idempotency_key or "-",
        )
        return False

    from google.cloud import pubsub_v1  # noqa: PLC0415

    publisher = pubsub_v1.PublisherClient()
    data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    attributes = {"event_type": event_type}
    if idempotency_key:
        # The subscriber's deduplication key. Pub/Sub is at-least-once, so a
        # consumer that is not idempotent WILL double-process without this.
        attributes["idempotency_key"] = idempotency_key
    publisher.publish(topic, data, **attributes).result(timeout=30)
    logger.info("events: fact_published type=%s key=%s", event_type, idempotency_key or "-")
    return True


def drain_outbox(limit: int = 100) -> dict:
    """Publish pending outbox rows, oldest first. Story 56.7.

    Claims with ``FOR UPDATE SKIP LOCKED`` so two concurrent drains cannot
    publish the same row, marks 'delivered' on success, and on failure leaves the
    row pending with ``attempts`` incremented and ``retry_at`` pushed out. A fact
    that cannot be published is never dropped: the row is the ledger here too.

    Never raises. Returns counts.
    """
    result = {"published": 0, "failed": 0, "skipped_no_topic": 0}
    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, event_type, payload, attempts
                    FROM app.operation_outbox
                    WHERE state = 'pending' AND retry_at <= now()
                    ORDER BY retry_at
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED
                    """,
                    (limit,),
                )
                rows = cur.fetchall()
                for row_id, event_type, payload, attempts in rows:
                    try:
                        left = publish_fact(event_type, payload or {}, idempotency_key=row_id)
                    except Exception as exc:  # noqa: BLE001 -- one bad fact must not stop the rest
                        logger.warning("events: publish_failed id=%s: %s", row_id, exc)
                        cur.execute(
                            """
                            UPDATE app.operation_outbox
                            SET attempts = attempts + 1,
                                retry_at = now() + (LEAST(%s, 60) * interval '1 minute')
                            WHERE id = %s
                            """,
                            (int(attempts) + 1, row_id),
                        )
                        result["failed"] += 1
                        continue
                    cur.execute(
                        "UPDATE app.operation_outbox SET state='delivered', "
                        "delivered_at=now() WHERE id=%s",
                        (row_id,),
                    )
                    if left:
                        result["published"] += 1
                    else:
                        result["skipped_no_topic"] += 1
            conn.commit()
    except Exception as exc:  # noqa: BLE001 -- periodic; it runs again
        logger.warning("events: drain_failed: %s", exc)
    return result
