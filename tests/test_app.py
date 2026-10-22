import json
from datetime import date, datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app import memory, router, tools, when
from app.main import app, store

client = TestClient(app)


def chat(message):
    r = client.post("/api/chat", json={"message": message})
    assert r.status_code == 200
    return [json.loads(line) for line in r.text.splitlines()]


def test_health():
    assert client.get("/health").json() == {"status": "ok"}


def test_weather_question_picks_weather_tool():
    turn = next(e for e in chat("내일 서울 날씨 어때?") if e["type"] == "turn")
    assert [t["name"] for t in turn["tools"]] == ["get_weather"]


def test_crisis_message_gets_fixed_safety_reply():
    events = chat("요즘 너무 죽고 싶어")
    text = "".join(e["text"] for e in events if e["type"] == "delta")
    assert "109" in text


def test_reminder_is_saved_with_timezone():
    chat("내일 아침 8시에 물 마시라고 알려 줘")
    pending = store.list_reminders("local-user", status="pending")
    assert pending and pending[-1]["due_at"].tzinfo is not None


def test_due_reminder_is_delivered_only_once():
    due = datetime.now(timezone.utc) - timedelta(minutes=1)
    store.add_reminder("local-user", "테스트 약속", due)
    now = datetime.now(timezone.utc)
    first = store.claim_due_reminders("local-user", now)
    second = store.claim_due_reminders("local-user", now)
    assert [r["text"] for r in first].count("테스트 약속") == 1
    assert "테스트 약속" not in [r["text"] for r in second]


def test_start_event_comes_first():
    events = chat("안녕")
    assert events[0]["type"] == "start" and "instance_age" in events[0]


def test_cut_off_message_is_left_out_of_context():
    store.delete_all("local-user")
    store.add_message("local-user", "user", "끊긴 말")          # 답이 저장되지 않은 말(요청이 끊김)
    store.add_message("local-user", "user", "다음 말")
    store.add_message("local-user", "assistant", "다음 답")
    _, _, recent = memory.build_context(store, "local-user")
    assert [m["content"] for m in recent] == ["다음 말", "다음 답"]
    shown = client.get("/api/messages").json()
    assert [m["answered"] for m in shown] == [False, True, True]


def test_same_reminder_is_not_saved_twice():
    store.delete_all("local-user")
    chat("내일 아침 8시에 물 마시라고 알려 줘")
    events = chat("내일 아침 8시에 물 마시라고 알려 줘")
    assert len(store.list_reminders("local-user", status="pending")) == 1
    note = next(e for e in events if e["type"] == "reminder")
    assert "이미" in note["summary"]


def test_event_memory_keeps_date_and_time():
    store.delete_all("local-user")
    chat("내일 6시에 면접 있어")
    event = next(m for m in client.get("/api/memories").json() if m["kind"] == "event")
    assert "18:00" in event["text"] and event["time"] == "18:00" and event["date"]


def test_upcoming_reminders_go_into_the_answer_context():
    store.delete_all("local-user")
    store.add_reminder("local-user", "엄마한테 전화", datetime.now(timezone.utc) + timedelta(hours=3))
    ctx = next(e for e in chat("오늘 좀 피곤하다") if e["type"] == "context")
    assert any("엄마한테 전화" in line for line in ctx["upcoming"])


def test_reminder_question_lists_reminders_instead_of_asking_back():
    class Unsure:  # 실습에서 본 모델 응답: 도구 없이 확신도 0
        can_reason = True

        def complete_json(self, system, user):
            return {"tools": [], "confidence": 0.0, "reason": "모르겠음"}

    now = datetime.now(timezone.utc)
    choice = router.choose("나의 내일 약속에 대해 아는 게 있어?", [], now, "서울", Unsure())
    assert [t["name"] for t in choice.tools] == ["list_reminders"] and choice.action == "answer"


def test_event_day_reads_month_day_and_time():
    ref = datetime(2026, 12, 20, tzinfo=timezone.utc)
    assert when.event_day("1월 3일(일) 09:30 신년 산행", ref) == (date(2027, 1, 3), "09:30")
    assert when.event_day("10월 16일(금) 자격증 시험", ref) == (date(2026, 10, 16), None)
    assert when.event_day("고양이를 좋아함", ref) == (None, None)


