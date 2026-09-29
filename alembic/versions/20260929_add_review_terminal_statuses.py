"""Add truthful terminal statuses and per-file review coverage records.

The application Enum stores Python enum *names* in PostgreSQL by default, so
the labels added here are uppercase (``PARTIAL`` and ``UNCOVERED``).  The
public API still exposes the enum values ``partial`` and ``uncovered``.
"""

from alembic import op


revision = "20260929_review_coverage"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    # This repository previously bootstrapped tables with ``create_all`` and
    # therefore has no separate initial Alembic revision.  Make this revision
    # safe for a brand-new database as well as an existing pre-coverage one.
    # The IF NOT EXISTS guards keep the upgrade idempotent for deployments
    # that already ran the application bootstrap.
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'task_status') THEN
                CREATE TYPE task_status AS ENUM
                    ('PENDING', 'RUNNING', 'COMPLETED', 'PARTIAL', 'UNCOVERED', 'FAILED');
            END IF;
        END $$;
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS review_tasks (
            id BIGSERIAL PRIMARY KEY,
            pr_url VARCHAR(2048) NOT NULL,
            status task_status NOT NULL DEFAULT 'PENDING',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS review_results (
            id BIGSERIAL PRIMARY KEY,
            task_id BIGINT NOT NULL REFERENCES review_tasks(id) ON DELETE CASCADE,
            agent_name VARCHAR(256) NOT NULL,
            findings JSON NOT NULL DEFAULT '{}'::json,
            confidence DOUBLE PRECISION NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS review_reports (
            id BIGSERIAL PRIMARY KEY,
            task_id BIGINT NOT NULL UNIQUE REFERENCES review_tasks(id) ON DELETE CASCADE,
            final_report TEXT NOT NULL DEFAULT '',
            markdown_report TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )

    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_type WHERE typname = 'task_status') THEN
                ALTER TYPE task_status ADD VALUE IF NOT EXISTS 'PARTIAL';
                ALTER TYPE task_status ADD VALUE IF NOT EXISTS 'UNCOVERED';
            END IF;
        END $$;
        """
    )
    op.execute(
        """
        ALTER TABLE review_results
            ADD COLUMN IF NOT EXISTS filename VARCHAR(2048),
            ADD COLUMN IF NOT EXISTS status VARCHAR(32) NOT NULL DEFAULT 'completed',
            ADD COLUMN IF NOT EXISTS error_code VARCHAR(64)
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS review_coverage (
            id BIGSERIAL PRIMARY KEY,
            task_id BIGINT NOT NULL REFERENCES review_tasks(id) ON DELETE CASCADE,
            filename VARCHAR(2048) NOT NULL,
            language VARCHAR(64) NOT NULL DEFAULT '',
            status VARCHAR(32) NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            expected_agents BIGINT NOT NULL DEFAULT 0,
            completed_agents BIGINT NOT NULL DEFAULT 0,
            failed_agents BIGINT NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_review_coverage_task_id "
        "ON review_coverage (task_id)"
    )


def downgrade() -> None:
    # PostgreSQL cannot safely remove enum labels that may already be stored.
    raise NotImplementedError(
        "The review terminal enum migration is irreversible; restore a database backup to downgrade."
    )
