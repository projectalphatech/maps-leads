import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
import uvicorn
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.activity import ActivityProcessor, clear as clear_activity, recent as recent_activity
from app.config import settings
from app.database import get_session, init_db
from app.dedup import known_count
from app.locations import (
    get_all_city_targets,
    get_all_niches,
    get_cities_for_governorate,
    get_governorates,
    get_niche_categories,
    get_niche_presets,
)
from app.models import Batch, BatchStatus, Campaign, CampaignStatus, Lead, SkippedLead
from app.orchestrator.campaign_runner import campaign_manager
from app.proxy.pool import proxy_pool

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        ActivityProcessor(),
        structlog.processors.JSONRenderer(),
    ]
)
logger = structlog.get_logger(__name__)

def _asset_dir(*parts: str) -> Path:
    """Templates/static live under app/ in source, or bundled under _MEIPASS/app/."""
    bundle = settings.bundle_root
    candidates = [
        bundle / "app" / Path(*parts),
        Path(__file__).resolve().parent / Path(*parts),
        bundle / Path(*parts),
    ]
    for path in candidates:
        if path.exists():
            return path
    return candidates[0]


templates = Jinja2Templates(directory=str(_asset_dir("templates")))


@asynccontextmanager
async def lifespan(_: FastAPI):
    from app.preflight import run_preflight
    from app.scrapers.browser_factory import cleanup_profile_locks, kill_orphaned_chrome

    kill_orphaned_chrome()
    run_preflight()
    for profile_name in ("maps", "social"):
        profile_dir = settings.profiles_dir / profile_name
        if profile_dir.exists():
            cleanup_profile_locks(profile_dir)

    await init_db()
    ps = proxy_pool.status()
    if settings.proxy_enabled:
        logger.info("proxy.pool.ready", **ps)
        if ps["total"] == 0:
            logger.warning("proxy.enabled_but_empty", hint="Add proxies to proxies.txt")

    campaign_manager.start_watchdog()

    async def _startup_tasks() -> None:
        try:
            logger.info("browser.prewarm.start")
            await campaign_manager._scraper._ensure_context()
            logger.info("browser.prewarm.ready")
        except Exception as exc:
            logger.warning("browser.prewarm.failed", error=str(exc))

        resumed = await campaign_manager.resume_interrupted_batches()
        if resumed:
            logger.info("batch.startup.resumed", count=resumed)

    import asyncio

    asyncio.create_task(_startup_tasks())

    yield
    await campaign_manager.shutdown()


app = FastAPI(title="Egypt Lead Scraper", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(_asset_dir("static"))), name="static")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return RedirectResponse(url="/static/favicon.svg", status_code=307)


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(Campaign).order_by(Campaign.id.desc()).limit(20))
    campaigns = list(result.scalars().all())

    batch_result = await session.execute(select(Batch).order_by(Batch.id.desc()).limit(10))
    batches = list(batch_result.scalars().all())

    brain_count = await known_count()
    leads_count = await session.scalar(select(func.count()).select_from(Lead)) or 0
    running_batches = sum(1 for b in batches if b.status == "running")

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "campaigns": campaigns,
            "batches": batches,
            "brain_count": brain_count,
            "leads_count": leads_count,
            "running_batches": running_batches,
            "active_nav": "home",
        },
    )


@app.get("/leads", response_class=HTMLResponse)
async def leads_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
    q: str = "",
):
    query = select(Lead).order_by(Lead.id.desc()).limit(500)
    result = await session.execute(query)
    leads = list(result.scalars().all())
    q_clean = (q or "").strip().lower()
    if q_clean:
        leads = [
            lead
            for lead in leads
            if q_clean in (lead.business_name or "").lower()
            or q_clean in (lead.phone or "")
            or q_clean in (lead.phone_display or "")
            or q_clean in (lead.address or "").lower()
            or q_clean in (lead.city or "").lower()
        ]

    return templates.TemplateResponse(
        request,
        "leads.html",
        {
            "leads": leads,
            "q": q or "",
            "active_nav": "leads",
            "total_shown": len(leads),
        },
    )


@app.get("/leads/export")
async def export_all_leads(session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(Lead).order_by(Lead.id))
    leads = list(result.scalars().all())
    if not leads:
        raise HTTPException(status_code=404, detail="No leads to export")

    from app.export.excel import export_all_leads_to_excel

    path = export_all_leads_to_excel(leads)
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=path.name,
    )


@app.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request):
    return templates.TemplateResponse(
        request,
        "logs.html",
        {"active_nav": "logs"},
    )


@app.get("/api/activity")
async def api_activity(after_id: int = 0, limit: int = 80):
    return {"events": recent_activity(limit=min(limit, 200), after_id=after_id)}


@app.post("/api/activity/clear")
async def api_activity_clear():
    clear_activity()
    return {"ok": True}


