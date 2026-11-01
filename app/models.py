import enum
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class CampaignStatus(str, enum.Enum):
    DRAFT = "draft"
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class BatchStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_id: Mapped[int | None] = mapped_column(
        ForeignKey("batches.id"), nullable=True, index=True
    )
    niche: Mapped[str] = mapped_column(String(255), nullable=False)
    governorate: Mapped[str] = mapped_column(String(128), nullable=False)
    city: Mapped[str] = mapped_column(String(128), nullable=False)
    max_results: Mapped[int] = mapped_column(Integer, default=100)
    status: Mapped[str] = mapped_column(String(32), default=CampaignStatus.QUEUED.value)
    progress_scanned: Mapped[int] = mapped_column(Integer, default=0)
    progress_with_phone: Mapped[int] = mapped_column(Integer, default=0)
    progress_no_phone: Mapped[int] = mapped_column(Integer, default=0)
    progress_no_website: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    export_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    leads: Mapped[list["Lead"]] = relationship(back_populates="campaign", cascade="all, delete-orphan")
    skipped: Mapped[list["SkippedLead"]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan"
    )
    batch: Mapped["Batch | None"] = relationship(back_populates="campaigns")


class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), nullable=False, index=True)
    phone: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    phone_display: Mapped[str | None] = mapped_column(String(64), nullable=True)
    business_name: Mapped[str] = mapped_column(String(512), nullable=False)
    category: Mapped[str | None] = mapped_column(String(255), nullable=True)
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    city: Mapped[str | None] = mapped_column(String(128), nullable=True)
    governorate: Mapped[str | None] = mapped_column(String(128), nullable=True)
    maps_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    website: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    website_status: Mapped[str] = mapped_column(String(32), default="missing")
    facebook_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    instagram_handle: Mapped[str | None] = mapped_column(String(255), nullable=True)
    social_confidence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    scraped_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    campaign: Mapped["Campaign"] = relationship(back_populates="leads")


class SkippedLead(Base):
    __tablename__ = "skipped_leads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), nullable=False, index=True)
    business_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    maps_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    scraped_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    campaign: Mapped["Campaign"] = relationship(back_populates="skipped")


class KnownLead(Base):
    """Global memory ('brain') to avoid scraping/saving the same lead twice."""

    __tablename__ = "known_leads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    phone: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, index=True)
    place_key: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    business_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    maps_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    first_campaign_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Batch(Base):
    """An overnight run: many niche x city campaigns executed sequentially."""

    __tablename__ = "batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    niches: Mapped[list] = mapped_column(JSON, nullable=False)
    targets: Mapped[list] = mapped_column(JSON, nullable=False)
    max_results: Mapped[int] = mapped_column(Integer, default=0)
    only_no_website: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(32), default=BatchStatus.QUEUED.value)
    total_jobs: Mapped[int] = mapped_column(Integer, default=0)
    completed_jobs: Mapped[int] = mapped_column(Integer, default=0)
    total_leads: Mapped[int] = mapped_column(Integer, default=0)
    current_job_label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    scrape_profile: Mapped[str] = mapped_column(String(32), default="safe")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    campaigns: Mapped[list["Campaign"]] = relationship(back_populates="batch")
