import sys
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


def resolve_project_root() -> Path:
    """Writable project folder (data, exports, .env) — next to the .exe when frozen."""
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        # dist/EgyptLeads/EgyptLeads.exe → prefer parent project folder if present
        for candidate in (exe_dir.parent.parent, exe_dir.parent, exe_dir):
            if (candidate / "config" / "egypt_locations.yaml").exists() or (candidate / ".env").exists():
                return candidate
        return exe_dir
    return Path(__file__).resolve().parent.parent


def resolve_bundle_root() -> Path:
    """Read-only bundled assets (templates, static, yaml) — PyInstaller _MEIPASS when frozen."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    host: str = "127.0.0.1"
    port: int = 8787
    database_url: str = "sqlite+aiosqlite:///./data/leads.db"

    headless: bool = True
    maps_daily_cap: int = 200
    scroll_pause_min_ms: int = 1500
    scroll_pause_max_ms: int = 4500
    listing_pause_min_ms: int = 3000
    listing_pause_max_ms: int = 8000
    maps_locale: str = "ar-EG"

    batch_rest_min_ms: int = 60000
    batch_rest_max_ms: int = 180000

    maps_workers: int = 3
    social_workers: int = 2
    skip_social: bool = True  # FB/IG lookup searches Google per lead and trips its captcha; off by default

    # Turbo overnight mode — much faster scraping
    turbo_mode: bool = True
    turbo_maps_workers: int = 6
    turbo_listing_pause_min_ms: int = 400
    turbo_listing_pause_max_ms: int = 1200
    turbo_batch_rest_min_ms: int = 3000
    turbo_batch_rest_max_ms: int = 8000
    turbo_skip_social: bool = True  # skip FB/IG during overnight — huge speedup

    overnight_safe_default: bool = True  # Overnight form defaults to Safe profile

    # Batched export (was: full Excel rewrite on EVERY lead — major bottleneck)
    export_batch_size: int = 25
    export_interval_seconds: int = 45
    brain_batch_size: int = 20
    progress_every_n: int = 5
    turbo_progress_every_n: int = 15

    # Proxies — set PROXY_ENABLED=true and add proxies to proxies.txt
    proxy_enabled: bool = False
    proxy_file: Path = Path("proxies.txt")
    proxies: str = ""  # optional: comma-separated in env
    proxy_rotate_every: int = 25
    captcha_cooldown_seconds: int = 90
    captcha_max_retries: int = 3
    # Smarter captcha recovery
    captcha_manual_solve: bool = False  # never wait for human — auto backoff + rotate only
    captcha_manual_timeout_seconds: int = 300
    captcha_slow_seconds: int = 600  # scrape slower for 10 min after a hit
    captcha_max_backoff_seconds: int = 600  # cap exponential pause at 10 min
    captcha_reset_window_seconds: int = 1800  # reset consecutive streak after 30 min quiet

    exports_dir: Path = Path("./exports")
    profiles_dir: Path = Path("./profiles")

    @property
    def project_root(self) -> Path:
        return resolve_project_root()

    @property
    def bundle_root(self) -> Path:
        return resolve_bundle_root()


# Load .env from the writable project root when present
_env_path = resolve_project_root() / ".env"
settings = Settings(_env_file=_env_path if _env_path.exists() else ".env")