@app.get("/campaigns/new", response_class=HTMLResponse)
async def new_campaign_form(request: Request):
    return templates.TemplateResponse(
        request,
        "new_campaign.html",
        {
            "governorates": get_governorates(),
            "niche_presets": get_niche_presets(),
            "active_nav": "new",
        },
    )


@app.get("/api/cities/{governorate_id}")
async def api_cities(governorate_id: str):
    return {"cities": get_cities_for_governorate(governorate_id)}


@app.post("/campaigns")
async def create_campaign(
    request: Request,
    niche: str = Form(...),
    governorate_id: str = Form(...),
    governorate_name: str = Form(...),
    city: str = Form(...),
    max_results: int = Form(50),
    session: AsyncSession = Depends(get_session),
):
    niche = niche.strip()
    city = city.strip()
    if not niche or not city:
        raise HTTPException(status_code=400, detail="Niche and city are required")

    max_results = max(10, min(max_results, settings.maps_daily_cap))

    campaign = Campaign(
        niche=niche,
        governorate=governorate_name,
        city=city,
        max_results=max_results,
        status=CampaignStatus.QUEUED.value,
    )
    session.add(campaign)
    await session.commit()
    await session.refresh(campaign)

    await campaign_manager.start_campaign(campaign.id)
    return RedirectResponse(url=f"/campaigns/{campaign.id}", status_code=303)


