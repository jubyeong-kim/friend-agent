"""AI 친구 에이전트 서비스.

화면(/)과 API를 FastAPI 하나가 함께 제공한다.
- /health, /version: 서비스 상태와 배포 버전
- /api/config: 화면이 쓰는 설정(로그인 방식, 개발용 패널 표시 여부)
- /api/me: 로그인한 사용자. 처음 온 사용자에게 가입 무료 크레딧을 한 번 준다
- /api/character, /api/settings: 캐릭터와 사용자 설정
- /api/chat: 메시지 한 턴. 크레딧 1개를 쓰고(위기 표현은 차감하지 않음) 결과를 이벤트(JSON 한 줄씩)로 스트리밍한다
- /api/credits: 남은 크레딧과 최근 기록
- /api/greeting: 앱을 열 때 먼저 말 걸기
- /api/feedback, /api/report: 답에 좋아요·별로예요, 신고. Langfuse 점수로 보낸다(user-feedback, report)
- /api/ocr: 사진 속 글자 읽기(OCR_ENABLED=true 일 때)
- /api/memories, /api/reminders, /api/data: 기억, 약속, 내 데이터 관리
- /api/reminders/{id}/card.png: 약속 하나를 카드 그림으로(한글 글꼴이 필요하다)
- /api/holidays: 달력에 칠할 공휴일
"""
import html
import json
import os
import time
import uuid
from typing import Literal

import httpx
from fastapi import Depends, FastAPI, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from app import agent, card, memory, ocr, tracing, when
from app.auth import Auth, User
from app.config import CREDITS_PER_MESSAGE, ROOT, SIGNUP_CREDITS, load_settings
from app.llm import make_llm
from app.logs import log
from app.persona import DEFAULT_CHARACTER, OPTIONS, build_persona, clean_character
from app.safety import is_crisis
from app.store.base import make_store
from app.tools import CITIES, holidays_of

settings = load_settings()  # 꼭 필요한 값이 없으면 여기서 종료한다
tracing.setup()             # .env 를 읽은 뒤에. Langfuse 키가 없으면 계측만 꺼진다
llm = make_llm(settings)
store = make_store(settings)
current_user = Auth(settings)

# FastAPI 내장 OTel 추적을 끈다. 켜 두면 요청 span 이 들어온 traceparent 를 부모로 삼고, Cloud Run 앞단이
# '표본 아님'으로 표시한 요청에선 그 아래 Langfuse 트레이스까지 기록되지 않아 빠진다(staging 5초 간격에서 절반 넘게).
app = FastAPI(title="AI 친구 에이전트", telemetry={"tracing": False, "operation_spans": False})

# 시작 지연 스위치(배포 실습용). 포트는 바로 열지만 정한 시간 동안은 준비 중이라 모든 요청에 503을 돌려준다.
# AI 서비스가 시작할 때 모델 설정이나 색인을 불러오느라 늦게 준비되는 상황을 흉내 낸다. 기본값 0(끔).
SLOW_START_SECONDS = float(os.environ.get("SLOW_START_SECONDS", "0") or 0)
READY_AT = time.time() + SLOW_START_SECONDS
if SLOW_START_SECONDS:
    log("WARNING", f"시작 지연 스위치가 켜져 있습니다. {SLOW_START_SECONDS:.0f}초 동안 준비 중으로 응답합니다.")


@app.middleware("http")
async def not_ready_yet(request, call_next):
    if time.time() < READY_AT:
        return JSONResponse({"status": "starting"}, status_code=503)
    return await call_next(request)


def get_llm():
    """테스트에서는 이 함수를 가짜 LLM을 돌려주는 함수로 바꿔 끼운다."""
    return llm


class ChatRequest(BaseModel):
    message: str


class SettingsRequest(BaseModel):
    location: str | None = None
    timezone: str | None = None


class ReportRequest(BaseModel):
    message_id: int
    reason: str = ""


class FeedbackRequest(BaseModel):
    message_id: int
    value: Literal["up", "down"]  # 좋아요, 별로예요


