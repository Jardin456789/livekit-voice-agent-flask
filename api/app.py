import logging
import os

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from sqlalchemy.exc import IntegrityError

from api.database import SessionLocal, init_db
from api.models import Call

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logger = logging.getLogger("api")

app = Flask(__name__)
init_db()


@app.get("/health")
def health():
    return {"status": "ok"}, 200


@app.post("/end-of-call")
def end_of_call():
    payload = request.get_json(silent=True) or {}

    room_name = payload.get("room_name")
    if not isinstance(room_name, str) or not room_name.strip():
        return jsonify({"error": "room_name is required"}), 400

    record = Call(
        room_name=room_name.strip(),
        caller_phone=payload.get("caller_phone"),
        appointment_date=payload.get("appointment_date"),
        appointment_raw=payload.get("appointment_raw"),
    )

    with SessionLocal() as session:
        session.add(record)
        try:
            session.commit()
        except IntegrityError:
            # Same room_name posted twice — agent retried. Return existing row idempotently.
            session.rollback()
            existing = session.query(Call).filter_by(room_name=room_name.strip()).one()
            logger.info("end-of-call already recorded for room=%s", room_name)
            return jsonify(existing.to_dict()), 200

        logger.info(
            "end-of-call recorded id=%s room=%s phone=%s date=%s",
            record.id,
            record.room_name,
            record.caller_phone,
            record.appointment_date,
        )
        return jsonify(record.to_dict()), 201


if __name__ == "__main__":
    port = int(os.getenv("FLASK_PORT", "5001"))
    app.run(host="0.0.0.0", port=port, debug=False)
