import json
from datetime import timedelta
from itertools import count

import pytest
from fastapi.testclient import TestClient

from app import router, tools, when
from app.main import app, store

client = TestClient(app)
users = count(1)


def setup(*names):
    """새 사용자에게 약속을 넣고 (Context, 사용자 ID)를 돌려준다."""
    uid = f"cancel-{next(users)}"
    now = when.now_in("Asia/Seoul")
    for i, name in enumerate(names):
        store.add_reminder(uid, name, now + timedelta(days=1 + i))
    return tools.Context(store, uid, "서울", "Asia/Seoul"), uid


def pending(uid):
    return [r["text"] for r in store.list_reminders(uid, status="pending")]


@pytest.mark.parametrize("names, query, cancelled", [
    (["치과 가기"], "치과 약속", "치과 가기"),
    (["치과 가기"], "약속 해 줘", "치과 가기"),                                    # 무엇인지 말하지 않았고 약속이 하나뿐
    (["운동하기", "친구 만나기"], "운동하기 전에 친구 만나는 약속", "친구 만나기"),  # 겹치는 낱말이 더 많은 쪽
    (["치과 가기", "치과 예약 전화하기"], "치과 예약", "치과 예약 전화하기"),
    (["약 먹기", "운동하기"], "약 먹는 거", "약 먹기"),                             # 한 글자 낱말
    (["약 먹기", "약 먹기 전에 물 마시기"], "약 먹기", "약 먹기"),                   # 이름이 똑같으면 그것
])
def test_cancels_best_matching_reminder(names, query, cancelled):
    ctx, uid = setup(*names)
    r = tools.cancel_reminder({"query": query}, ctx)
    assert r.ok and cancelled in r.summary
    assert pending(uid) == [n for n in names if n != cancelled]


def test_single_reminder_with_other_name_is_not_cancelled():
    ctx, uid = setup("치과 가기")
    r = tools.cancel_reminder({"query": "회의 약속 해 줘"}, ctx)
    assert not r.ok and pending(uid) == ["치과 가기"]


@pytest.mark.parametrize("query", ["치과 약속", "약속"])
def test_tie_or_no_name_asks_back_instead_of_cancelling(query):
    ctx, uid = setup("치과 가기", "치과 예약 전화하기")
    r = tools.cancel_reminder({"query": query}, ctx)
    assert not r.ok and "'치과 가기'" in r.summary and "'치과 예약 전화하기'" in r.summary
    assert pending(uid) == ["치과 가기", "치과 예약 전화하기"]


class NoTools:  # 도구를 하나도 고르지 않는 모델
    can_reason = True

    def complete_json(self, system, user):
        return {"tools": [], "confidence": 0.9, "reason": "도구가 필요 없는 대화"}


def test_cancel_request_gets_cancel_tool_even_if_model_picks_none():
    choice = router.choose("회의 약속 취소해 줘", [], when.now_in("Asia/Seoul"), "서울", NoTools())
    assert [t["name"] for t in choice.tools] == ["cancel_reminder"] and choice.action == "answer"
    assert any("약속 취소" in g for g in choice.guards)


def test_rule_cancel_in_chat_does_not_cancel_other_reminder():
    client.delete("/api/data")
    client.post("/api/chat", json={"message": "내일 저녁 7시에 치과 가라고 알려 줘"})
    events = [json.loads(line) for line in client.post("/api/chat", json={"message": "회의 약속 취소해 줘"}).text.splitlines()]
    tool = next(e for e in events if e["type"] == "tool")
    assert tool["name"] == "cancel_reminder" and not tool["ok"]
    assert pending("local-user") == ["치과 가기"]
