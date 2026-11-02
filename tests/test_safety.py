import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import agent
from app.main import app
from app.persona import DEFAULT_CHARACTER
from app.safety import has_risk_hint, is_crisis

client = TestClient(app)


def chat(message):
    r = client.post("/api/chat", json={"message": message})
    assert r.status_code == 200
    return [json.loads(line) for line in r.text.splitlines()]


@pytest.mark.parametrize("message", [
    "죽 고 싶 어",                       # 띄어 쓴 말
    "그냥 없어지고 싶다",
    "그만 살고 싶다",
    "요즘은 살고 싶지 않아",
    "다 끝내고 싶어. 아무 의미가 없어",  # 조심 표현이 둘 함께
])
def test_crisis_expressions_outside_old_keywords(message):
    assert is_crisis(message)


@pytest.mark.parametrize("message", [
    "오늘 시험 망해서 죽을 것 같아 ㅋㅋ",  # 일상적 과장
    "그 과자 살까?",                      # 띄어쓰기를 무시해도 '자살'로 읽지 않는다
    "오늘 안에 일 다 끝내고 싶어",
    "다 끝내 버리고 싶어",                # 혼자 나오면 위기로 단정하지 않고 조심 메모만
])
def test_everyday_words_are_not_crisis(message):
    assert not is_crisis(message)


def test_risk_hint_alone_adds_caution_note_instead_of_fixed_reply():
    assert has_risk_hint("다 끝내 버리고 싶어")
    assert not has_risk_hint("내일 서울 날씨 어때?")
    events = chat("다 끝내 버리고 싶어")
    text = "".join(e["text"] for e in events if e["type"] == "delta")
    turn = next(e for e in events if e["type"] == "turn")
    assert "109" not in text
    assert any("[조심]" in g for g in turn["guards"])


def test_caution_note_asks_model_for_hotline():
    now =datetime(2026, 11, 2, 14, 0, tzinfo=timezone.utc)
    plain = agent.system_prompt(DEFAULT_CHARACTER, now, "서울", [], None)
    careful = agent.system_prompt(DEFAULT_CHARACTER, now, "서울", [], None, caution=True)
    assert "[조심]" not in plain and "[조심]" in careful and "109" in careful.split("[조심]")[1]
