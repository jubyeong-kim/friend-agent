"""A/B 나눠 보내기. 같은 사람은 늘 같은 쪽, 비율대로 나뉘는지, 실험 이름을 바꾸면 새로 섞이는지."""
from datetime import datetime

from app import abtest, router, tracing

USERS = [f"user-{i}" for i in range(1000)]


def test_off_without_experiment_name(monkeypatch):
    monkeypatch.delenv("AB_TEST", raising=False)
    assert abtest.variant("user-1") is None


def test_off_without_user(monkeypatch):
    monkeypatch.setenv("AB_TEST", "router-ab-1")
    assert abtest.variant("") is None


def test_same_user_same_side(monkeypatch):
    monkeypatch.setenv("AB_TEST", "router-ab-1")
    assert {abtest.variant("user-1") for _ in range(5)} in ({"a"}, {"b"})


def test_half_go_to_b_by_default(monkeypatch):
    monkeypatch.setenv("AB_TEST", "router-ab-1")
    monkeypatch.delenv("AB_B_PERCENT", raising=False)
    share = sum(abtest.variant(u) == "b" for u in USERS) / len(USERS)
    assert 0.45 < share < 0.55


def test_percent_zero_and_hundred(monkeypatch):
    monkeypatch.setenv("AB_TEST", "router-ab-1")
    monkeypatch.setenv("AB_B_PERCENT", "0")
    assert {abtest.variant(u) for u in USERS} == {"a"}
    monkeypatch.setenv("AB_B_PERCENT", "100")
    assert {abtest.variant(u) for u in USERS} == {"b"}


def test_new_experiment_name_reshuffles(monkeypatch):
    monkeypatch.setenv("AB_TEST", "router-ab-1")
    first = [abtest.variant(u) for u in USERS]
    monkeypatch.setenv("AB_TEST", "router-ab-2")
    second = [abtest.variant(u) for u in USERS]
    moved = sum(a != b for a, b in zip(first, second)) / len(USERS)
    assert 0.4 < moved < 0.6  # 따로 나눈 것처럼 절반쯤 자리가 바뀐다


def test_variant_label_reaches_langfuse(monkeypatch):
    asked = []

    class FakeLangfuse:
        def get_prompt(self, name, **kwargs):
            asked.append(kwargs["label"])
            raise TimeoutError()  # 가져오지 못해도 코드의 글로 이어 간다

    class FakeLLM:
        def complete_json(self, system, user):
            return {"tools": [], "confidence": 0.9, "reason": "일상 대화"}

    monkeypatch.setattr(tracing, "_client", FakeLangfuse())
    router.model_choose("안녕", [], datetime.fromisoformat("2026-10-12T14:00:00+09:00"), "서울", FakeLLM(),
                        abtest.LABELS["b"])
    assert asked == ["prod-b"]
