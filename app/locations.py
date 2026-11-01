from functools import lru_cache

import yaml

from app.config import settings


@lru_cache(maxsize=1)
def load_locations_config() -> dict:
    candidates = [
        settings.project_root / "config" / "egypt_locations.yaml",
        settings.bundle_root / "config" / "egypt_locations.yaml",
    ]
    for path in candidates:
        if path.exists():
            with path.open(encoding="utf-8") as handle:
                return yaml.safe_load(handle)
    raise FileNotFoundError("config/egypt_locations.yaml not found")


def get_governorates() -> list[dict]:
    data = load_locations_config()
    return data.get("governorates", [])


def get_niche_categories() -> dict[str, list[str]]:
    data = load_locations_config()
    return data.get("niche_categories", {})


def get_all_niches() -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for niches in get_niche_categories().values():
        for niche in niches:
            if niche not in seen:
                seen.add(niche)
                ordered.append(niche)
    return ordered


def get_niche_presets() -> list[str]:
    data = load_locations_config()
    presets = data.get("niche_presets")
    if presets:
        return presets
    return get_all_niches()


def get_cities_for_governorate(governorate_id: str) -> list[str]:
    for gov in get_governorates():
        if gov["id"] == governorate_id:
            return gov.get("cities", [])
    return []


def get_governorate_name(governorate_id: str) -> str | None:
    for gov in get_governorates():
        if gov["id"] == governorate_id:
            return gov.get("name_en")
    return None


def get_all_city_targets(governorate_ids: list[str] | None = None) -> list[dict]:
    targets: list[dict] = []
    for gov in get_governorates():
        if governorate_ids and gov["id"] not in governorate_ids:
            continue
        for city in gov.get("cities", []):
            targets.append(
                {
                    "governorate_id": gov["id"],
                    "governorate": gov.get("name_en"),
                    "city": city,
                }
            )
    return targets
