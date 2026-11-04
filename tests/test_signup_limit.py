"""하루 무료 크레딧 상한 테스트. 게스트를 계속 새로 만들어 무료 크레딧을 받는 공격을 막는다.

다른 테스트도 새 사용자를 만들므로, 상한은 「오늘 이미 받은 수 + n」으로 잡아 시험한다.
"""
import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app import config, main
from app.auth import User
from app.main import app, store

client = TestClient(app)


@pytest.fixture(autouse=True)
def restore_login():
    yield
    app.dependency_overrides.pop(main.current_user, None)


def as_user(uid):
    app.dependency_overrides[main.current_user] = lambda: User(uid, anonymous=True)


def new_guest():
    """새 익명 게스트가 처음 들어온 것처럼 /api/me 를 부르고 잔액을 돌려준다."""
    uid = f"guest-{uuid.uuid4().hex}"
    as_user(uid)
    client.get("/api/me")
    return uid, client.get("/api/credits").json()["balance"]


def today():
    return store.count_signups_since(main.signup_day_start())


def test_guests_stop_getting_free_credits_at_daily_limit(monkeypatch):
    """상한에 닿은 뒤 새로 만든 게스트는 무료 크레딧 0, 첫 메시지부터 402. 상한 전 게스트는 20 으로 대화된다."""
    monkeypatch.setattr(main.settings, "signup_daily_limit", today() + 2)
    guests = [new_guest() for _ in range(4)]
    assert [b for _, b in guests] == [20, 20, 0, 0]
    as_user(guests[1][0])
    assert client.post("/api/chat", json={"message": "안녕"}).status_code == 200
    as_user(guests[2][0])
    r = client.post("/api/chat", json={"message": "안녕"})
    assert r.status_code == 402 and r.json()["error"] == "insufficient_credits"


def test_already_granted_user_keeps_credits_after_limit(monkeypatch):
    """이미 받은 사용자는 상한에 닿은 뒤 /api/me 를 다시 불러도 잔액 그대로, signup 줄도 더 생기지 않는다."""
    uid, balance = new_guest()
    assert balance == 20
    monkeypatch.setattr(main.settings, "signup_daily_limit", 0)
    before = today()
    as_user(uid)
    client.get("/api/me")
    assert client.get("/api/credits").json()["balance"] == 20 and today() == before


def test_only_today_signups_are_counted(monkeypatch):
    """어제(한국 날짜) 받은 줄은 오늘 상한에 세지 않는다."""
    yesterday = main.signup_day_start() - timedelta(hours=1)
    with store.lock:  # 메모리 저장소에 어제 시각의 signup 줄을 직접 넣는다
        store.ledger.append({"id": -1, "user_id": "old-guest", "delta": 20, "reason": "signup",
                             "reference": f"signup:old-{uuid.uuid4().hex}", "created_at": yesterday})
    monkeypatch.setattr(main.settings, "signup_daily_limit", today() + 1)
    assert new_guest()[1] == 20


def test_daily_limit_comes_from_config(monkeypatch):
    """기본값은 config 한곳(50), 시험할 때만 환경변수로 바꾼다. 음수는 0 으로 본다."""
    assert config.SIGNUP_DAILY_LIMIT == 50
    monkeypatch.setenv("SIGNUP_DAILY_LIMIT", "")
    assert config.load_settings().signup_daily_limit == 50
    monkeypatch.setenv("SIGNUP_DAILY_LIMIT", "7")
    assert config.load_settings().signup_daily_limit == 7
    monkeypatch.setenv("SIGNUP_DAILY_LIMIT", "-3")
    assert config.load_settings().signup_daily_limit == 0
