"""
ai_service.py – Message processing via LangChain with conversation state.

Features:
  process_message()   – full pipeline: menu + conversation history + order extraction.
  is_order_complete() – kiểm tra đơn có đủ thông tin (items, sđt, địa chỉ).
  clear_conversation() – xóa history sau khi tạo đơn thành công.
"""

import json
import logging
import re
from typing import Any

from langchain.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain.schema.output_parser import StrOutputParser
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, AIMessage

from config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# ---------------------------------------------------------------------------
# Conversation history store (in-memory, per zalo_user_id)
# ---------------------------------------------------------------------------

# { zalo_user_id: [HumanMessage | AIMessage, ...] }
_conversations: dict[str, list] = {}
_MAX_HISTORY_TURNS = 10  # giữ tối đa 10 lượt (20 message objects)


def clear_conversation(user_id: str) -> None:
    """Xóa lịch sử hội thoại của user sau khi đơn được tạo."""
    _conversations.pop(user_id, None)


# ---------------------------------------------------------------------------
# LLM factory
# ---------------------------------------------------------------------------

def _build_llm() -> BaseChatModel:
    provider = settings.LLM_PROVIDER.lower()
    if provider == "google":
        from langchain_google_genai import ChatGoogleGenerativeAI  # type: ignore
        return ChatGoogleGenerativeAI(
            model="gemini-2.5-flash",
            google_api_key=settings.GOOGLE_API_KEY,
            temperature=0.3,
        )
    from langchain_openai import ChatOpenAI  # type: ignore
    return ChatOpenAI(
        model="gpt-4o-mini",
        openai_api_key=settings.OPENAI_API_KEY,
        temperature=0.3,
    )


# Lazily initialised singleton so we don't import heavy deps at module load
_llm: BaseChatModel | None = None


def get_llm() -> BaseChatModel:
    global _llm
    if _llm is None:
        _llm = _build_llm()
    return _llm


# ---------------------------------------------------------------------------
# Prompt template
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """Bạn là nhân viên tư vấn thân thiện của quán trà sữa "An Nhiên".
Nhiệm vụ: tư vấn và nhận đơn hàng. Không trả lời câu hỏi ngoài lề.

MENU HIỆN TẠI (chỉ gồm các món ĐANG CÓ SẴN):
{menu}

QUY TẮC:
1. Chỉ nhận các món CÓ TRONG DANH SÁCH MENU HIỆN TẠI bên trên.
   - Nếu khách đặt món KHÔNG có trong danh sách đó (dù có thể là món quán từng bán), hãy trả lời: "Xin lỗi bạn, [tên món] hiện đã hết, bạn có muốn thử [gợi ý món gần nhất] không? 🙏" và KHÔNG thêm thẻ <order_data>.
   - KHÔNG bao giờ xác nhận đơn hàng có món nằm ngoài danh sách menu hiện tại.
2. Cần thu thập đủ 4 thông tin trước khi xác nhận đơn:
   a) Món đồ uống (tên, size M/L, số lượng)
   b) Số điện thoại khách hàng
   c) Địa chỉ giao hàng
3. Nếu thiếu bất kỳ thông tin nào, hỏi lại khách một câu cụ thể (đừng hỏi nhiều thứ một lúc).
4. Khi đủ thông tin, tóm tắt đơn và thêm JSON ở cuối phản hồi theo đúng định dạng:
   <order_data>{{"items": [{{"name": "...", "size": "M", "quantity": 1, "price": 0}}], "total": 0, "phone": "...", "address": "..."}}</order_data>
5. Nếu khách chưa đặt hàng (chỉ hỏi thông tin), KHÔNG thêm thẻ <order_data>.
6. Luôn trả lời bằng tiếng Việt, ngắn gọn và thân thiện.
"""

_prompt = ChatPromptTemplate.from_messages(
    [
        ("system", _SYSTEM_PROMPT),
        MessagesPlaceholder(variable_name="history"),
        ("human", "{user_message}"),
    ]
)


# ---------------------------------------------------------------------------
# Helpers & Public API
# ---------------------------------------------------------------------------

def _format_menu(menu_data: list[dict[str, Any]]) -> str:
    """Chuyển danh sách menu thành chuỗi dễ đọc cho prompt."""
    if not menu_data:
        return "Menu đang được cập nhật. Vui lòng liên hệ nhân viên."
    lines = []
    for item in menu_data:
        name = item.get("name", "")
        price_m = item.get("price_m", 0)
        price_l = item.get("price_l", 0)
        desc = item.get("description", "")
        line = f"- {name}"
        if price_m and price_l:
            line += f": Size M {price_m:,}đ / Size L {price_l:,}đ"
        elif price_m:
            line += f": {price_m:,}đ"
        if desc:
            line += f" – {desc}"
        lines.append(line)
    return "\n".join(lines)


def _extract_order_json(raw_reply: str) -> dict[str, Any] | None:
    """Parse thẻ <order_data>...</order_data> từ reply của LLM."""
    match = re.search(r"<order_data>(.*?)</order_data>", raw_reply, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(1).strip())
    except json.JSONDecodeError:
        logger.warning("Failed to parse order JSON from LLM reply.")
        return None


def is_order_complete(order_data: dict[str, Any] | None) -> bool:
    """
    Kiểm tra đơn hàng có đủ thông tin để tạo record hay chưa.
    Yêu cầu: items không rỗng, có phone và address.
    """
    if not order_data:
        return False
    items = order_data.get("items") or []
    phone = str(order_data.get("phone", "")).strip()
    address = str(order_data.get("address", "")).strip()
    return bool(items and phone and address)


async def process_message(
    user_id: str,
    user_message: str,
    menu_data: list[dict[str, Any]],
) -> tuple[str, dict[str, Any] | None]:
    """
    Xử lý tin nhắn khách hàng với LangChain + conversation history.

    Returns:
        (clean_reply_text, order_data_or_None)
    """
    menu_text = _format_menu(menu_data)
    history = _conversations.get(user_id, [])

    chain = _prompt | get_llm() | StrOutputParser()
    raw_reply: str = await chain.ainvoke(
        {
            "menu": menu_text,
            "history": history,
            "user_message": user_message,
        }
    )

    # Cập nhật history
    updated_history = history + [HumanMessage(content=user_message), AIMessage(content=raw_reply)]
    if len(updated_history) > _MAX_HISTORY_TURNS * 2:
        updated_history = updated_history[-(_MAX_HISTORY_TURNS * 2):]
    _conversations[user_id] = updated_history

    order_data = _extract_order_json(raw_reply)
    clean_reply = re.sub(r"<order_data>.*?</order_data>", "", raw_reply, flags=re.DOTALL).strip()

    # --- Safety validation: đảm bảo tất cả món trong đơn đều available ---
    if order_data:
        available_names = {item["name"].lower().strip() for item in menu_data}
        unavailable_items = [
            item["name"]
            for item in (order_data.get("items") or [])
            if item["name"].lower().strip() not in available_names
        ]
        if unavailable_items:
            unavailable_str = ", ".join(unavailable_items)
            clean_reply = (
                f"Xin lỗi bạn, {unavailable_str} hiện đã hết hàng rồi 😢 "
                "Bạn vui lòng chọn món khác trong menu nhé!"
            )
            order_data = None
            # Cập nhật lại history để LLM không bị lệch ngữ cảnh
            _conversations[user_id][-1] = AIMessage(content=clean_reply)
            logger.info(
                "Blocked order for user %s: unavailable items %s",
                user_id, unavailable_items,
            )

    return clean_reply, order_data