@app.get("/", response_class=HTMLResponse)
def home():
    page = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    banner = "" if settings.env in ("production", "local") else f'<div class="banner">테스트 환경 ({settings.env})</div>'
    if os.environ.get("BANNER"):  # 이미지를 새로 만들지 않고 환경변수만 바꿔 새 리비전을 만들 때 쓰는 화면 문구
        banner += f'<div class="banner">{html.escape(os.environ["BANNER"][:80])}</div>'
    return page.replace("<!--ENV_BANNER-->", banner)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/version")
def version():
    return {"commit": settings.commit, "revision": settings.revision, "env": settings.env,
            "llm": settings.provider, "model": settings.model, "storage": settings.storage, "auth": settings.auth,
            "banner": os.environ.get("BANNER", "")}


@app.get("/api/config")
def config():
    # publishable key 는 브라우저에 공개해도 되는 키다. secret key 는 절대 여기에 넣지 않는다.
    return {"env": settings.env, "auth": settings.auth, "storage": settings.storage,
            "supabase_url": settings.supabase_url if settings.auth == "supabase" else "",
            "supabase_publishable_key": settings.supabase_publishable_key if settings.auth == "supabase" else "",
            "google_login": settings.google_login, "show_turn_panel": settings.show_turn_panel,
            "ocr_enabled": settings.ocr_enabled,
            "options": OPTIONS, "cities": list(CITIES)}


@app.get("/api/me")
def me(user: User = Depends(current_user)):
    # 처음 온 사용자에게 무료 크레딧. 참조가 유일해서 여러 번 불러도 한 번만 쌓인다
    if not store.add_credit(user.id, SIGNUP_CREDITS, "signup", f"signup:{user.id}")["duplicate"]:
        log("INFO", "가입 무료 크레딧", amount=SIGNUP_CREDITS)
    return {"id": user.id, "anonymous": user.anonymous, "email": user.email,
            "has_character": store.get_character(user.id) is not None}


@app.get("/api/credits")
def credits(user: User = Depends(current_user)):
    return {"balance": store.credit_balance(user.id),
            "recent": [{"delta": c["delta"], "reason": c["reason"], "created_at": c["created_at"].isoformat()}
                       for c in store.recent_credits(user.id, 10)]}


# ── 캐릭터와 설정 ──────────────────────────────────────────────────

@app.get("/api/character")
def get_character(user: User = Depends(current_user)):
    c = store.get_character(user.id)
    return {"character": c or DEFAULT_CHARACTER, "saved": c is not None}


@app.put("/api/character")
def put_character(data: dict, user: User = Depends(current_user)):
    c = clean_character(data)
    store.save_character(user.id, c)
    log("INFO", "캐릭터 저장")
    return {"character": c}


@app.post("/api/character/preview")
def preview_character(data: dict):
    return {"prompt": build_persona(clean_character(data))}


@app.get("/api/settings")
def get_settings(user: User = Depends(current_user)):
    return store.get_settings(user.id)


@app.put("/api/settings")
def put_settings(req: SettingsRequest, user: User = Depends(current_user)):
    changes = {}
    if req.location in CITIES:
        changes["location"] = req.location
    if req.timezone:
        changes["timezone"] = req.timezone if req.timezone == "Asia/Seoul" else "Asia/Seoul"  # 지금은 한국 시간만
    store.save_settings(user.id, changes)
    return store.get_settings(user.id)


# ── 대화 ───────────────────────────────────────────────────────────

@app.get("/api/messages")
def messages(user: User = Depends(current_user)):
    show = settings.show_turn_panel
    items = store.recent_messages(user.id, 50)
    # answered: 사용자 메시지에 답이 저장됐는지. 요청이 끊겨 답이 없는 말은 화면에 표시하고 다음 답의 문맥에서는 뺀다.
    return [{"id": m["id"], "role": m["role"], "content": m["content"],
             "answered": m["role"] != "user" or memory.is_answered(items, i),
             "meta": m["meta"] if show else {}, "created_at": m["created_at"].isoformat()}
            for i, m in enumerate(items)]


