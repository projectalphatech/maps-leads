"""Re-exports from browser_factory for backward compatibility."""

from app.scrapers.browser_factory import (
    CaptchaDetectedError,
    configure_context,
    detect_captcha,
    dismiss_google_consent,
    safe_goto,
)

__all__ = [
    "CaptchaDetectedError",
    "configure_context",
    "detect_captcha",
    "dismiss_google_consent",
    "safe_goto",
]
