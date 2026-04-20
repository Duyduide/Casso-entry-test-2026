"""
zalo_service.py – Zalo bot interactions powered by python-zalo-bot SDK.

SDK docs: https://pypi.org/project/python-zalo-bot/
"""

import logging

import zalo_bot
from zalo_bot.constants import ChatAction

from config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# ---------------------------------------------------------------------------
# Singleton Bot instance – reused across all requests
# ---------------------------------------------------------------------------

_bot: zalo_bot.Bot | None = None


def get_bot() -> zalo_bot.Bot:
    """Return the shared Bot instance, creating it on first call."""
    global _bot
    if _bot is None:
        _bot = zalo_bot.Bot(settings.ZALO_BOT_TOKEN)
    return _bot


# ---------------------------------------------------------------------------
# Messaging helpers
# ---------------------------------------------------------------------------

async def send_text_message(chat_id: str, text: str) -> None:
    """
    Send a plain-text message to a Zalo user.

    Args:
        chat_id: Zalo user ID (chat ID from Update).
        text: Message content.
    """
    await get_bot().send_message(chat_id, text[:2000])
    logger.info("Sent text message to %s", chat_id)


async def send_typing(chat_id: str) -> None:
    """Show a 'typing...' indicator to the user."""
    await get_bot().send_chat_action(chat_id, ChatAction.TYPING)


async def send_photo_message(chat_id: str, photo_url: str, caption: str = "") -> None:
    """Send an image message with optional caption."""
    await get_bot().send_photo(chat_id, caption, photo_url)
    logger.info("Sent photo message to %s", chat_id)


async def register_webhook(webhook_url: str, secret_token: str) -> bool:
    """
    Register (or update) the bot's webhook URL with Zalo.

    Args:
        webhook_url: Publicly reachable HTTPS URL for the /webhook/zalo endpoint.
        secret_token: A random secret to validate incoming webhook requests.

    Returns:
        True if registration succeeded.
    """
    result = await get_bot().set_webhook(url=webhook_url, secret_token=secret_token)
    logger.info("Webhook registration result: %s", result)
    return result


async def send_message_to_admin(text: str) -> None:
    """
    Gửi tin nhắn tới Admin Zalo ID (lấy từ config).
    Dùng để thông báo đơn hàng mới cho admin.
    """
    await send_text_message(settings.ADMIN_ZALO_ID, text)


async def close_bot() -> None:
    """Gracefully shut down the SDK bot (closes underlying HTTP session)."""
    global _bot
    if _bot is not None:
        try:
            async with _bot:
                pass  # __aexit__ handles teardown
        except Exception:
            pass
        _bot = None
