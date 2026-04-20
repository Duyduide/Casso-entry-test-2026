"""
main.py – FastAPI entry point.

Zalo Webhook contract:
  - GET  /webhook/zalo  → OA verification (Zalo sends a challenge token)
  - POST /webhook/zalo  → Incoming events (text messages, follows, etc.)
  - POST /webhook/payos → PayOS payment confirmation

The POST /webhook/zalo handler returns HTTP 200 immediately and processes
the message in a BackgroundTask to avoid Zalo's 5-second timeout.
"""

import asyncio
import json
import logging
import re
from contextlib import asynccontextmanager
from typing import Any

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from zalo_bot import Update

from config import Settings, get_settings
from database import get_db, init_db
from models import Order, OrderStatus
from services import ai_service, sheet_service, zalo_service, payos_service

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s – %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting up – initialising database…")
    await init_db()
    logger.info("Database ready.")
    yield
    logger.info("Shutting down – closing Zalo bot…")
    await zalo_service.close_bot()


app = FastAPI(
    title="Tiệm trà sữa An Nhiên Chatbot",
    version="2.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Health-check
# ---------------------------------------------------------------------------

@app.get("/health", tags=["ops"])
async def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Zalo Webhook – verification (GET)
# ---------------------------------------------------------------------------

@app.get("/webhook/zalo", tags=["webhook"])
async def zalo_verify(
    challenge: str = Query(..., description="Zalo verification token"),
):
    """Zalo OA verification handshake."""
    return PlainTextResponse(content=challenge)


# ---------------------------------------------------------------------------
# Admin command handler
# ---------------------------------------------------------------------------

async def _handle_admin_command(
    text: str,
    db: AsyncSession,
    settings: Settings,
) -> None:
    """
    Xử lý lệnh từ Admin Zalo:
      ok {id}         → Xác nhận đơn + gửi PayOS link cho khách
      huy {id}        → Hủy đơn + xin lỗi khách
      het {id} {tên}  → Hủy đơn + đánh dấu món hết trong Sheets + báo khách
    """
    match = re.match(r"^(ok|huy|het)\s+(\d+)(?:\s+(.+))?$", text.strip(), re.IGNORECASE)
    if not match:
        await zalo_service.send_message_to_admin(
            "⚠️ Lệnh không hợp lệ. Các lệnh hợp lệ:\n"
            "  ok {id}\n  huy {id}\n  het {id} {tên món}"
        )
        return

    command = match.group(1).lower()
    order_id = int(match.group(2))
    extra = (match.group(3) or "").strip()

    # Lấy Order từ DB
    result = await db.execute(select(Order).where(Order.id == order_id))
    order: Order | None = result.scalar_one_or_none()
    if order is None:
        await zalo_service.send_message_to_admin(f"❌ Không tìm thấy đơn #{order_id}.")
        return

    if command == "ok":
        try:
            order.status = OrderStatus.CONFIRMED
            await db.commit()
            payment_url = await payos_service.create_payment_link(order.id, order.total_amount)
            await zalo_service.send_text_message(
                order.zalo_user_id,
                f"🎉 Đơn #{order.id} đã được xác nhận!\n"
                f"Vui lòng thanh toán qua link sau:\n{payment_url}",
            )
            await zalo_service.send_message_to_admin(
                f"✅ Đã xác nhận đơn #{order_id} và gửi link thanh toán cho khách."
            )
        except Exception as exc:
            logger.exception("Error processing 'ok %s': %s", order_id, exc)
            await zalo_service.send_message_to_admin(f"❌ Lỗi khi xử lý đơn #{order_id}: {exc}")

    elif command == "huy":
        try:
            order.status = OrderStatus.CANCELED
            await db.commit()
            await zalo_service.send_text_message(
                order.zalo_user_id,
                f"😢 Xin lỗi bạn, đơn #{order.id} của bạn đã bị hủy.\n"
                "Nếu cần hỗ trợ, hãy nhắn lại cho chúng mình nhé!",
            )
            await zalo_service.send_message_to_admin(f"✅ Đã hủy đơn #{order_id}.")
        except Exception as exc:
            logger.exception("Error processing 'huy %s': %s", order_id, exc)
            await zalo_service.send_message_to_admin(f"❌ Lỗi khi hủy đơn #{order_id}: {exc}")

    elif command == "het":
        if not extra:
            await zalo_service.send_message_to_admin("⚠️ Vui lòng cung cấp tên món: het {id} {tên món}")
            return
        try:
            order.status = OrderStatus.CANCELED
            await db.commit()
            await asyncio.to_thread(sheet_service.update_item_availability, extra, False)
            await zalo_service.send_text_message(
                order.zalo_user_id,
                f"😔 Xin lỗi bạn, món \"{extra}\" hiện đã hết.\n"
                "Bạn có thể đặt lại và chọn món khác nhé!",
            )
            await zalo_service.send_message_to_admin(
                f"✅ Đã hủy đơn #{order_id} và đánh dấu \"{extra}\" hết hàng trong Sheets."
            )
        except Exception as exc:
            logger.exception("Error processing 'het %s %s': %s", order_id, extra, exc)
            await zalo_service.send_message_to_admin(f"❌ Lỗi khi xử lý lệnh het #{order_id}: {exc}")


# ---------------------------------------------------------------------------
# Background processing (customer + admin routing)
# ---------------------------------------------------------------------------

async def _handle_message(
    sender_id: str,
    user_text: str,
    db: AsyncSession,
    settings: Settings,
) -> None:
    """
    Pipeline xử lý tin nhắn (chạy trong BackgroundTask):
      - Nếu sender là admin → xử lý lệnh admin.
      - Nếu là khách hàng   → AI pipeline đặt món.
    """
    # ── Admin routing ──────────────────────────────────────────────────────
    if sender_id == settings.ADMIN_ZALO_ID:
        await _handle_admin_command(user_text, db, settings)
        return

    # ── Customer flow ───────────────────────────────────────────────────────
    try:
        await zalo_service.send_typing(sender_id)
    except Exception as exc:
        logger.warning("send_typing failed for %s: %s", sender_id, exc)

    try:
        menu_data = await asyncio.to_thread(sheet_service.get_menu)
    except Exception as exc:
        logger.error("Failed to fetch menu: %s", exc)
        menu_data = []

    try:
        reply, order_data = await ai_service.process_message(sender_id, user_text, menu_data)
    except Exception as exc:
        logger.exception("AI processing error for user %s: %s", sender_id, exc)
        reply = "Xin lỗi bạn, mình đang gặp sự cố. Vui lòng thử lại sau nhé! 🙏"
        order_data = None

    # Nếu đơn hàng đầy đủ thông tin → tạo Order + notify admin
    if order_data and ai_service.is_order_complete(order_data):
        try:
            items = order_data.get("items", [])
            total = int(order_data.get("total", 0))
            phone = str(order_data.get("phone", "")).strip()
            address = str(order_data.get("address", "")).strip()

            order = Order(
                zalo_user_id=sender_id,
                customer_info={"phone": phone, "address": address},
                order_details=items,
                total_amount=total,
                status=OrderStatus.PENDING,
            )
            db.add(order)
            await db.commit()
            await db.refresh(order)

            ai_service.clear_conversation(sender_id)

            # Tóm tắt đơn gửi admin
            items_str = "\n".join(
                f"  • {it.get('name', '')} x{it.get('quantity', 1)} ({it.get('size', '')})"
                for it in items
            )
            await zalo_service.send_message_to_admin(
                f"🔔 ĐƠN MỚI #{order.id}\n"
                f"Khách: {sender_id}\nSĐT: {phone}\nĐịa chỉ: {address}\n"
                f"Món:\n{items_str}\n"
                f"Tổng: {total:,}đ\n\n"
                f"Trả lời: ok {order.id} | huy {order.id}"
            )
            logger.info("Order #%s created for user %s", order.id, sender_id)
        except Exception as exc:
            logger.exception("Failed to create order for user %s: %s", sender_id, exc)

    # Gửi reply cho khách
    try:
        await zalo_service.send_text_message(sender_id, reply)
    except Exception as exc:
        logger.exception("Failed to send Zalo reply to %s: %s", sender_id, exc)


# ---------------------------------------------------------------------------
# Webhook registration
# ---------------------------------------------------------------------------

@app.post("/webhook/register", tags=["ops"])
async def register_webhook(
    webhook_url: str,
    secret_token: str = "casso_webhook_secret",
):
    """Đăng ký / cập nhật webhook URL với Zalo OA."""
    try:
        result = await zalo_service.register_webhook(webhook_url, secret_token)
        logger.info("Webhook registered: url=%s result=%s", webhook_url, result)
        return {"registered": True, "url": webhook_url, "result": result}
    except Exception as exc:
        logger.exception("Webhook registration failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------------------------
# Zalo Webhook – events (POST)
# ---------------------------------------------------------------------------

@app.post("/webhook/zalo", tags=["webhook"])
async def zalo_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Receive events from Zalo OA."""
    try:
        raw: dict[str, Any] = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    logger.debug("Zalo webhook raw payload: %s", raw)
    event_data = raw.get("result", raw)
    update: Update = Update.de_json(event_data, zalo_service.get_bot())

    if update is None or update.message is None or not update.message.text:
        return JSONResponse({"status": "ignored", "reason": "no text message"})

    user_text: str = update.message.text.strip()
    sender_id: str = str(update.message.chat.id)

    logger.info("Zalo message received | sender_id=%s | text=%r", sender_id, user_text)

    if not user_text or not sender_id:
        return JSONResponse({"status": "ignored", "reason": "empty message or sender"})

    background_tasks.add_task(
        _handle_message,
        sender_id=sender_id,
        user_text=user_text,
        db=db,
        settings=settings,
    )
    return JSONResponse({"status": "ok"})


# ---------------------------------------------------------------------------
# PayOS Webhook – payment confirmation (POST)
# ---------------------------------------------------------------------------
# receive-hook
@app.post("/webhook/payos", tags=["webhook"])
async def payos_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Nhận webhook thanh toán từ PayOS.
    Xác minh checksum → cập nhật Order PAID → ghi Sheets → cảm ơn khách.
    """
    raw_body = await request.body()

    try:
        webhook_data = await payos_service.verify_webhook(raw_body)
    except Exception as exc:
        logger.warning("PayOS webhook verification failed: %s", exc)
        raise HTTPException(status_code=400, detail="Invalid PayOS webhook signature")

    order_code = getattr(webhook_data, "order_code", None)
    if order_code is None:
        logger.warning("PayOS webhook missing order_code.")
        return JSONResponse({"message": "OK"})

    # Lấy Order từ DB
    result = await db.execute(select(Order).where(Order.id == int(order_code)))
    order: Order | None = result.scalar_one_or_none()
    if order is None:
        logger.warning("PayOS webhook: order #%s not found in DB.", order_code)
        return JSONResponse({"message": "OK"})

    # Cập nhật trạng thái PAID
    order.status = OrderStatus.PAID
    await db.commit()
    logger.info("Order #%s marked as PAID.", order.id)

    # Ghi vào Google Sheets – tab Orders (lịch sử) + tab Doanh thu hôm nay
    try:
        await asyncio.to_thread(
            sheet_service.append_order,
            order.id,
            order.zalo_user_id,
            order.order_details or [],
            order.total_amount or 0,
            OrderStatus.PAID.value,
        )
    except Exception as exc:
        logger.error("Failed to append order #%s to Orders tab: %s", order.id, exc)

    try:
        await asyncio.to_thread(
            sheet_service.append_revenue,
            order.id,
            order.zalo_user_id,
            order.order_details or [],
            order.total_amount or 0,
        )
    except Exception as exc:
        logger.error("Failed to append order #%s to revenue tab: %s", order.id, exc)

    # Gửi tin cảm ơn cho khách
    try:
        items_str = ", ".join(
            f"{it.get('name', '')} x{it.get('quantity', 1)}"
            for it in (order.order_details or [])
        )
        await zalo_service.send_text_message(
            order.zalo_user_id,
            f"🎉 Cảm ơn bạn đã thanh toán đơn #{order.id}!\n"
            f"Món: {items_str}\n"
            f"Tổng: {(order.total_amount or 0):,}đ\n"
            "Chúng mình sẽ giao hàng sớm nhất có thể. Cảm ơn bạn! 🧋",
        )
    except Exception as exc:
        logger.error("Failed to send thank-you message for order #%s: %s", order.id, exc)

    # Thông báo admin đơn đã thanh toán
    try:
        await zalo_service.send_message_to_admin(
            f"💰 Đơn #{order.id} đã thanh toán thành công!\n"
            f"Khách: {order.zalo_user_id}\n"
            f"Tổng: {(order.total_amount or 0):,}đ"
        )
    except Exception as exc:
        logger.error("Failed to notify admin of payment for order #%s: %s", order.id, exc)

    return JSONResponse({"message": "OK"})


# ---------------------------------------------------------------------------
# Ops – confirm PayOS webhook URL
# ---------------------------------------------------------------------------

@app.post("/ops/confirm-payos-webhook", tags=["ops"])
async def confirm_payos_webhook(webhook_url: str):
    """
    Xác nhận webhook URL với PayOS (gọi 1 lần sau khi đổi ngrok URL).
    Ví dụ: POST /ops/confirm-payos-webhook?webhook_url=https://xxx.ngrok-free.app/webhook/payos
    """
    try:
        result = await payos_service.confirm_webhook_url(webhook_url)
        logger.info("PayOS webhook confirmed: %s", webhook_url)
        return {"confirmed": True, "url": webhook_url, "result": result}
    except Exception as exc:
        logger.exception("PayOS webhook confirmation failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))
