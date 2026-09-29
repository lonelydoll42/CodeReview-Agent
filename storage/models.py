import enum
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSON
from sqlalchemy.ext.asyncio import AsyncAttrs, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from config import settings


# ---------------------------------------------------------------------------
# Engine & session factory
# ---------------------------------------------------------------------------

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------

class Base(AsyncAttrs, DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class TaskStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    UNCOVERED = "uncovered"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class ReviewTask(Base):
    """Represents a single code-review request for a PR."""

    __tablename__ = "review_tasks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    pr_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    status: Mapped[TaskStatus] = mapped_column(
        Enum(TaskStatus, name="task_status"),
        nullable=False,
        default=TaskStatus.PENDING,
        server_default=TaskStatus.PENDING.name,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relationships
    results: Mapped[list["ReviewResult"]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )
    report: Mapped["ReviewReport | None"] = relationship(
        back_populates="task", cascade="all, delete-orphan", uselist=False
    )
    coverage: Mapped[list["ReviewCoverage"]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<ReviewTask id={self.id} status={self.status} pr_url={self.pr_url!r}>"


class ReviewResult(Base):
    """Stores one per-file agent attempt, including failures and timeouts."""

    __tablename__ = "review_results"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("review_tasks.id", ondelete="CASCADE"), nullable=False
    )
    agent_name: Mapped[str] = mapped_column(String(256), nullable=False)
    # Nullable keeps rows written by pre-coverage versions readable.
    filename: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="completed", server_default="completed"
    )
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    findings: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Relationships
    task: Mapped[ReviewTask] = relationship(back_populates="results")

    def __repr__(self) -> str:
        return (
            f"<ReviewResult id={self.id} task_id={self.task_id}"
            f" agent={self.agent_name!r} confidence={self.confidence}>"
        )


class ReviewCoverage(Base):
    """Durable file-level coverage, including unsupported/skipped files."""

    __tablename__ = "review_coverage"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("review_tasks.id", ondelete="CASCADE"), nullable=False
    )
    filename: Mapped[str] = mapped_column(String(2048), nullable=False)
    language: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    expected_agents: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    completed_agents: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    failed_agents: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    task: Mapped[ReviewTask] = relationship(back_populates="coverage")

    def __repr__(self) -> str:
        return (
            f"<ReviewCoverage id={self.id} task_id={self.task_id}"
            f" filename={self.filename!r} status={self.status!r}>"
        )


class ReviewReport(Base):
    """Aggregated final report for a task, produced after all agents finish."""

    __tablename__ = "review_reports"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("review_tasks.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    final_report: Mapped[str] = mapped_column(Text, nullable=False, default="")
    markdown_report: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Relationships
    task: Mapped[ReviewTask] = relationship(back_populates="report")

    def __repr__(self) -> str:
        return f"<ReviewReport id={self.id} task_id={self.task_id}>"


# ---------------------------------------------------------------------------
# Dependency helper (for FastAPI)
# ---------------------------------------------------------------------------

async def get_db():
    """Yield an async database session; roll back on error."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def bootstrap_database() -> None:
    """Create new tables and extend an existing PostgreSQL task enum safely.

    ``Base.metadata.create_all`` does not alter an existing PostgreSQL enum.
    The explicit enum bootstrap keeps deployments that have not yet adopted
    Alembic from getting stuck when the application starts after the terminal
    status fields were added.  The checked-in Alembic migration remains the
    preferred production upgrade path.
    """
    # create_all first also creates the enum on a brand-new database.  The
    # explicit ALTER then runs in AUTOCOMMIT so older PostgreSQL versions do
    # not reject ALTER TYPE inside the transaction used by create_all.
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    if engine.dialect.name == "postgresql":
        async with engine.connect() as connection:
            connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
            await connection.execute(text(
                "ALTER TYPE task_status ADD VALUE IF NOT EXISTS 'PARTIAL'"
            ))
            await connection.execute(text(
                "ALTER TYPE task_status ADD VALUE IF NOT EXISTS 'UNCOVERED'"
            ))
            await connection.execute(text(
                "ALTER TABLE review_results ADD COLUMN IF NOT EXISTS filename VARCHAR(2048)"
            ))
            await connection.execute(text(
                "ALTER TABLE review_results ADD COLUMN IF NOT EXISTS status VARCHAR(32) NOT NULL DEFAULT 'completed'"
            ))
            await connection.execute(text(
                "ALTER TABLE review_results ADD COLUMN IF NOT EXISTS error_code VARCHAR(64)"
            ))
