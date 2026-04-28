import os
import tempfile

import pytest


@pytest.fixture(scope="session")
def _flask_app():
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"

    # Import after the env var is set so the engine binds to the temp file.
    from api.app import app as flask_app

    yield flask_app

    os.unlink(db_path)


@pytest.fixture()
def client(_flask_app):
    # Wipe between tests so each one starts on an empty schema.
    from api.database import Base, engine

    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    with _flask_app.test_client() as c:
        yield c


def test_end_of_call_persists_payload(client):
    payload = {
        "room_name": "call-abc-123",
        "caller_phone": "+33612345678",
        "appointment_date": "2026-05-12",
        "appointment_raw": "le 12 mai",
    }
    res = client.post("/end-of-call", json=payload)
    assert res.status_code == 201
    body = res.get_json()
    assert body["id"] > 0
    assert body["room_name"] == "call-abc-123"
    assert body["appointment_date"] == "2026-05-12"
    assert body["caller_phone"] == "+33612345678"


def test_end_of_call_requires_room_name(client):
    res = client.post("/end-of-call", json={"appointment_date": "2026-05-12"})
    assert res.status_code == 400


def test_end_of_call_is_idempotent_on_room_name(client):
    payload = {"room_name": "call-dup", "appointment_date": "2026-05-12"}
    first = client.post("/end-of-call", json=payload)
    second = client.post("/end-of-call", json=payload)
    assert first.status_code == 201
    assert second.status_code == 200
    assert first.get_json()["id"] == second.get_json()["id"]
