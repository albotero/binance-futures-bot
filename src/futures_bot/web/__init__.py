"""Same-origin authenticated operator API and optional compiled dashboard."""

from futures_bot.web.api import configure_web
from futures_bot.web.auth import WebAuthSettings, bootstrap_owner

__all__ = ["WebAuthSettings", "bootstrap_owner", "configure_web"]
