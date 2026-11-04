"""크레딧 원장 테스트. 가짜 LLM 과 메모리 저장소로 돈다(conftest.py).

개발용 사용자(local-user)는 conftest 가 테스트마다 크레딧을 채우므로, 여기서는 테스트마다 새 사용자를 쓴다.
"""
import json
import threading
import uuid

import pytest
from fastapi.testclient import TestClient

from app import agent, main
from app.auth import User
from app.main import app, store
from app.store.memory_store import MemoryStore

client = TestClient(app)

NO_CREDITS = {"error": "insufficient_credits",
              "message": "크레딧을 다 썼어요. 충전하면 바로 이어서 이야기할 수 있어요.", "balance": 0}


@pytest.fixture
def uid():
    """이 테스트에서만 쓰는 새 사용자. 요청마다 이 사용자로 로그인한 것처럼 바꿔 끼운다."""
    new = f"credit-test-{uuid.uuid4().hex}"
    app.dependency_overrides[main.current_user] = lambda: User(new, anonymous=True)
    yield new
    app.dependency_overrides.pop(main.current_user, None)


def lines(r):
    return [json.loads(line) for line in r.text.splitlines()]


def test_chat_spends_one_credit(uid):
    """보내면 잔액이 1 줄고, 원장에 chat −1 줄이 하나 생긴다."""
    client.get("/api/me")
    r = client.post("/api/chat", json={"message": "안녕"})
    assert r.status_code == 200 and any(e["type"] == "done" for e in lines(r))
    credits = client.get("/api/credits").json()
    assert credits["balance"] == 19
    spent = [c for c in store.recent_credits(uid, 10) if c["delta"] < 0]
    assert [(c["delta"], c["reason"]) for c in spent] == [(-1, "chat")] and spent[0]["reference"].startswith("chat:")


def test_zero_balance_gets_402_with_exact_body(uid):
    """잔액 0 이면 402 와 정해진 본문(detail 로 감싸지 않음)을 돌려주고, 보낸 말은 저장하지 않는다."""
    r = client.post("/api/chat", json={"message": "안녕"})
    assert r.status_code == 402 and r.json() == NO_CREDITS
    assert store.recent_messages(uid, 10) == []


def test_crisis_message_is_answered_even_at_zero_balance(uid):
    """위기 표현은 잔액 0 이어도 안전 응답(109)이 나가고, 잔액과 원장은 그대로다."""
    r = client.post("/api/chat", json={"message": "요즘 너무 죽고 싶어"})
    assert r.status_code == 200
    assert "109" in "".join(e["text"] for e in lines(r) if e["type"] == "delta")
    assert client.get("/api/credits").json() == {"balance": 0, "recent": []}


def test_reply_error_writes_refund_line(uid, monkeypatch):
    """답을 만들다 오류가 나면 error 줄이 나가고, 같은 값의 refund:chat: +1 줄로 잔액이 돌아온다."""
    def broken_turn(*args, **kwargs):
        yield {"type": "start", "remembers": False, "instance_age": 0}
        raise RuntimeError("답 만들기 실패")

    client.get("/api/me")
    monkeypatch.setattr(agent, "run_turn", broken_turn)
    assert lines(client.post("/api/chat", json={"message": "안녕"}))[-1]["type"] == "error"
    rows = store.recent_credits(uid, 10)
    spent = next(c for c in rows if c["reason"] == "chat")
    refund = next(c for c in rows if c["reason"] == "refund")
    assert (spent["delta"], refund["delta"]) == (-1, 1) and refund["reference"] == "refund:" + spent["reference"]
    assert store.credit_balance(uid) == 20


def test_signup_credits_are_given_once(uid):
    """/api/me 를 두 번 불러도 가입 무료 20 은 한 번만 쌓인다."""
    client.get("/api/me")
    client.get("/api/me")
    credits = client.get("/api/credits").json()
    assert credits["balance"] == 20
    assert [(c["delta"], c["reason"]) for c in credits["recent"]] == [(20, "signup")]


def test_closed_stream_is_not_refunded(uid):
    """사용자가 답 도중 창을 닫은 것처럼 첫 줄 뒤에 스트림을 닫으면(GeneratorExit) 되돌림 줄이 생기지 않는다."""
    store.add_credit(uid, 20, "signup", f"signup:{uid}")
    ref = uuid.uuid4().hex
    assert store.spend_credit(uid, 1, "chat", f"chat:{ref}")["ok"]
    stream = main.chat_lines("안녕", uid, main.get_llm(), ref)
    assert json.loads(next(stream))["type"] == "start"
    stream.close()
    assert [c["reason"] for c in store.recent_credits(uid, 10)] == ["chat", "signup"]
    assert store.credit_balance(uid) == 19


def test_memory_store_spends_last_credit_only_once():
    """메모리 저장소에서 잔액 1 에 두 요청이 동시에 차감하면 한쪽만 성공하고 잔액은 0 이다."""
    mem = MemoryStore()
    mem.add_credit("u", 1, "signup", "signup:u")
    together = threading.Barrier(2)  # 두 스레드가 같은 때에 차감을 시작하게
    results = []

    def spend(i):
        together.wait()
        results.append(mem.spend_credit("u", 1, "chat", f"chat:{i}"))

    threads = [threading.Thread(target=spend, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(r["ok"] for r in results) == [False, True]
    assert mem.credit_balance("u") == 0
