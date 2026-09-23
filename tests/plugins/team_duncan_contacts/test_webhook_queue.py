"""Unit tests for the OPS-114 durable Plaud webhook queue.

Every test points at a temp-dir data directory -- never the real
~/.hermes.
"""

from __future__ import annotations

from pathlib import Path

from plugins.team_duncan_contacts.webhook_queue import WebhookQueue


def test_enqueue_then_list_pending(tmp_path: Path) -> None:
    queue = WebhookQueue(tmp_path)
    assert queue.enqueue("rec-1", now=1000.0) is True
    assert queue.list_pending() == ["rec-1"]


def test_enqueue_dedupes_by_recording_id(tmp_path: Path) -> None:
    queue = WebhookQueue(tmp_path)
    assert queue.enqueue("rec-1", now=1000.0) is True
    assert queue.enqueue("rec-1", now=2000.0) is True  # replay, not a new row
    assert queue.list_pending() == ["rec-1"]


def test_list_pending_is_ordered_by_enqueue_time(tmp_path: Path) -> None:
    queue = WebhookQueue(tmp_path)
    queue.enqueue("rec-second", now=2000.0)
    queue.enqueue("rec-first", now=1000.0)
    assert queue.list_pending() == ["rec-first", "rec-second"]


def test_remove_drops_the_row(tmp_path: Path) -> None:
    queue = WebhookQueue(tmp_path)
    queue.enqueue("rec-1", now=1000.0)
    queue.remove("rec-1")
    assert queue.list_pending() == []


def test_remove_of_unknown_recording_id_is_a_safe_noop(tmp_path: Path) -> None:
    queue = WebhookQueue(tmp_path)
    queue.remove("never-enqueued")  # must not raise
    assert queue.list_pending() == []


def test_queue_state_survives_across_instances_same_db_file(tmp_path: Path) -> None:
    """A fresh WebhookQueue instance over the same data_dir (as a restarted
    process would construct) sees exactly what a prior instance committed
    -- the durability guarantee the crash-recovery drain path depends on."""
    first = WebhookQueue(tmp_path)
    first.enqueue("rec-1", now=1000.0)

    second = WebhookQueue(tmp_path)
    assert second.list_pending() == ["rec-1"]

    second.remove("rec-1")
    third = WebhookQueue(tmp_path)
    assert third.list_pending() == []


def test_no_source_content_columns_exist(tmp_path: Path) -> None:
    """Guard against a future edit accidentally widening this table to
    hold a title, transcript, speaker name, or summary."""
    import sqlite3

    queue = WebhookQueue(tmp_path)
    queue.enqueue("rec-1", now=1000.0)
    conn = sqlite3.connect(str(tmp_path / "plaud_webhook_queue.db"))
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(webhook_events)")}
    finally:
        conn.close()
    assert columns == {"plaud_recording_id", "enqueued_at"}
