"""앞 대화 없이 '그날 비 와?'처럼 가리키는 날을 알 수 없으면 날짜를 짐작하지 않고 되묻는다.

15번 staging 재측정(11/9)에서 모델이 '그날'을 오늘로 정해 날씨를 조회했다(확신값 0.9).
문장은 측정 시나리오의 지어낸 문장이다. 오늘은 2026-11-09(월) 낮 12시다.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app import router
from app.llm import FakeLLM

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=ZoneInfo("Asia/Seoul"))


class Picks:  # 모델 대신 정해 둔 도구 선택을 확신도 0.9 로 돌려준다
    can_reason = True

    def __init__(self, *tools):
        self.tools = list(tools)

    def complete_json(self, system, user):
        return {"tools": self.tools, "confidence": 0.9, "reason": "정해 둔 응답"}


def weather(day="2026-11-09", location="서울"):
    return {"name": "get_weather", "args": {"location": location, "date": day}}


def air(day="2026-11-09"):
    return {"name": "get_air_quality", "args": {"location": "서울", "date": day}}


@pytest.mark.parametrize("message, tool", [
    ("그날 비 와?", weather()),
    ("그날 날씨 어때?", weather()),
    ("그때 비 온대?", weather("2026-11-10")),
    ("그 날 미세먼지 심해?", air()),
    ("그날 우산 챙겨야 돼?", weather()),
    ("그날 3시에 치과 간다고 알려 줘", {"name": "create_reminder", "args": {"text": "치과", "due_at": "2026-11-09T15:00:00+09:00"}}),
])
def test_unknown_day_asks_back(message, tool):
    r = router.choose(message, [], NOW, "서울", Picks(tool))
    assert r.action == "clarify"
    assert r.tools == [] and r.dropped == []
    assert r.confidence == 0.9  # 모델 확신값은 그대로 남긴다
    assert any("그날 · 그때" in g for g in r.guards)


def test_rule_mode_also_asks_back():
    r = router.choose("그날 비 와?", [], NOW, "서울", FakeLLM())
    assert r.action == "clarify" and r.tools == []


def test_history_lets_the_model_decide():
    history = [{"role": "user", "content": "토요일에 부산 가"}, {"role": "assistant", "content": "좋겠다!"}]
    r = router.choose("그날 비 와?", history, NOW, "서울", Picks(weather("2026-11-14", "부산")))
    assert r.action == "answer"
    assert r.tools == [weather("2026-11-14", "부산")]


@pytest.mark.parametrize("message, tools", [
    ("토요일에 부산 가는데 날씨랑 그날 쉬는 날인지도 알려 줘",
     [weather("2026-11-14", "부산"), {"name": "get_holidays", "args": {"from": "2026-11-14", "to": "2026-11-14"}}]),
    ("다음 주 금요일 소풍인데 그날 날씨 어때?", [weather("2026-11-20")]),
    ("크리스마스에 데이트하는데 그날 눈 와?", [weather("2026-12-25")]),
    ("12월 3일에 시험인데 그날 날씨 어때?", [weather("2026-12-03")]),
    ("이번 주말에 캠핑 가는데 그때 비 와?", [weather("2026-11-14")]),
])
def test_day_in_message_is_kept(message, tools):
    r = router.choose(message, [], NOW, "서울", Picks(*tools))
    assert r.action == "answer"
    assert [t["name"] for t in r.tools] == [t["name"] for t in tools]


@pytest.mark.parametrize("message, tools", [
    ("그 날씨면 우산 챙겨야 돼?", [weather()]),            # '그 날씨'는 날을 가리키지 않는다
    ("그 때문에 마스크 써야 돼?", [air()]),                 # '그 때문에'도 아니다
    ("그때 약속 뭐였지?", [{"name": "list_reminders", "args": {}}]),  # 날짜가 필요 없는 도구
    ("그때 진짜 재밌었어", []),                             # 도구를 안 골랐다
])
def test_other_words_are_untouched(message, tools):
    r = router.choose(message, [], NOW, "서울", Picks(*tools))
    assert r.action == "answer"
    assert r.tools == tools
