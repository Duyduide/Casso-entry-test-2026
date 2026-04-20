"""
payos_service.py – PayOS payment integration.

Uses the official payos Python SDK (AsyncPayOS for non-blocking I/O).
SDK docs: https://payos.vn/docs/sdks/back-end/python
"""

import logging

from payos import AsyncPayOS
from payos.types import CreatePaymentLinkRequest, WebhookData

from config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# ---------------------------------------------------------------------------
# Singleton AsyncPayOS instance
# ---------------------------------------------------------------------------

_payos: AsyncPayOS | None = None


def get_payos() -> AsyncPayOS:
    global _payos
    if _payos is None:
        _payos = AsyncPayOS(
            client_id=settings.PAYOS_CLIENT_ID,
            api_key=settings.PAYOS_API_KEY,
            checksum_key=settings.PAYOS_CHECKSUM_KEY,
        )
    return _payos


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def confirm_webhook_url(url: str) -> str:
    """
    Đăng ký / xác nhận webhook URL với PayOS.
    Gọi 1 lần mỗi khi thay đổi ngrok URL.
    """
    result = await get_payos().confirmWebhook(url)
    logger.info("PayOS webhook URL confirmed: %s → %s", url, result)
    return result


async def create_payment_link(order_id: int, amount: int) -> str:
    """
    Tạo PayOS payment link cho đơn hàng.

    Args:
        order_id: ID của Order trong DB (dùng làm order_code).
        amount:   Tổng tiền đơn hàng (VNĐ, nguyên).

    Returns:
        checkout_url – URL thanh toán gửi cho khách.
    """
    payment_request = CreatePaymentLinkRequest(
        order_code=order_id,
        amount=amount,
        description=f"Thanh toan don {order_id}",
        cancel_url=settings.PAYOS_CANCEL_URL,
        return_url=settings.PAYOS_RETURN_URL,
    )
    try:
        response = await get_payos().payment_requests.create(payment_request)
        logger.info(
            "PayOS payment link created for order #%s: %s",
            order_id,
            response.checkout_url,
        )
        return response.checkout_url
    except Exception as exc:
        logger.error("PayOS create_payment_link error (order #%s): %s", order_id, exc)
        raise


async def verify_webhook(raw_body: bytes) -> WebhookData:
    """
    Xác minh chữ ký PayOS webhook và trả về WebhookData.

    Args:
        raw_body: Raw request body bytes từ FastAPI.

    Returns:
        WebhookData với các field snake_case: order_code, amount, …

    Raises:
        WebhookError nếu checksum không hợp lệ.
    """
    webhook_data = await get_payos().webhooks.verify(raw_body)
    logger.info("PayOS webhook verified: order_code=%s", webhook_data.order_code)
    return webhook_data
