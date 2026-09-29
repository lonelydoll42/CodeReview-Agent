"""Regression checks for PostgreSQL enum/bootstrap compatibility."""
from pathlib import Path

from storage.models import ReviewTask, TaskStatus


def test_sqlalchemy_persists_terminal_enum_labels_by_name():
    labels = ReviewTask.__table__.c.status.type.enums
    assert labels == [item.name for item in TaskStatus]
    assert "PARTIAL" in labels
    assert "UNCOVERED" in labels
    assert "partial" not in labels
    assert "uncovered" not in labels


def test_upgrade_migration_uses_postgresql_enum_names():
    migration = (
        Path(__file__).parents[1]
        / "alembic"
        / "versions"
        / "20260929_add_review_terminal_statuses.py"
    ).read_text()
    assert "ADD VALUE IF NOT EXISTS 'PARTIAL'" in migration
    assert "ADD VALUE IF NOT EXISTS 'UNCOVERED'" in migration
    assert "ADD VALUE IF NOT EXISTS 'partial'" not in migration
    assert "ADD VALUE IF NOT EXISTS 'uncovered'" not in migration


def test_upgrade_migration_can_bootstrap_a_new_database():
    migration = (
        Path(__file__).parents[1]
        / "alembic"
        / "versions"
        / "20260929_add_review_terminal_statuses.py"
    ).read_text()
    assert "CREATE TYPE task_status AS ENUM" in migration
    assert "CREATE TABLE IF NOT EXISTS review_tasks" in migration
    assert "CREATE TABLE IF NOT EXISTS review_results" in migration
    assert "CREATE TABLE IF NOT EXISTS review_reports" in migration
