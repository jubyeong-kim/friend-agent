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
