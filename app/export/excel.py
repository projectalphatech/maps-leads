from datetime import datetime
from pathlib import Path

import pandas as pd

from app.config import settings
from app.models import Batch, Campaign, Lead

# Only fields useful for outreach / Google Sheets import
LEAD_COLUMNS = [
    "phone",
    "business_name",
    "address",
    "facebook_url",
    "instagram_handle",
    "maps_url",
]

SHEET_NAME = "Leads"


def _slug(value: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in value).strip("_")[:40]


def _lead_row(lead: Lead, *, niche: str | None = None) -> dict:
    return {
        "phone": lead.phone_display or lead.phone,
        "business_name": lead.business_name,
        "address": lead.address or "",
        "facebook_url": lead.facebook_url or "",
        "instagram_handle": lead.instagram_handle or "",
        "maps_url": lead.maps_url or "",
    }


def write_leads_sheet(path: Path, rows: list[dict]) -> Path:
    settings.exports_dir.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        pd.DataFrame(rows, columns=LEAD_COLUMNS).to_excel(
            writer, sheet_name=SHEET_NAME, index=False
        )
    return path


def build_campaign_export_path(campaign: Campaign) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{_slug(campaign.governorate)}_{_slug(campaign.city)}_{_slug(campaign.niche)}_{timestamp}.xlsx"
    return settings.exports_dir.resolve() / filename


def build_batch_export_path(batch: Batch) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"batch_{batch.id}_{_slug(batch.name)}_{timestamp}.xlsx"
    return settings.exports_dir.resolve() / filename


def build_all_leads_export_path() -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return settings.exports_dir.resolve() / f"all_leads_{timestamp}.xlsx"


def export_campaign_to_excel(campaign: Campaign, leads: list[Lead], skipped: list | None = None) -> Path:
    """Export campaign leads to a single-sheet Excel file."""
    export_path = build_campaign_export_path(campaign)
    rows = [_lead_row(lead) for lead in leads]
    return write_leads_sheet(export_path, rows)


def export_batch_to_excel(batch: Batch, leads: list[Lead], campaigns_by_id: dict[int, Campaign]) -> Path:
    """Export all batch leads into one single-sheet Excel file."""
    export_path = build_batch_export_path(batch)
    rows = [_lead_row(lead) for lead in leads]
    return write_leads_sheet(export_path, rows)


def export_all_leads_to_excel(leads: list[Lead]) -> Path:
    export_path = build_all_leads_export_path()
    rows = [_lead_row(lead) for lead in leads]
    return write_leads_sheet(export_path, rows)
