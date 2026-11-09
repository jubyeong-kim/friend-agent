"""'다음 공휴일'처럼 기간을 말하지 않고 다음 공휴일을 물으면 공휴일 조회 기간을 앞으로 1년으로 넓힌다.

15번 staging 측정(11/9)에서 모델이 '다음 공휴일 언제야?'를 11/9 ~ 11/22 처럼 2주만 조회했다(확신값 0.9).
문장은 측정 시나리오의 지어낸 문장이다. 오늘은 2026-11-09(월) 낮 12시다.
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


def holidays(start, end):
    return {"name": "get_holidays", "args": {"from": start, "to": end}}


@pytest.mark.parametrize("message", ["다음 공휴일 언제야?", "다음 빨간 날 언제야?", "다음 연휴 언제야?",
                                     "다음 쉬는 날 며칠이야?", "다음 휴일까지 얼마나 남았어?", "제일 가까운 공휴일 언제야?"])
def test_next_holiday_looks_a_year_ahead(message):
    r = router.choose(message, [], NOW, "서울", Picks(holidays("2026-11-09", "2026-11-22")))
    assert r.action == "answer"
    assert r.tools == [{"name": "get_holidays", "args": {}}]  # 빈 기간 = 공휴일 도구가 오늘부터 1년을 본다
    assert any("앞으로 1년" in g for g in r.guards)


def test_split_ranges_become_one():
    # 같은 문장을 5번 보내면 한 번은 기간을 둘로 쪼개 골랐다(도구 선택 흔들림)
    r = router.choose("다음 공휴일 언제야?", [], NOW, "서울",
                      Picks(holidays("2026-11-09", "2026-11-15"), holidays("2026-11-16", "2026-11-22")))
    assert r.tools == [{"name": "get_holidays", "args": {}}]


@pytest.mark.parametrize("message, tools", [
    ("다음 주에 공휴일 있어?", [holidays("2026-11-16", "2026-11-22")]),
    ("12월에 연휴 있어?", [holidays("2026-12-01", "2026-12-31")]),
    ("다음 달에 쉬는 날 있어?", [holidays("2026-12-01", "2026-12-31")]),
    ("크리스마스 다음 공휴일은 언제야?", [holidays("2026-12-26", "2027-01-01")]),
    ("12월 지나고 다음 연휴는?", [holidays("2027-01-01", "2027-02-28")]),
    ("연말에 쉬는 날 있어?", [holidays("2026-11-09", "2026-12-31")]),
])
def test_said_period_is_kept(message, tools):
    r = router.choose(message, [], NOW, "서울", Picks(*tools))
    assert r.tools == tools
    assert not any("앞으로 1년" in g for g in r.guards)


def test_other_tools_are_untouched():
    weather = {"name": "get_weather", "args": {"location": "서울", "date": "2026-11-10"}}
    r = router.choose("내일 날씨랑 다음 공휴일도 알려 줘", [], NOW, "서울",
                      Picks(weather, holidays("2026-11-09", "2026-11-22")))
    assert r.tools == [weather, {"name": "get_holidays", "args": {}}]
