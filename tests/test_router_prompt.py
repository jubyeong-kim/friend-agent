"""도구 선택 지시문을 Langfuse 에서 가져오고, 못 가져오면 코드의 글을 쓰는지. 실제 Langfuse 와 모델은 부르지 않는다."""
from datetime import datetime

from app import router, tracing

NOW = datetime.fromisoformat("2026-10-12T14:00:00+09:00")


class FakePrompt:
    """Langfuse 텍스트 프롬프트 흉내. 채운 값을 기억한다."""

    def __init__(self, text):
        self.text = text
        self.values = None

    def compile(self, **values):
        self.values = values
        return self.text


class FakeLangfuse:
    def __init__(self, prompt=None, error=None):
        self.prompt, self.error, self.asked = prompt, error, []

    def get_prompt(self, name, **kwargs):
        self.asked.append((name, kwargs["label"]))
        if self.error:
            raise self.error
        return self.prompt


class FakeLLM:
    def __init__(self):
        self.systems = []

    def complete_json(self, system, user):
        self.systems.append(system)
        return {"tools": [], "confidence": 0.9, "reason": "일상 대화"}


def test_code_prompt_fills_every_variable():
    text = router.router_prompt()
    assert "{{" not in text
    assert "- get_weather:" in text
    assert f"최대 {router.MAX_REMINDERS}개" in text and f"최대 {router.MAX_TOOLS}개" in text


def test_langfuse_prompt_is_used_with_the_same_values(monkeypatch):
    prompt = FakePrompt("Langfuse 버전")
    fake = FakeLangfuse(prompt)
    monkeypatch.setattr(tracing, "_client", fake)
    monkeypatch.setenv("LANGFUSE_PROMPT_LABEL", "staging")
    llm = FakeLLM()
    router.model_choose("안녕", [], NOW, "서울", llm)
    assert llm.systems == ["Langfuse 버전"]
    assert fake.asked == [(router.PROMPT_NAME, "staging")]
    assert set(prompt.values) == {"tools", "max_reminders", "max_tools"}


def test_label_defaults_to_production(monkeypatch):
    fake = FakeLangfuse(FakePrompt("x"))
    monkeypatch.setattr(tracing, "_client", fake)
    monkeypatch.delenv("LANGFUSE_PROMPT_LABEL", raising=False)
    tracing.get_prompt(router.PROMPT_NAME)
    assert fake.asked == [(router.PROMPT_NAME, "production")]


def test_code_prompt_is_used_when_langfuse_fails(monkeypatch):
    monkeypatch.setattr(tracing, "_client", FakeLangfuse(error=TimeoutError()))
    llm = FakeLLM()
    router.model_choose("안녕", [], NOW, "서울", llm)
    assert llm.systems == [router.router_prompt()]


def test_code_prompt_is_used_when_tracing_is_off(monkeypatch):
    monkeypatch.setattr(tracing, "_client", None)
    llm = FakeLLM()
    router.model_choose("안녕", [], NOW, "서울", llm)
    assert llm.systems == [router.router_prompt()]
