"""Langfuse 에 닿지 못할 때 요청이 오래 멈추지 않는지, 임시 진단 로그가 원문을 남기지 않는지. 실제 Langfuse 는 부르지 않는다."""
import time
from types import SimpleNamespace

from app import tracing

TRACE = "0123456789abcdef0123456789abcdef"


class SlowLangfuse:
    """flush 가 오래 걸리는(닿지 못하는) Langfuse 흉내."""

    def __init__(self, seconds):
        self.seconds = seconds
        self.scores = []

    def create_score(self, **kwargs):
        self.scores.append(kwargs)

    def flush(self):
        time.sleep(self.seconds)


def test_score_waits_at_most_score_flush_wait(monkeypatch):
    slow = SlowLangfuse(3)
    monkeypatch.setattr(tracing, "_client", slow)
    start = time.perf_counter()
    assert tracing.score(TRACE, "user-feedback", 1) is True
    assert time.perf_counter() - start < tracing.SCORE_FLUSH_WAIT + 0.5
    assert len(slow.scores) == 1


def test_trace_is_recorded_even_if_request_says_not_sampled(monkeypatch):
    """Cloud Run 앞단이 '표본 아님'(traceparent 끝 -00)으로 보낸 요청에서도 chat-turn 이 기록된다.
    FastAPI 내장 추적이 켜져 있으면 그 결정을 부모로 물려받아 트레이스가 통째로 빠졌다."""
    from fastapi.testclient import TestClient
    from langfuse import Langfuse

    from app.main import app

    monkeypatch.setattr(tracing, "_client", Langfuse(public_key="pk-lf-test", secret_key="sk-lf-test",
                                                     base_url="http://127.0.0.1:9", timeout=1))  # 닿지 않는 주소
    roots = []
    real_trace = tracing.trace
    monkeypatch.setattr(tracing, "trace", lambda *a, **k: roots.append(real_trace(*a, **k)) or roots[-1])
    incoming = "00-" + "1" * 32 + "-" + "2" * 16 + "-00"
    with TestClient(app).stream("POST", "/api/chat", json={"message": "안녕"}, headers={"traceparent": incoming}) as r:
        for _ in r.iter_lines():
            pass
    ctx = roots[0].span._otel_span.get_span_context()
    assert ctx.trace_flags.sampled
    assert ctx.trace_id != int("1" * 32, 16)  # 들어온 traceparent 를 부모로 삼지 않았다


class FakeExporter:
    def export(self, spans):
        return SimpleNamespace(name="SUCCESS")

    def shutdown(self):
        return "shut"


def test_export_log_keeps_only_counts_and_trace_prefix(monkeypatch, capsys):
    span = SimpleNamespace(context=SimpleNamespace(trace_id=int(TRACE, 16)), attributes={"input": "비밀 대화"})
    wrapped = tracing._ExportLog(FakeExporter())
    assert wrapped.export([span, span]).name == "SUCCESS"
    assert wrapped.shutdown() == "shut"           # 나머지는 그대로 넘긴다
    out = capsys.readouterr().out
    assert '"spans": 2' in out and TRACE[:8] in out
    assert "비밀 대화" not in out and TRACE not in out  # 원문도, 트레이스 ID 전체도 없다
