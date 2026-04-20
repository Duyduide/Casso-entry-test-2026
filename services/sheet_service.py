"""
sheet_service.py – Google Sheets integration.

Tab "Menu" (row 1 = header):
  category | item_id | name | description | price_m | price_l | available

Tab "Orders" (Bot ghi):
  Thời gian | Mã đơn | Zalo ID Khách | Chi tiết món | Tổng tiền | Trạng thái

Uses google-api-python-client with a service-account credentials file.
The menu is cached in-process (TTL = CACHE_TTL_SECONDS) to avoid hammering
the Sheets API on every message.
"""

import logging
import json
import time
from datetime import datetime
from typing import Any

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# Readwrite scope – cần ghi vào tab Orders và cập nhật available
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
MENU_RANGE = "Menu!A:G"
ORDERS_TAB = "Orders"
CACHE_TTL_SECONDS = 300  # 5 minutes

_cache: dict[str, Any] = {"data": None, "ts": 0.0}


def _build_service():
    # Ưu tiên biến môi trường GOOGLE_CREDENTIALS_JSON (Railway / production)
    if settings.GOOGLE_CREDENTIALS_JSON:
        info = json.loads(settings.GOOGLE_CREDENTIALS_JSON)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    else:
        creds = Credentials.from_service_account_file(
            settings.GOOGLE_CREDENTIALS_FILE, scopes=SCOPES
        )
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def _parse_menu_rows(rows: list[list[str]]) -> list[dict[str, Any]]:
    """Convert raw rows (header + data) into a list of dicts, filter available=TRUE."""
    if not rows:
        return []
    headers = [h.lower().strip() for h in rows[0]]
    result = []
    for row in rows[1:]:
        padded = row + [""] * (len(headers) - len(row))
        item: dict[str, Any] = dict(zip(headers, padded))
        # Chỉ trả về món đang có sẵn
        if str(item.get("available", "")).strip().upper() != "TRUE":
            continue
        # Coerce price_m / price_l sang int
        for price_col in ("price_m", "price_l"):
            try:
                item[price_col] = int(str(item.get(price_col, "0")).replace(",", "").strip())
            except (ValueError, TypeError):
                item[price_col] = 0
        result.append(item)
    return result


def get_menu() -> list[dict[str, Any]]:
    """
    Return available menu items as a list of dicts, using an in-memory TTL cache.

    Synchronous – call with asyncio.to_thread() from async contexts.
    """
    now = time.monotonic()
    if _cache["data"] is not None and (now - _cache["ts"]) < CACHE_TTL_SECONDS:
        return _cache["data"]

    try:
        service = _build_service()
        result = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=settings.GOOGLE_SHEET_ID, range=MENU_RANGE)
            .execute()
        )
        rows: list[list[str]] = result.get("values", [])
        menu = _parse_menu_rows(rows)
        _cache["data"] = menu
        _cache["ts"] = now
        logger.info("Menu refreshed: %d available items loaded.", len(menu))
        return menu
    except HttpError as exc:
        logger.error("Google Sheets API error (get_menu): %s", exc)
        return _cache["data"] or []
    except Exception as exc:
        logger.error("Unexpected error fetching menu: %s", exc)
        return _cache["data"] or []