def test_route_question_picks_route_tool():
    now = datetime(2026, 10, 12, 14, 0, tzinfo=timezone(timedelta(hours=9)))
    choice = router.rule_choose("6시 퇴근하고 낙성대에서 인왕산 가면 몇 시간 걸려?", now, "서울")
    assert choice.tools == [{"name": "get_route", "args": {
        "origin": "낙성대", "destination": "인왕산", "depart_at": "2026-10-12T18:00:00+09:00"}}]
    late = router.rule_choose("그럼 7시까지 경복궁역 갈 수 있어?", now, "서울").tools[0]["args"]
    assert late["destination"] == "경복궁역" and late["arrive_by"].startswith("2026-10-12T19:00")
    assert router.rule_choose("어제 인왕산 가는 데 2시간이나 걸렸어", now, "서울").tools == []


def test_model_route_calls_are_merged_and_clock_fixed():
    class Split:  # 배포 뒤 실제로 본 모델 응답: 출발용·도착용으로 나눠 두 번, 출발 시각엔 지금 시각
        can_reason = True

        def complete_json(self, system, user):
            return {"tools": [
                {"name": "get_route", "args": {"origin": "낙성대역", "destination": "경복궁역", "depart_at": "2026-10-21T15:45:00+09:00"}},
                {"name": "get_route", "args": {"origin": "낙성대역", "destination": "경복궁역", "arrive_by": "2026-10-21T19:00:00+09:00"}}],
                "confidence": 0.9, "reason": "두 번 요청"}

    now = datetime(2026, 10, 21, 15, 45, tzinfo=timezone(timedelta(hours=9)))
    choice = router.choose("6시에 퇴근하고 낙성대역에서 경복궁역 가면 7시까지 도착할 수 있어?", [], now, "서울", Split())
    assert choice.tools == [{"name": "get_route", "args": {"origin": "낙성대역", "destination": "경복궁역",
                                                           "depart_at": "2026-10-21T18:00:00+09:00", "arrive_by": "2026-10-21T19:00:00+09:00"}}]


def test_route_tool_without_key_does_not_guess(monkeypatch):
    monkeypatch.delenv("KAKAO_REST_KEY", raising=False)
    result = tools.get_route({"origin": "낙성대역", "destination": "인왕산"}, tools.Context(store, "local-user", "서울", "Asia/Seoul"))
    assert not result.ok and "짐작" in result.summary


def test_route_tool_adds_travel_time_to_departure(monkeypatch):
    # 카카오 응답 모양은 공식 문서(developers.kakao.com/docs/ko/kakaomap/rest-api)의 예시를 따른다
    def fake_kakao(path, params):
        if "keyword" in path:
            return {"documents": [{"place_name": params["query"], "x": "126.96", "y": "37.47"}]}
        return {"status": "OK", "routes": [{"properties": {"totalTime": 2280, "transfers": 1, "fare": {"value": 1550}}, "steps": [
            {"properties": {"type": "WALKING", "time": 240}},
            {"properties": {"type": "SUBWAY", "time": 1320, "vehicles": [{"name": "2호선", "type": ""}]}},
            {"properties": {"type": "SUBWAY", "time": 540, "vehicles": [{"name": "3호선", "type": ""}]}},
            {"properties": {"type": "WALKING", "time": 180}}]}]}
    monkeypatch.setenv("KAKAO_REST_KEY", "test")
    monkeypatch.setattr(tools, "kakao", fake_kakao)
    ctx = tools.Context(store, "local-user", "서울", "Asia/Seoul")
    result = tools.get_route({"origin": "낙성대역", "destination": "경복궁역", "depart_at": "18:00", "arrive_by": "19:00"}, ctx)
    assert result.ok and "38분" in result.summary and "18:38쯤 도착" in result.summary and "22분 여유" in result.summary
    card = result.data["card"]
    assert [leg["name"] for leg in card["legs"]] == ["도보", "2호선", "3호선", "도보"] and card["legs"][1]["color"] == "#00a84d"


def test_ocr_is_off_by_default():
    r = client.post("/api/ocr", files={"photo": ("a.png", b"x", "image/png")})
    assert r.status_code == 404