@app.get("/campaigns/{campaign_id}", response_class=HTMLResponse)
async def campaign_detail(
    request: Request,
    campaign_id: int,
    session: AsyncSession = Depends(get_session),
):
    campaign = await session.get(Campaign, campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    leads_result = await session.execute(
        select(Lead).where(Lead.campaign_id == campaign_id).order_by(Lead.id.desc()).limit(50)
    )
    leads = list(leads_result.scalars().all())

    skipped_result = await session.execute(
        select(SkippedLead)
        .where(SkippedLead.campaign_id == campaign_id)
        .order_by(SkippedLead.id.desc())
        .limit(30)
    )
    skipped = list(skipped_result.scalars().all())

    return templates.TemplateResponse(
        request,
        "campaign_detail.html",
        {
            "campaign": campaign,
            "leads": leads,
            "skipped": skipped,
            "is_running": campaign_manager.is_running(campaign_id),
        },
    )


@app.post("/campaigns/{campaign_id}/stop")
async def stop_campaign(campaign_id: int, session: AsyncSession = Depends(get_session)):
    campaign = await session.get(Campaign, campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    await campaign_manager.stop_campaign(campaign_id)
    return RedirectResponse(url=f"/campaigns/{campaign_id}", status_code=303)


@app.get("/campaigns/{campaign_id}/export")
async def download_export(campaign_id: int, session: AsyncSession = Depends(get_session)):
    campaign = await session.get(Campaign, campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    leads_result = await session.execute(
        select(Lead).where(Lead.campaign_id == campaign_id).order_by(Lead.id)
    )
    leads = list(leads_result.scalars().all())
    if not leads:
        raise HTTPException(status_code=404, detail="No leads to export")

    from app.export.excel import export_campaign_to_excel

    path = export_campaign_to_excel(campaign, leads)
    campaign.export_path = str(path)
    await session.commit()

    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=path.name,
    )


@app.get("/batches/{batch_id}/export")
async def download_batch_export(batch_id: int, session: AsyncSession = Depends(get_session)):
    batch = await session.get(Batch, batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")

    campaigns_result = await session.execute(
        select(Campaign).where(Campaign.batch_id == batch_id)
    )
    campaigns = list(campaigns_result.scalars().all())
    if not campaigns:
        raise HTTPException(status_code=404, detail="No campaigns in this batch")

    campaign_ids = [c.id for c in campaigns]
    leads_result = await session.execute(
        select(Lead).where(Lead.campaign_id.in_(campaign_ids)).order_by(Lead.id)
    )
    leads = list(leads_result.scalars().all())
    if not leads:
        raise HTTPException(status_code=404, detail="No leads to export yet")

    from app.export.excel import export_batch_to_excel

    path = export_batch_to_excel(batch, leads, {c.id: c for c in campaigns})

    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=path.name,
    )


@app.get("/overnight", response_class=HTMLResponse)
async def overnight_form(request: Request):
    from app.proxy.pool import proxy_pool

    return templates.TemplateResponse(
        request,
        "overnight.html",
        {
            "governorates": get_governorates(),
            "niche_categories": get_niche_categories(),
            "total_niches": len(get_all_niches()),
            "proxy_status": proxy_pool.status(),
            "default_profile": "safe" if settings.overnight_safe_default else "turbo",
            "active_nav": "overnight",
        },
    )


@app.post("/overnight")
async def create_batch(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    form = await request.form()

    selected_niches = form.getlist("niches")
    if "__all__" in selected_niches or not selected_niches:
        selected_niches = get_all_niches()

    selected_gov_ids = form.getlist("governorates")
    if "__all__" in selected_gov_ids:
        selected_gov_ids = []

    targets = get_all_city_targets(selected_gov_ids or None)

    if not targets or not selected_niches:
        raise HTTPException(status_code=400, detail="Select at least one niche and location")

    name = (form.get("name") or "").strip() or "Overnight run"
    profile = (form.get("scrape_profile") or "safe").strip().lower()
    if profile not in ("safe", "turbo"):
        profile = "safe"

    batch = Batch(
        name=name,
        niches=selected_niches,
        targets=targets,
        max_results=0,
        only_no_website=True,
        status=BatchStatus.QUEUED.value,
        total_jobs=len(selected_niches) * len(targets),
        scrape_profile=profile,
    )
    session.add(batch)
    await session.commit()
    await session.refresh(batch)

    await campaign_manager.start_batch(batch.id)
    return RedirectResponse(url=f"/batches/{batch.id}", status_code=303)


@app.get("/batches/{batch_id}", response_class=HTMLResponse)
async def batch_detail(
    request: Request,
    batch_id: int,
    session: AsyncSession = Depends(get_session),
):
    batch = await session.get(Batch, batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")

    campaigns_result = await session.execute(
        select(Campaign).where(Campaign.batch_id == batch_id).order_by(Campaign.id.desc()).limit(60)
    )
    campaigns = list(campaigns_result.scalars().all())

    return templates.TemplateResponse(
        request,
        "batch_detail.html",
        {
            "batch": batch,
            "campaigns": campaigns,
            "is_running": campaign_manager.is_batch_running(batch_id)
            or batch.status in ("running", "queued"),
        },
    )


@app.get("/api/campaigns/{campaign_id}/status")
async def campaign_status_json(campaign_id: int, session: AsyncSession = Depends(get_session)):
    campaign = await session.get(Campaign, campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    leads_result = await session.execute(
        select(Lead).where(Lead.campaign_id == campaign_id).order_by(Lead.id.desc()).limit(5)
    )
    recent = list(leads_result.scalars().all())
    return {
        "status": campaign.status,
        "scanned": campaign.progress_scanned,
        "leads_saved": campaign.progress_with_phone,
        "no_phone": campaign.progress_no_phone,
        "is_running": campaign_manager.is_running(campaign_id),
        "export_ready": bool(campaign.export_path),
        "recent_leads": [
            {
                "phone": l.phone_display or l.phone,
                "name": l.business_name,
                "facebook": l.facebook_url,
                "instagram": l.instagram_handle,
            }
            for l in recent
        ],
    }


@app.get("/api/batches/{batch_id}/status")
async def batch_status_json(batch_id: int, session: AsyncSession = Depends(get_session)):
    batch = await session.get(Batch, batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    try:
        # Self-heal orphaned rows after app restart/crash:
        # DB may say "running", but the in-memory task no longer exists.
        # Auto-resume instead of cancelling — user shouldn't lose overnight progress.
        if (
            batch.status
            in (BatchStatus.RUNNING.value, BatchStatus.QUEUED.value, BatchStatus.PAUSED.value)
            and not campaign_manager.is_batch_running(batch_id)
        ):
            logger.warning("batch.orphan.detected", batch_id=batch_id)
            import asyncio

            asyncio.create_task(campaign_manager.try_resume_batch(batch_id))
    except Exception as exc:
        logger.exception("batch.status.self_heal_error", batch_id=batch_id, error=str(exc))
        await session.rollback()
    return {
        "status": batch.status,
        "completed_jobs": batch.completed_jobs,
        "total_jobs": batch.total_jobs,
        "total_leads": batch.total_leads,
        "current_job_label": batch.current_job_label,
        "is_running": campaign_manager.is_batch_running(batch_id),
    }


@app.post("/batches/{batch_id}/stop")
async def stop_batch(batch_id: int, session: AsyncSession = Depends(get_session)):
    batch = await session.get(Batch, batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    await campaign_manager.stop_batch(batch_id)
    return RedirectResponse(url=f"/batches/{batch_id}", status_code=303)


@app.post("/batches/stop-all")
async def stop_all_batches():
    await campaign_manager.stop_all_batches()
    return RedirectResponse(url="/", status_code=303)


def run() -> None:
    import logging
    import threading

    url = f"http://{settings.host}:{settings.port}"

    def _open_browser() -> None:
        import time

        time.sleep(1.8)
        webbrowser.open(f"{url}/leads")

    import os
    import sys

    # pythonw / windowed builds have no console
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")

    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    threading.Thread(target=_open_browser, daemon=True).start()
    try:
        print(f"\n  Egypt Lead Scraper ready — opening Leads…\n  {url}/leads\n")
    except Exception:
        pass
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
        log_level="warning",
        log_config=None,
    )


if __name__ == "__main__":
    run()
