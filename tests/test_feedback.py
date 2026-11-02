"""좋아요·별로예요(/api/feedback)와 신고(/api/report)의 Langfuse 점수. 실제 Langfuse 는 부르지 않는다."""
from fastapi.testclient import TestClient

from app import tracing
from app.auth import LOCAL_USER
from app.main import app, store

client = TestClient(app)
TRACE = "0123456789abcdef0123456789abcdef"


class FakeLangfuse:
    """create_score 로 받은 인자만 모아 둔다."""

    def __init__(self):
        self.scores = []

    def create_score(self, **kwargs):
        self.scores.append(kwargs)

    def flush(self):
        pass


def reply(uid=LOCAL_USER, role="assistant", meta=None):
    return store.add_message(uid, role, "답", {"trace_id": TRACE} if meta is None else meta)


def feedback(message_id, value="up"):
    return client.post("/api/feedback", json={"message_id": message_id, "value": value})


def test_cannot_score_someone_elses_message(monkeypatch):
    fake = FakeLangfuse()
    monkeypatch.setattr(tracing, "_client", fake)
    other = reply(uid="someone-else")
    assert feedback(other["id"]).status_code == 404
    assert client.post("/api/report", json={"message_id": other["id"], "reason": "x"}).json() == {"ok": True}
    assert fake.scores == []


def test_only_my_assistant_reply_can_be_scored(monkeypatch):
    fake = FakeLangfuse()
    monkeypatch.setattr(tracing, "_client", fake)
    assert feedback(reply(role="user")["id"]).status_code == 404   # 내가 보낸 말
    assert feedback(10 ** 9).status_code == 404                    # 없는 메시지
    assert feedback(reply()["id"], value="meh").status_code == 422
    assert fake.scores == []


def test_without_langfuse_keys_feedback_and_report_still_work(monkeypatch):
    monkeypatch.setattr(tracing, "_client", None)
    m = reply()
    assert feedback(m["id"]).json() == {"ok": True, "scored": False}
    assert client.post("/api/report", json={"message_id": m["id"], "reason": "이상해요"}).json() == {"ok": True}


def test_old_reply_without_trace_id_is_skipped(monkeypatch):
    fake = FakeLangfuse()
    monkeypatch.setattr(tracing, "_client", fake)
    assert feedback(reply(meta={"action": "answer"})["id"], "down").json() == {"ok": True, "scored": False}
    assert fake.scores == []


def test_feedback_and_report_scores_overwrite_and_hide_contacts(monkeypatch):
    fake = FakeLangfuse()
    monkeypatch.setattr(tracing, "_client", fake)
    monkeypatch.setenv("ENV", "staging")
    m = reply()
    assert feedback(m["id"], "up").json() == {"ok": True, "scored": True}
    assert feedback(m["id"], "down").json() == {"ok": True, "scored": True}
    client.post("/api/report", json={"message_id": m["id"], "reason": "010-1234-5678 로 연락 a@b.com"})

    up, down, report = fake.scores
    assert (up["name"], up["value"], down["value"]) == ("user-feedback", 1.0, 0.0)
    # 같은 score_id, 같은 시각(답 저장 시각)이라 Langfuse 가 덮어쓴다
    assert up["score_id"] == down["score_id"] == f"{TRACE}-user-feedback"
    assert up["timestamp"] == down["timestamp"] == m["created_at"]
    assert all(s["data_type"] == "BOOLEAN" and s["trace_id"] == TRACE and s["environment"] == "staging"
               for s in fake.scores)
    assert (report["name"], report["value"], report["score_id"]) == ("report", 1.0, f"{TRACE}-report")
    assert report["comment"] == "[전화번호] 로 연락 [이메일]"


def test_saved_reply_carries_trace_id_only_when_tracing_is_on():
    r = client.post("/api/chat", json={"message": "안녕"})
    done = [line for line in r.text.splitlines() if '"done"' in line]
    assert done
    last = store.recent_messages(LOCAL_USER, 1)[-1]
    assert last["role"] == "assistant" and "trace_id" not in last["meta"]  # 테스트는 키가 없어 계측이 꺼져 있다