def append_order(order_id: int, zalo_user_id: str, order_details: list, total_amount: int, status: str) -> None:
    """
    Thêm 1 dòng dữ liệu vào tab "Orders" để thống kê doanh thu.

    Synchronous – call with asyncio.to_thread() from async contexts.
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    details_str = "; ".join(
        f"{item.get('name', '')} x{item.get('quantity', 1)} ({item.get('size', '')})"
        for item in (order_details or [])
    )
    row = [timestamp, str(order_id), zalo_user_id, details_str, total_amount, status]
    try:
        service = _build_service()
        service.spreadsheets().values().append(
            spreadsheetId=settings.GOOGLE_SHEET_ID,
            range=f"{ORDERS_TAB}!A:F",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": [row]},
        ).execute()
        logger.info("Order #%s appended to Google Sheets Orders tab.", order_id)
    except HttpError as exc:
        logger.error("Google Sheets API error (append_order): %s", exc)
        raise
    except Exception as exc:
        logger.error("Unexpected error appending order: %s", exc)
        raise


def update_item_availability(item_name: str, available: bool) -> None:
    """
    Tìm dòng có name == item_name trong tab Menu và cập nhật cột available.

    Synchronous – call with asyncio.to_thread() from async contexts.
    """
    try:
        service = _build_service()
        result = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=settings.GOOGLE_SHEET_ID, range=MENU_RANGE)
            .execute()
        )
        rows: list[list[str]] = result.get("values", [])
        if not rows:
            logger.warning("Menu tab is empty, cannot update availability.")
            return

        headers = [h.lower().strip() for h in rows[0]]
        try:
            name_col_idx = headers.index("name")
            avail_col_idx = headers.index("available")
        except ValueError:
            logger.error("Menu tab missing 'name' or 'available' columns.")
            return

        # Tìm dòng khớp (so sánh case-insensitive)
        target_row_num: int | None = None
        for row_idx, row in enumerate(rows[1:], start=2):  # 1-indexed, bỏ header
            if len(row) > name_col_idx and row[name_col_idx].strip().lower() == item_name.strip().lower():
                target_row_num = row_idx
                break

        if target_row_num is None:
            logger.warning("Item '%s' not found in Menu tab.", item_name)
            return

        # Cột available: avail_col_idx + 1 (1-indexed) → chuyển sang ký tự cột (A=1)
        col_letter = chr(ord("A") + avail_col_idx)
        cell_range = f"Menu!{col_letter}{target_row_num}"
        new_value = "TRUE" if available else "FALSE"

        service.spreadsheets().values().update(
            spreadsheetId=settings.GOOGLE_SHEET_ID,
            range=cell_range,
            valueInputOption="USER_ENTERED",
            body={"values": [[new_value]]},
        ).execute()
        # Xóa cache để menu được làm mới lần sau
        invalidate_cache()
        logger.info("Updated '%s' availability to %s (row %d).", item_name, new_value, target_row_num)
    except HttpError as exc:
        logger.error("Google Sheets API error (update_item_availability): %s", exc)
        raise
    except Exception as exc:
        logger.error("Unexpected error updating availability: %s", exc)
        raise


def invalidate_cache() -> None:
    """Force a menu refresh on the next call."""
    _cache["data"] = None
    _cache["ts"] = 0.0


def append_revenue(order_id: int, zalo_user_id: str, order_details: list, total_amount: int) -> None:
    """
    Ghi đơn hàng đã thanh toán vào tab "Doanh thu YYYY-MM-DD" (tạo nếu chưa có).

    Columns: Thời gian | Mã đơn | Zalo ID Khách | Chi tiết món | Tổng tiền (VNĐ)

    Synchronous – call with asyncio.to_thread() from async contexts.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    tab_name = f"Doanh thu {today}"

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    details_str = "; ".join(
        f"{item.get('name', '')} x{item.get('quantity', 1)} ({item.get('size', '')})"
        for item in (order_details or [])
    )
    row = [timestamp, str(order_id), zalo_user_id, details_str, total_amount]

    try:
        service = _build_service()
        spreadsheet_id = settings.GOOGLE_SHEET_ID

        # Lấy danh sách sheet hiện có
        meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
        existing_titles = [s["properties"]["title"] for s in meta.get("sheets", [])]

        # Tạo tab mới nếu chưa có
        if tab_name not in existing_titles:
            service.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={
                    "requests": [
                        {"addSheet": {"properties": {"title": tab_name}}}
                    ]
                },
            ).execute()
            # Ghi header
            header = [["Thời gian", "Mã đơn", "Zalo ID Khách", "Chi tiết món", "Tổng tiền (VNĐ)"]]
            service.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range=f"{tab_name}!A1:E1",
                valueInputOption="USER_ENTERED",
                body={"values": header},
            ).execute()
            logger.info("Created new revenue tab: %s", tab_name)

        # Thêm dòng dữ liệu
        service.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range=f"{tab_name}!A:E",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": [row]},
        ).execute()
        logger.info("Revenue recorded for order #%s in tab '%s'.", order_id, tab_name)
    except HttpError as exc:
        logger.error("Google Sheets API error (append_revenue): %s", exc)
        raise
    except Exception as exc:
        logger.error("Unexpected error in append_revenue: %s", exc)
        raise
