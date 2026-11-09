"""기억 정리(memory.update_memories)가 모델의 계획(add · update · remove)을 저장소에 옮기는 방식."""
from datetime import datetime, timezone

from app import memory
from app.store.memory_store import MemoryStore

UID = "memory-test"
NOW = datetime(2026, 11, 9, 15, 0, tzinfo=timezone.utc)


class PlanLLM:
    """정해 둔 계획을 그대로 돌려주는 가짜 모델."""
    can_reason = True
    model = "plan"

    def __init__(self, plan):
        self.plan = plan

    def complete_json(self, system, user):
        return self.plan


def store_with(*texts):
    store = MemoryStore()
    ids = [store.add_memory(UID, "fact", text)["id"] for text in texts]
    return store, ids


def texts(store):
    return [m["text"] for m in store.list_memories(UID)]


def test_update_and_same_add_leave_one_memory():
    # 모델이 바뀐 직업을 update 하면서 같은 글을 add 에도 넣은 경우(실측 4/4)
    store, (job,) = store_with("직업: 회사원")
    plan = {"add": [{"kind": "fact", "text": "사용자는 대학원생이다"}],
            "update": [{"id": job, "text": "사용자는 대학원생이다"}], "remove": []}
    changes = memory.update_memories(store, UID, "나 회사 그만두고 이제 대학원생이야", "축하해!", NOW, PlanLLM(plan))
    assert texts(store) == ["사용자는 대학원생이다"]
    assert changes["added"] == [] and len(changes["updated"]) == 1


def test_trailing_period_counts_as_same_memory():
    store, _ = store_with("고양이 두 마리 키우고 있다.")
    plan = {"add": [{"kind": "fact", "text": "고양이 두 마리 키우고 있다"}], "update": [], "remove": []}
    memory.update_memories(store, UID, "참고로 나 고양이 두 마리 키워", "귀엽겠다", NOW, PlanLLM(plan))
    assert texts(store) == ["고양이 두 마리 키우고 있다."]


def saved_event(message, text, now=NOW, kind="event"):
    store = MemoryStore()
    plan = {"add": [{"kind": kind, "text": text}], "update": [], "remove": []}
    memory.update_memories(store, UID, message, "그렇구나", now, PlanLLM(plan))
    return texts(store)[0]


def test_vague_next_week_becomes_the_whole_week():
    # NOW 는 11월 9일(월). 모델이 「다음 주」를 보고 수요일을 골라 적은 경우(실측 4/4)
    assert saved_event("나 다음 주에 수술 받아", "11월 18일(수) 수술") == "11월 16일(월)~22일(일) 수술"


def test_vague_this_weekend_becomes_saturday_and_sunday():
    # 12일은 목요일인데 토요일이라고 적은 경우(staging 2/2)
    assert saved_event("나 이번 주말에 제주도 여행 가", "11월 12일(토) 제주도 여행") == "11월 14일(토)~15일(일) 제주도 여행"


def test_vague_period_across_months():
    wed = datetime(2026, 11, 25, 15, 0, tzinfo=timezone.utc)
    assert saved_event("다음 주에 이사해", "12월 2일(수) 이사", now=wed) == "11월 30일(월)~12월 6일(일) 이사"


def test_exact_day_or_start_point_is_left_alone():
    assert saved_event("다음 주 수요일에 수술 받아", "11월 18일(수) 수술") == "11월 18일(수) 수술"
    assert saved_event("다음 주부터 헬스 다녀", "11월 16일(월)부터 헬스") == "11월 16일(월)부터 헬스"
    assert saved_event("다음 주에 수술 받아", "11월 18일(수) 수술", kind="fact") == "11월 18일(수) 수술"


def test_add_is_kept_when_update_points_to_unknown_id():
    # update 가 없는 id 를 가리켜 버려지면, 같은 글의 add 까지 막아서 사실을 잃으면 안 된다
    store, _ = store_with("사용자는 서울에 산다")
    plan = {"add": [{"kind": "fact", "text": "사용자는 대전에 산다"}],
            "update": [{"id": 999999, "text": "사용자는 대전에 산다"}], "remove": []}
    memory.update_memories(store, UID, "나 이번에 대전으로 이사했어", "이사 고생했어", NOW, PlanLLM(plan))
    assert "사용자는 대전에 산다" in texts(store)
