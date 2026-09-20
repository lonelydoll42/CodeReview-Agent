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

    def __repr__(self) -> str:
        return f"<ReviewTask id={self.id} status={self.status} pr_url={self.pr_url!r}>"


class ReviewResult(Base):
    """Stores the output produced by a single agent for a task."""

    __tablename__ = "review_results"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("review_tasks.id", ondelete="CASCADE"), nullable=False
    )
    agent_name: Mapped[str] = mapped_column(String(256), nullable=False)
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
