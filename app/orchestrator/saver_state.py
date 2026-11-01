"""Track campaigns using batched LeadSaver (avoids circular imports)."""

_active_savers: set[int] = set()


def mark_saver_active(campaign_id: int) -> None:
    _active_savers.add(campaign_id)


def mark_saver_inactive(campaign_id: int) -> None:
    _active_savers.discard(campaign_id)


def is_saver_active(campaign_id: int) -> bool:
    return campaign_id in _active_savers
