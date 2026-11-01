import structlog
from sqlalchemy import select

from app.database import SessionLocal
from app.models import KnownLead

logger = structlog.get_logger(__name__)


async def load_known_phones() -> set[str]:
    async with SessionLocal() as session:
        result = await session.execute(select(KnownLead.phone))
        return {row for row in result.scalars().all() if row}


async def load_known_place_keys() -> set[str]:
    async with SessionLocal() as session:
        result = await session.execute(select(KnownLead.place_key))
        return {row for row in result.scalars().all() if row}


async def register_known_leads(leads: list[dict], campaign_id: int | None = None) -> int:
    """Persist newly saved leads into the global brain. Returns count added."""
    if not leads:
        return 0

    phones = [lead.get("phone") for lead in leads if lead.get("phone")]
    if not phones:
        return 0

    added = 0
    async with SessionLocal() as session:
        existing_result = await session.execute(
            select(KnownLead.phone).where(KnownLead.phone.in_(phones))
        )
        existing = {row for row in existing_result.scalars().all() if row}

        for lead in leads:
            phone = lead.get("phone")
            if not phone or phone in existing:
                continue
            session.add(
                KnownLead(
                    phone=phone,
                    place_key=lead.get("place_key"),
                    business_name=lead.get("business_name"),
                    maps_url=lead.get("maps_url"),
                    first_campaign_id=campaign_id,
                )
            )
            existing.add(phone)
            added += 1

        if added:
            await session.commit()

    if added:
        logger.info("brain.registered", added=added, campaign_id=campaign_id)
    return added


async def known_count() -> int:
    async with SessionLocal() as session:
        result = await session.execute(select(KnownLead.id))
        return len(result.scalars().all())
