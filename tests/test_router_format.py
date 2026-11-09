"""도구 선택 출력에서 confidence · reason 이 tools 목록 안에 들어온 경우(형식 깨짐)를 읽는 방법."""
import pytest

from app import router, when


class Fixed:  # 정해 둔 출력을 그대로 돌려주는 모델
    can_reason = True

    def __init__(self, data):
        self.data = data

    def complete_json(self, system, user):
        return self.data


def choose(data, message="다음 공휴일 언제야?"):
    return router.choose(message, [], when.now_in("Asia/Seoul"), "서울", Fixed(data))


HOLIDAYS = {"name": "get_holidays", "args": {"from": "2026-11-09", "to": "2027-11-08"}}


def test_confidence_inside_tools_is_read():
    c = choose({"tools": [HOLIDAYS, {"confidence": 0.9, "reason": "공휴일을 물었다"}]})
    assert [t["name"] for t in c.tools] == ["get_holidays"]
    assert c.confidence == 0.9 and c.action == "answer" and c.reason == "공휴일을 물었다"
    assert any("도구 목록 안에" in g for g in c.guards)


def test_normal_output_is_unchanged():
    c = choose({"tools": [HOLIDAYS], "confidence": 0.9, "reason": "공휴일을 물었다"})
    assert c.confidence == 0.9 and c.action == "answer"
    assert not any("도구 목록 안에" in g for g in c.guards)


def test_top_level_confidence_wins_over_inner():
    c = choose({"tools": [HOLIDAYS, {"confidence": 0.95}], "confidence": 0.2, "reason": "모호하다"})
    assert c.confidence == 0.2 and c.action == "clarify"


@pytest.mark.parametrize("data", [
    {"tools": [HOLIDAYS, {"confidence": 0.3, "reason": "모호하다"}]},  # 안쪽 확신이 낮으면 그대로 되묻는다
    {"tools": [HOLIDAYS]},                                               # 어디에도 없으면 지금처럼 0
])
def test_low_or_missing_confidence_still_asks_back(data):
    c = choose(data)
    assert c.action == "clarify" and c.tools == []
