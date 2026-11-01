import asyncio
import sys

sys.path.insert(0, ".")


async def main() -> None:
    from sqlalchemy import select

    from app.database import SessionLocal, init_db
    from app.models import Batch, BatchStatus, Campaign
    from app.orchestrator.campaign_runner import campaign_manager

    await init_db()

    async with SessionLocal() as session:
        batch = Batch(
            name="verify-steady",
            niches=["dentists"],
            targets=[{"city": "Nasr City", "governorate": "Cairo"}],
            max_results=0,
            only_no_website=True,
            status=BatchStatus.QUEUED.value,
            total_jobs=1,
            scrape_profile="safe",
        )
        session.add(batch)
        await session.commit()
        await session.refresh(batch)
        batch_id = batch.id
        print("batch", batch_id)

    await campaign_manager.start_batch(batch_id)

    for i in range(36):
        await asyncio.sleep(5)
        running = campaign_manager.is_batch_running(batch_id)
        async with SessionLocal() as session:
            batch = await session.get(Batch, batch_id)
            camps = (
                await session.execute(select(Campaign).where(Campaign.batch_id == batch_id))
            ).scalars().all()
            campaign = camps[0] if camps else None
            scanned = campaign.progress_scanned if campaign else 0
            saved = campaign.progress_with_phone if campaign else 0
            print(
                f"t+{(i + 1) * 5}s status={batch.status} leads={batch.total_leads} "
                f"scanned={scanned} saved={saved} running={running}"
            )
            if batch.status in ("completed", "failed", "cancelled") and not running:
                break
            if saved >= 3:
                print("SUCCESS: got leads")
                break


if __name__ == "__main__":
    asyncio.run(main())