NO_CREDITS_MESSAGE = "크레딧을 다 썼어요. 충전하면 바로 이어서 이야기할 수 있어요."


def chat_lines(message, uid, model, charge_ref):
    """한 턴의 이벤트를 JSON 한 줄씩 내보낸다. charge_ref 는 이번 메시지에 쓴 크레딧의 무작위 값(차감 안 했으면 None).

    답을 만들다 오류가 나면 쓴 크레딧을 되돌린다. 사용자가 창을 닫아 연결이 끊기면 제너레이터가
    GeneratorExit 으로 닫히는데, 이건 Exception 이 아니라 아래 except 에 걸리지 않으므로 되돌리지 않는다.
    """
    try:
        for event in agent.run_turn(message, uid, model, store, settings.show_turn_panel):
            yield json.dumps(event, ensure_ascii=False, default=str) + "\n"
    except Exception as error:
        log("ERROR", "대화 처리 실패", error=type(error).__name__)
        if charge_ref:
            try:
                store.add_credit(uid, CREDITS_PER_MESSAGE, "refund", f"refund:chat:{charge_ref}")
                log("WARNING", "크레딧 되돌림", amount=CREDITS_PER_MESSAGE)
            except Exception as refund_error:
                log("ERROR", "크레딧 되돌림 실패", error=type(refund_error).__name__)
        yield json.dumps({"type": "error", "message": "답을 만들지 못했어요. 잠시 뒤 다시 보내 주세요."}, ensure_ascii=False) + "\n"


@app.post("/api/chat")
def chat(req: ChatRequest, user: User = Depends(current_user), model=Depends(get_llm)):
    message = req.message.strip()[:1000]
    if not message:
        raise HTTPException(400, "메시지가 비어 있습니다")

    # 스트림을 열기 전에 차감한다(사용자 메시지는 스트림 안에서 저장되므로, 402 면 아무것도 저장되지 않는다).
    # 위기 표현은 차감도 거절도 하지 않는다. 잔액이 없어도 안전 응답이 나가야 하기 때문이다(판단은 agent 와 같은 함수).
    charge_ref = None
    if not is_crisis(message):
        charge_ref = uuid.uuid4().hex
        spent = store.spend_credit(user.id, CREDITS_PER_MESSAGE, "chat", f"chat:{charge_ref}")
        if not spent["ok"]:
            log("INFO", "크레딧 부족")
            # HTTPException 은 본문을 detail 로 감싸서, 정해진 본문 그대로 보내려고 JSONResponse 를 돌려준다
            return JSONResponse({"error": "insufficient_credits", "message": NO_CREDITS_MESSAGE,
                                 "balance": spent["balance"]}, status_code=402)

    return StreamingResponse(chat_lines(message, user.id, model, charge_ref), media_type="application/x-ndjson")


@app.post("/api/greeting")
def greeting(user: User = Depends(current_user), model=Depends(get_llm)):
    return {"greeting": agent.greet(user.id, model, store)}


@app.post("/api/ocr")
async def read_photo(photo: UploadFile, user: User = Depends(current_user)):
    if not settings.ocr_enabled:
        raise HTTPException(404, "사진 글자 읽기가 꺼져 있습니다")
    try:
        return {"text": ocr.read_text(await photo.read())}
    except ocr.OcrError as error:
        raise HTTPException(500, str(error)) from error


def my_reply(uid, message_id):
    """로그인한 사용자의 캐릭터 답(assistant)만. 없거나, 남의 것이거나, 내 말이면 None."""
    m = store.get_message(uid, message_id)
    return m if m and m["role"] == "assistant" else None


def score_reply(message, name, value, comment=None):
    """답이 저장될 때 meta 에 넣은 트레이스로 점수를 보낸다. 트레이스 ID 가 없는 지난 답이나 키가 없으면 건너뛴다.
    점수 시각을 답 저장 시각으로 고정해, 같은 답에 다시 누르면 새로 쌓지 않고 덮어쓴다."""
    return tracing.score(message["meta"].get("trace_id"), name, value, comment, timestamp=message["created_at"])


