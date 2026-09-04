from pathlib import Path


def test_order_closure_migration_has_audit_and_atomic_queue_tables() -> None:
    text = Path("alembic/versions/e8f9012345a6_add_order_closure_queue.py").read_text(
        encoding="utf-8"
    )
    assert 'down_revision = "d7e8f9012345"' in text
    assert '"order_closure_batch"' in text
    assert '"order_closure_item"' in text
    assert '"order_closure_event"' in text
    assert "diagnosis_hash" in text
    assert "lease_token" in text
    assert text.count("sa.String(36)") >= 5
