from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from api.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Call(Base):
    __tablename__ = "calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    room_name: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    caller_phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    appointment_date: Mapped[str | None] = mapped_column(String(32), nullable=True)
    appointment_raw: Mapped[str | None] = mapped_column(String(256), nullable=True)
    ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "room_name": self.room_name,
            "caller_phone": self.caller_phone,
            "appointment_date": self.appointment_date,
            "appointment_raw": self.appointment_raw,
            "ended_at": self.ended_at.isoformat(),
        }
