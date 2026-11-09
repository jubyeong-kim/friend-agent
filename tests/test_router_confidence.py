"""날짜 교차 확인: 모델이 넣은 날짜가 메시지의 날짜 말과 다르면 코드가 셈한 날짜로 고친다.

15번 기준 측정에서 모델의 확신값은 맞을 때도 틀릴 때도 0.9 ~ 1.0 이었다(120턴 중 115턴).
문장은 기준 측정 시나리오의 지어낸 문장이다. 오늘은 2026-11-09(월) 낮 12시다.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app import router

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=ZoneInfo("Asia/Seoul"))


class Picks:  # 모델 대신 정해 둔 도구 선택을 확신도 0.9 로 돌려준다
    can_reason = True

    def __init__(self, *tools):
        self.tools = list(tools)

    def complete_json(self, system, user):
        return {"tools": self.tools, "confidence": 0.9, "reason": "정해 둔 응답"}


def choose(message, *tools):
    return router.choose(message, [], NOW, "서울", Picks(*tools))


def reminder(due_at):
    return {"name": "create_reminder", "args": {"text": "알림", "due_at": due_at}}


def weather(day, location="서울"):
    return {"name": "get_weather", "args": {"location": location, "date": day}}


def air(day, location="서울"):
    return {"name": "get_air_quality", "args": {"location": location, "date": day}}


# 기준 측정 · 로컬 실측에서 확신도 0.9 ~ 1.0 으로 틀린 날짜를 넣은 턴들. 날짜만 고치고 시각과 확신도는 그대로 둔다
@pytest.mark.parametrize("message, tools, fixed", [
    ("다음 주 금요일 오후 3시에 치과 간다고 알려 줘", [reminder("2026-11-16T15:00:00+09:00")],
     ["2026-11-20T15:00:00+09:00"]),
    ("이번 주 금요일 시험인데 시험 전날 밤에 일찍 자라고 알려 줘", [reminder("2026-11-11T21:00:00+09:00")],
     ["2026-11-12T21:00:00+09:00"]),
    ("이번 주 금요일 시험 전날 밤에 일찍 자라고 알려 줘", [reminder("2026-11-13T21:00:00+09:00")],   # 전날을 빠뜨림
     ["2026-11-12T21:00:00+09:00"]),
    ("다음 주 화요일 아침에 분리수거하라고 알려 줘", [reminder("2026-11-10T08:00")],             # 시간대가 없는 값
     ["2026-11-17T08:00:00+09:00"]),
    ("토요일에 부산 가는데 날씨랑 그날 쉬는 날인지도 알려 줘", [weather("2026-11-11", "부산")], ["2026-11-14"]),
    ("이번 주 토요일 부산 날씨, 그날 미세먼지 알려 줘", [weather("2026-11-14", "부산"), air("2026-11-11", "부산")],
     ["2026-11-14", "2026-11-14"]),                                                          # 둘째 도구만 틀림
])
def test_wrong_date_is_fixed(message, tools, fixed):
    c = choose(message, *tools)
    assert c.action == "answer" and c.confidence == 0.9
    assert [t["args"].get("due_at") or t["args"].get("date") for t in c.tools] == fixed
    assert any("날짜 말로 셈한" in g for g in c.guards)


def test_same_tool_after_fix_is_kept_once():
    # 기준 측정: '내일 제주 바다 파도 높이 어때?'에 날씨를 내일 · 모레로 두 번 골랐다. 고치면 둘이 같아진다
    c = choose("내일 제주 바다 파도 높이 어때?", weather("2026-11-10", "제주"), weather("2026-11-11", "제주"))
    assert c.tools == [weather("2026-11-10", "제주")]
    assert any("하나만 남겼습니다" in g for g in c.guards)


# 맞게 고른 턴은 그대로 둔다
@pytest.mark.parametrize("message, tools", [
    ("다음 주 금요일 오후 3시에 치과 간다고 알려 줘", [reminder("2026-11-20T15:00:00+09:00")]),
    ("이번 주 금요일 시험인데 시험 전날 밤에 일찍 자라고 알려 줘", [reminder("2026-11-12T21:00:00+09:00")]),
    ("모레 서울 날씨 어때?", [weather("2026-11-11")]),
    ("내일 모레 서울 날씨 어때?", [weather("2026-11-11")]),
    ("이번 주 토요일 부산 비 와?", [weather("2026-11-14", "부산")]),
    ("글피 대구 미세먼지 어때?", [air("2026-11-12", "대구")]),
    ("내일 아침에 약 먹으라고 알려 줘", [reminder("2026-11-10T08:00:00+09:00")]),
    ("오늘 밤 12시에 약 먹으라고 알려 줘", [reminder("2026-11-10T00:00:00+09:00")]),   # 밤 12시는 다음 날 0시
    ("이번 주 수요일 오후 3시 치과 알려 주고 그날 비 오는지도 봐 줘",
     [reminder("2026-11-11T15:00:00+09:00"), weather("2026-11-11")]),
    ("이번 주 토요일 결혼식 하루 전에 정장 다리라고 알려 줘", [reminder("2026-11-13T09:00:00+09:00")]),
    ("다음 주 화요일 아침에 분리수거하라고 알려 줘", [reminder("2026-11-17T08:00")]),          # 시간대가 없는 값
])
def test_right_date_keeps_answer(message, tools):
    c = choose(message, *tools)
    assert c.action == "answer" and len(c.tools) == len(tools)
    assert c.confidence == 0.9
    assert not any("날짜 말로 셈한" in g for g in c.guards)


# 코드가 셈하지 않는 메시지는 견주지 않는다(틀리게 셈해서 맞는 턴까지 되묻지 않게)
@pytest.mark.parametrize("message, tools", [
    # 날짜 말이 둘 이상: '오늘'만 보고 셈하면 금요일 약속이 틀렸다고 잘못 볼 수 있다
    ("다음 주에 공휴일 있는지 보고, 금요일 저녁 7시 저녁 약속 저장하고, 오늘 미세먼지도 알려 줘",
     [reminder("2026-11-13T19:00:00+09:00"), air("2026-11-09")]),
    ("11월 20일 오후 3시에 치과 간다고 알려 줘", [reminder("2026-11-20T15:00:00+09:00")]),   # 적힌 날짜
    ("주말에 비 와?", [weather("2026-11-15")]),
    ("수요일 다음 날 비 와?", [weather("2026-11-12")]),
    ("그날 비 와?", [weather("2026-11-09")]),   # 가리키는 말(그날)은 이 확인으로 잡지 못한다
    ("토요일 공휴일이야?", [{"name": "get_holidays", "args": {"from": "2026-11-11", "to": "2026-11-11"}}]),  # 공휴일은 견주지 않는다
])
def test_not_compared(message, tools):
    c = choose(message, *tools)
    assert c.action == "answer" and c.confidence == 0.9
    assert not any("날짜 말로 셈한" in g for g in c.guards)


def test_said_day():
    assert router.said_day("다음 주 금요일 오후 3시에 치과", NOW, reminder=True).isoformat() == "2026-11-20"
    assert router.said_day("이번 주 금요일 시험 전날 밤", NOW, reminder=True).isoformat() == "2026-11-12"
    assert router.said_day("오늘 밤 12시", NOW, reminder=True).isoformat() == "2026-11-10"
    assert router.said_day("오늘 밤 12시", NOW).isoformat() == "2026-11-09"   # 날씨 날짜는 시각을 보지 않는다
    assert router.said_day("어제 오늘 너무 추웠어", NOW) is None
    assert router.said_day("3일 뒤 날씨", NOW) is None