@app.post("/api/feedback")
def feedback(req: FeedbackRequest, user: User = Depends(current_user)):
    message = my_reply(user.id, req.message_id)
    if not message:
        raise HTTPException(404, "답을 찾지 못했습니다")
    scored = score_reply(message, "user-feedback", 1 if req.value == "up" else 0)
    log("INFO", "답 반응", value=req.value, scored=scored)
    return {"ok": True, "scored": scored}


@app.post("/api/report")
def report(req: ReportRequest, user: User = Depends(current_user)):
    log("WARNING", "부적절한 답변 신고", message_id=req.message_id, reason_length=len(req.reason))
    # 응답은 지금처럼 늘 ok. 내 답일 때만 점수를 보낸다. 사유는 가린 뒤 comment 로(로그에는 길이만)
    message = my_reply(user.id, req.message_id)
    if message:
        score_reply(message, "report", 1, req.reason.strip() or None)
    return {"ok": True}


# ── 기억, 약속, 내 데이터 ───────────────────────────────────────────

@app.get("/api/memories")
def list_memories(user: User = Depends(current_user)):
    # 일정(event) 메모는 글 속 날짜와 시각을 꺼내 달력에 놓을 수 있게 한다
    items = []
    for m in store.list_memories(user.id):
        day, clock = when.event_day(m["text"], m["created_at"]) if m["kind"] == "event" else (None, None)
        items.append({"id": m["id"], "kind": m["kind"], "text": m["text"],
                      "date": day.isoformat() if day else None, "time": clock})
    return items


@app.delete("/api/memories/{memory_id}")
def delete_memory(memory_id: int, user: User = Depends(current_user)):
    store.delete_memory(user.id, memory_id)
    return {"ok": True}


@app.delete("/api/memories")
def clear_memories(user: User = Depends(current_user)):
    store.clear_memories(user.id)
    return {"ok": True}


@app.get("/api/reminders")
def list_reminders(user: User = Depends(current_user)):
    tz = when.now_in(store.get_settings(user.id)["timezone"]).tzinfo
    items = []
    for r in store.list_reminders(user.id):
        local = r["due_at"].astimezone(tz)
        items.append({"id": r["id"], "text": r["text"], "status": r["status"], "due_label": when.label(local),
                      "date": local.date().isoformat(), "time": f"{local:%H:%M}"})
    return items


@app.get("/api/reminders/{reminder_id}/card.png")
def reminder_card(reminder_id: int, user: User = Depends(current_user)):
    found = next((r for r in store.list_reminders(user.id) if r["id"] == reminder_id), None)
    if not found:
        raise HTTPException(404, "약속을 찾지 못했습니다")
    tz = when.now_in(store.get_settings(user.id)["timezone"]).tzinfo
    try:
        png = card.draw_reminder(found["text"], when.label(found["due_at"].astimezone(tz)))
    except card.CardError as error:
        raise HTTPException(500, str(error)) from error
    return Response(png, media_type="image/png", headers={"Cache-Control": "no-store"})


@app.get("/api/holidays")
def list_holidays(year: int):
    """달력에 칠할 한국 공휴일. 공휴일 도구와 같은 공개 API와 캐시를 쓴다."""
    if not 2000 <= year <= 2100:
        raise HTTPException(400, "연도가 범위를 벗어났습니다")
    try:
        return [{"date": d.isoformat(), "name": n} for d, n in holidays_of(year)]
    except httpx.HTTPError as error:
        log("WARNING", "공휴일 불러오기 실패", error=type(error).__name__)
        return []


@app.delete("/api/reminders/{reminder_id}")
def cancel_reminder(reminder_id: int, user: User = Depends(current_user)):
    store.cancel_reminder(user.id, reminder_id)
    return {"ok": True}


@app.delete("/api/data")
def delete_data(user: User = Depends(current_user)):
    store.delete_all(user.id)
    log("INFO", "사용자 데이터 전체 삭제")
    return {"ok": True}
