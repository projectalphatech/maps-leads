# Maps Leads (Egypt Lead Scraper)

> **بالعربي في 4 خطوات (Windows):**
> 1. نزّل Python 3.11 أو أحدث من [python.org](https://www.python.org/downloads/) (علّم على "Add Python to PATH").
> 2. من فوق: **Code ← Download ZIP** وفكّ الملف.
> 3. دبل كليك على `start.bat`. أول مرة بيسطّب نفسه (دقايق).
> 4. اختار المجال والمحافظة والمدينة. الـ Excel بيطلع في فولدر `exports`.
>
> ⚠️ استخدمها بهدوء ولعملاءك إنت بس: عدد قليل في اليوم (25–50)، ومن غير سبام.

Local app that scrapes **Google Maps** for Egyptian businesses, keeps only leads with a **valid phone number**, filters for **no real website**, and exports to **Excel (.xlsx)** for manual upload to Google Sheets.

## Features (MVP)

- Local web dashboard at `http://127.0.0.1:8787`
- Campaign wizard: niche → governorate → city → max results
- Playwright-based Google Maps scraping (no paid APIs)
- **Phone-first rule**: no valid Egyptian phone → skipped
- Excel export with sheets: `leads`, `skipped`, `summary`
- Pause/stop running campaigns

## Requirements

- Windows 10/11 (or any OS with Python 3.11+)
- Python 3.11+
- Chromium (installed via Playwright)

## Setup

```powershell
cd d:\Projects\egypt-lead-scraper
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
playwright install chromium
copy .env.example .env
```

## Run

```powershell
python -m app.main
```

Or double-click `start.bat`.

The dashboard opens in your browser. Chromium runs hidden (`HEADLESS=true`); set it to `false` in `.env` to watch it work.

## How leads are filtered

1. Open each Google Maps listing
2. Extract phone — **required**
3. Normalize to Egyptian format (`+20…`)
4. Skip if phone missing/invalid
5. Skip if business has a real website
6. Save businesses with phone + no website (social-only website counts as no website)

## Excel output

Files are saved to `exports/`:

- **leads** — phone column first
- **skipped** — no phone, has website, duplicates, errors
- **summary** — campaign stats

Upload to Google Sheets: **File → Import → Upload**.

## Configuration (`.env`)

| Variable | Default | Description |
|----------|---------|-------------|
| `HOST` | 127.0.0.1 | Bind address |
| `PORT` | 8787 | Local port |
| `HEADLESS` | true | Set false to watch the browser |
| `TURBO_MODE` | false | More workers, shorter pauses, more captchas |
| `SKIP_SOCIAL` | true | Skip the Google search for Facebook/Instagram links |
| `MAPS_DAILY_CAP` | 200 | Max listings per campaign run |
| `MAPS_LOCALE` | ar-EG | Browser locale |

## Responsible use

Scraping Google Maps is against Google's Terms of Service. Use this tool at your own risk, at **low volume**, for your **own** outreach only.
Do not spam, do not resell the lists, and respect people who ask you to stop contacting them. Data stays on your computer.

## Notes

- `SKIP_SOCIAL=true` (default): the Facebook/Instagram lookup runs a Google search per lead and quickly triggers Google's captcha, which pauses the whole app. Turn it on only with proxies.

- Start with **low volume** (25–50 results) to avoid Google rate limits
- Google Maps DOM changes over time — selectors may need updates

## Project structure

```
egypt-lead-scraper/
├── app/
│   ├── main.py              # FastAPI + UI
│   ├── scrapers/maps/       # Playwright scraper
│   ├── orchestrator/        # Campaign runner
│   ├── export/excel.py      # XLSX export
│   └── utils/phone.py       # Egyptian phone validation
├── config/egypt_locations.yaml
├── exports/                 # Excel files (gitignored)
└── data/                    # SQLite DB (gitignored)
```
