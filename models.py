import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, Integer, JSON, String, func
from sqlalchemy.orm import Mapped, mapped_column

from database import Base


class OrderStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    PAID = "PAID"
    CANCELED = "CANCELED"


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, index=True)
    payment_code: Mapped[int | None] = mapped_column(Integer, nullable=True, unique=True, index=True)
    zalo_user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    customer_info: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # {phone, address}
    order_details: Mapped[list | None] = mapped_column(JSON, nullable=True)  # [{name, size, quantity, price}]
    total_amount: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[OrderStatus] = mapped_column(
        Enum(OrderStatus), default=OrderStatus.DRAFT, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:
        return f"<Order id={self.id} user={self.zalo_user_id} status={self.status}>"
