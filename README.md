# Chatbot Đặt Hàng – Quán Cà Phê An Nhiên

Dự án Entry Test Casso 2026.

Chatbot nhận đơn hàng qua Zalo OA, hỗ trợ thanh toán online và tự động lưu doanh thu vào Google Sheets.

## Tech Stack

| Thành phần | Công nghệ |
|---|---|
| Backend | Python, FastAPI, Uvicorn |
| AI / Chatbot | LangChain, Google Gemini 2.5 Flash |
| Database | PostgreSQL 15, SQLAlchemy (async) |
| Messaging | Zalo Bot |
| Payment | PayOS |
| Spreadsheet | Google Sheets API (service account) |
| Containerization | Docker, Docker Compose |

## Tính năng

- Nhận tin nhắn từ khách qua Zalo OA, trả lời tự động bằng AI
- Gợi ý món theo menu lấy từ Google Sheets (cache 5 phút)
- Từ chối đặt món đã hết hàng (`available=FALSE`)
- Tạo link thanh toán PayOS sau khi đơn hoàn tất
- Ghi đơn vào tab **Orders** và tab **Doanh thu YYYY-MM-DD** sau khi thanh toán thành công
- Thông báo admin qua Zalo khi có đơn mới và khi khách thanh toán

## Cấu trúc thư mục

```
chatbot-mvp/
├── main.py               # FastAPI entry point, webhook handlers
├── models.py             # SQLAlchemy models
├── database.py           # DB engine + init
├── config.py             # Settings (dotenv)
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
└── services/
    ├── ai_service.py     # LangChain conversation + order extraction
    ├── sheet_service.py  # Google Sheets read/write
    ├── zalo_service.py   # Zalo OA messaging
    └── payos_service.py  # PayOS payment link + webhook verify
```

## Chạy local

```bash
cp .env.example .env   # điền các biến môi trường
docker compose up -d --build
```

Sau khi server lên, đăng ký webhook PayOS (chạy 1 lần):

```
POST webhook_url=https://<your-domain>/webhook/payos
```
