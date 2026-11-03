import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app import memory, router, when
from app.main import app

client = TestClient(app)
KST = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 11, 2, 14, 0, tzinfo=KST)  # 월요일 오후 2시


class Picks:  # 모델 대신 정해 둔 도구 선택을 돌려준다
    can_reason = True

    def __init__(self, *tools):
        self.tools = list(tools)

    def complete_json(self, system, user):
        return {"tools": self.tools, "confidence": 0.9, "reason": "정해 둔 응답"}


def reminder(text, due_at):
    return {"name": "create_reminder", "args": {"text": text, "due_at": due_at}}


@pytest.mark.parametrize("text, expected", [
    ("내일모레 3시에 치과", "11/04 15:00"),     # '내일'보다 '내일모레'를 먼저 본다
    ("내일 모레 3시에 치과", "11/04 15:00"),
    ("내일 3시에 치과", "11/03 15:00"),
    ("밤 12시에 약 먹기", "11/03 00:00"),       # 낮 12시가 아니라 그날 밤이 끝나는 자정
    ("오늘 밤 12시", "11/03 00:00"),
    ("내일 밤 12시", "11/04 00:00"),
    ("밤 12시 30분", "11/03 00:30"),
    ("자정에 알려 줘", "11/03 00:00"),
    ("새벽 12시", "11/03 00:00"),
    ("점심 12시", "11/03 12:00"),
    ("7시에 일어나라고", "11/03 07:00"),        # 일어나기 · 깨우기는 아침
    ("내일 7시에 깨워 줘", "11/03 07:00"),
    ("7시에 운동하라고", "11/02 19:00"),        # 그 밖의 1 ~ 7시는 지금처럼 오후
    ("어제 3시", "11/01 15:00"),               # 지난 시각을 그대로 돌려줘 약속 저장이 거절한다
])
def test_parse_when(text, expected):
    assert when.parse_when(text, NOW).strftime("%m/%d %H:%M") == expected


@pytest.mark.parametrize("text", ["3시 70분에 회의", "25시에 알려 줘"])
def test_impossible_clock_is_not_read(text):
    assert when.parse_when(text, NOW) is None


def test_day_offset():
    assert when.day_offset("내일모레 치과", NOW) == 2
    assert when.day_offset("어제 날씨", NOW) == -1


def test_impossible_clock_makes_rule_ask_back():
    choice = router.choose("3시 70분에 회의하라고 알려 줘", [], NOW, "서울", None)
    assert choice.action == "clarify" and not choice.tools


def test_impossible_clock_does_not_break_chat():
    events = [json.loads(line) for line in client.post("/api/chat", json={"message": "3시 70분에 회의하라고 알려 줘"}).text.splitlines()]
    assert not [e for e in events if e["type"] == "error"]
    assert next(e for e in events if e["type"] == "turn")["action"] == "clarify"


def test_model_midnight_is_fixed():
    choice = router.choose("밤 12시에 약 먹으라고 알려 줘", [], NOW, "서울", Picks(reminder("약 먹기", "2026-11-02T21:00:00+09:00")))
    assert choice.tools[0]["args"]["due_at"] == "2026-11-03T00:00:00+09:00"
    assert any("자정" in g for g in choice.guards)


def test_model_midnight_left_alone_when_right_or_many():
    right = router.choose("밤 12시에 약 먹으라고 알려 줘", [], NOW, "서울", Picks(reminder("약 먹기", "2026-11-03T00:00:00+09:00")))
    assert right.tools[0]["args"]["due_at"] == "2026-11-03T00:00:00+09:00" and not right.guards
    many = router.choose("밤 12시에 약, 아침 8시에 운동 알려 줘", [], NOW, "서울",
                         Picks(reminder("약 먹기", "2026-11-03T00:00:00+09:00"), reminder("운동", "2026-11-03T08:00:00+09:00")))
    assert [t["args"]["due_at"][11:16] for t in many.tools] == ["00:00", "08:00"]


def test_memory_event_text_with_day_after_tomorrow():
    assert memory.event_text("내일모레 회사 면접", NOW) == "11월 4일(수) 회사 면접"
