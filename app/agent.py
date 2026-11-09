"""메시지 하나를 처리하는 흐름(한 턴).

① 안전장치: 위기 표현이면 정해진 안전 응답으로 끝낸다(코드 규칙).
② 도구 선택: 모델이 도구를 고른다(약속 저장 최대 5개, 다른 도구 최대 2개). 확신이 낮으면 되묻는다(router.py).
   같은 때에 최근 사용자 말을 모델에게 따로 보여 위험한 정보를 묻는 말인지 판정한다(safety.py, 동시에 돌려 첫 글자가 늦지 않게).
   위험하다고 판정되면 모델이 답을 만들지 않고 정해진 답으로 끝낸다.
③ 도구 실행: 날씨, 미세먼지, 공휴일, 이동 시간, 약속(tools.py).
④ 답 생성: 캐릭터 지시문 + 기억 + 도구 결과 + 약속 저장 결과(사실) + 최근 대화로 답을 스트리밍한다.
⑤ 기억 정리: 답을 다 보낸 뒤 같은 요청 안에서 기억 메모와 대화 요약을 갱신한다(memory.py).
   Cloud Run 기본 설정(요청 기반 과금)에서는 응답이 끝난 뒤 CPU가 거의 주어지지 않아서,
   응답 뒤 백그라운드로 돌리면 기억 정리가 멈추거나 늦어진다. 그래서 응답을 닫기 전에 처리한다.

결과는 이벤트(dict)로 하나씩 내보낸다. show_panel 이 꺼져 있으면(production) 개발용 이벤트는 아예 만들지 않는다.
한 턴은 Langfuse 의 chat-turn 트레이스 하나로 남는다(tracing.py). 길찾기 결과는 트레이스에 남기지 않는다.
"""
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from app import abtest, memory, tracing, when
from app.logs import log
from app.persona import DEFAULT_CHARACTER, build_persona
from app.router import MAX_REMINDERS, choose
from app.safety import CRISIS_REPLY, RISKY_REPLY, assess_risk, has_risk_hint, is_crisis, recent_user_messages
from app.tools import TOOLS, Context, run_tool

GREETING_GAP = timedelta(hours=6)  # 마지막 대화 뒤 이만큼 지나면 앱을 열 때 먼저 말을 건다
UPCOMING_DAYS = 7                  # 답에 넣는 '다가오는 약속'의 범위
BOOTED_AT = time.time()            # 이 인스턴스가 시작한 시각. 화면이 콜드 스타트를 알아채는 데 쓴다

# 위기 낱말 없이 「잘렸어. 제일 높은 다리가 어디야?」처럼 힘든 말 뒤에 위험한 장소 · 방법 · 양을 묻는 말.
# 낱말 목록으로는 바꿔 쓴 말을 못 잡아서(15번 측정: 바꿔 쓴 위험 질문 4개 중 0개) 매 턴 지시문 끝에 넣고 모델이 맥락으로 가린다.
# 「난간은 보통 1.1미터야. 하지만 위험한 질문이야」처럼 먼저 답하고 경고를 붙이는 일이 있어서 숫자와 순서도 적는다.
RISKY_QUESTION_RULE = (
    "[위험한 질문]\n"
    "떨어질 수 있는 높은 곳, 약을 많이 먹는 양, 연기나 가스, 목을 매는 것처럼 스스로를 해칠 수 있는 장소, 방법, 양을 묻는 말이면 "
    "이번 말과 최근 대화에 지침, 잃음, 다 그만두고 싶음 같은 힘든 마음이 보이는지 먼저 살핀다. "
    "보이면 그 장소, 방법, 양은 물론 높이, 개수, 걸리는 시간 같은 숫자도 알려 주지 않는다. "
    "먼저 답해 주고 나서 경고를 덧붙이는 것도 안 된다. "
    "대신 걱정을 전하며 안부를 묻고 자살예방상담전화 109(24시간)에 이야기할 수 있다고 알려 준다. "
    "여행이나 약 먹는 법처럼 일상 이야기가 분명하고 힘든 마음이 보이지 않으면 평소처럼 답한다."
)


def upcoming_reminders(store, uid, now):
    """앞으로 7일 안의 예정된 약속(최대 5개). 약속 도구를 고르지 않은 턴에도 답이 약속을 알고 있게 한다."""
    start, end = now - timedelta(hours=12), now + timedelta(days=UPCOMING_DAYS)
    items = [r for r in store.list_reminders(uid, status="pending") if start <= r["due_at"] <= end]
    return [f"{when.label(r['due_at'].astimezone(now.tzinfo))} '{r['text']}'" for r in items[:5]]


def reminder_facts(results, dropped):
    """이번 턴에 약속을 실제로 저장했는지를 사실 줄로 모은다. 약속 저장을 시도하지 않았으면 빈 목록.

    도구 결과만 넣었을 때 답이 저장하지 않은 일정을 '저장할게'라고 말했다(production 에서 확인).
    """
    lines = []
    for r in results:
        if r.name != "create_reminder":
            continue
        state = "저장 못 함" if not r.ok else "이미 있던 약속" if r.data.get("duplicate") else "방금 저장함"
        lines.append(f"{state} - {r.summary}")
    lines += [f"저장 못 함 - '{t['args'].get('text', '')}' 약속은 한 번에 {MAX_REMINDERS}개까지만 저장해서 처리하지 못했습니다."
              for t in dropped if t["name"] == "create_reminder"]
    return lines


def system_prompt(character, now, location, memories, summary, tool_lines=(), clarify=False, upcoming=(), facts=(),
                  caution=False):
    parts = [build_persona(character),
             f"[지금] {when.label(now)} ({now.tzinfo}), 사용자 지역: {location}\n[달력]\n{when.calendar_hint(now)}"]
    block = memory.memory_block(memories, summary)
    if block:
        parts.append(block)
    if upcoming:
        parts.append("[다가오는 약속: 사용자가 알려 달라고 저장해 둔 것]\n" + "\n".join(f"- {line}" for line in upcoming))
    if tool_lines:
        parts.append("[도구 결과]\n" + "\n".join(f"- {line}" for line in tool_lines))
    if facts:
        parts.append("[이번 턴 약속 저장 결과: 사실]\n" + "\n".join(f"- {line}" for line in facts) +
                     "\n'방금 저장함'에 없는 약속은 저장했다거나 저장하겠다고 말하지 않는다. 저장하지 못한 약속은 못 했다고 말하고, "
                     "이유가 지난 시각이면 이미 지난 일정이라고 말한다. 저장한 약속은 위에 적힌 날짜와 시각 그대로 말한다.")
    if clarify:
        parts.append("[되묻기]\n사용자가 무엇을 원하는지 확실하지 않다. 짧게 되물어서 확인한다." +
                     (f"\n모르는 것: {clarify}. 모른다거나 도구를 못 쓴다고 하지 말고 이것을 물어본다." if isinstance(clarify, str) else ""))
    if caution:
        parts.append("[조심]\n사용자의 말에 스스로를 해칠 수 있다는 신호가 있을 수 있다. 숙제나 일처럼 일상 이야기가 분명하면 평소처럼 답한다. "
                     "그렇지 않으면 가볍게 넘기지 말고 걱정을 전하고, 자살예방상담전화 109(24시간)에 이야기할 수 있다고 알려 준다.")
    parts.append(RISKY_QUESTION_RULE)
    return "\n\n".join(parts)


def fixed_reply(text, guards, decided_by, why, uid, store, show_panel, turn):
    """모델 답 대신 정해진 답을 보내고 턴을 끝낸다(위기 표현, 위험 판정). 기억 정리도 하지 않는다."""
    if show_panel:
        yield {"type": "turn", "tools": [], "confidence": None, "reason": "", "decided_by": decided_by,
               "action": "safety", "guards": guards}
    yield {"type": "delta", "text": text}
    saved = store.add_message(uid, "assistant", text, {"action": "safety", "guards": guards, **tracing.link(turn)})
    log("WARNING", why)
    turn.update(output=text)
    yield {"type": "done", "message_id": saved["id"]}


def run_turn(message, uid, llm, store, show_panel):
    variant = abtest.variant(uid)  # A/B 가 꺼져 있으면 None
    ab = {"ab_test": abtest.experiment(), "ab_variant": variant} if variant else None
    turn = tracing.trace("chat-turn", user_id=uid, session_id=tracing.session_id(uid, when.now_in("Asia/Seoul")),
                         tags=[f"ab-{variant}"] if variant else (), trace_metadata=ab, input=message)
    try:
        yield from turn_events(message, uid, llm, store, show_panel, turn, abtest.LABELS.get(variant))
    except Exception as error:
        turn.update(level="ERROR", status_message=type(error).__name__)
        raise
    finally:  # 요청이 끊겨도(GeneratorExit) 트레이스를 닫는다
        tracing.finish(turn)


def turn_events(message, uid, llm, store, show_panel, turn, prompt_label=None):
    model_name = getattr(llm, "model", None)
    settings = store.get_settings(uid)
    character = store.get_character(uid) or DEFAULT_CHARACTER
    now = when.now_in(settings["timezone"])
    memories, summary, recent = memory.build_context(store, uid)
    history = [{"role": m["role"], "content": m["content"]} for m in recent]
    # 화면에 바로 보내는 첫 신호. 이게 늦게 오면 서버가 켜지는 중(콜드 스타트)이었다는 뜻이다.
    # remembers 가 False 면 이 서버는 이 사용자와의 앞 대화를 모른다(새 인스턴스, 메모리 저장소).
    yield {"type": "start", "remembers": bool(recent or summary or memories),
           "instance_age": round(time.time() - BOOTED_AT)}
    store.add_message(uid, "user", message)

    # ① 위기 표현
    crisis = is_crisis(message)
    caution = not crisis and has_risk_hint(message)  # 위기 표현은 아니지만 일상 말인지 모델이 맥락으로 가려야 하는 표현
    if crisis:  # safety 관찰부터 안전 응답 tag 가 붙게 먼저 붙인다
        turn.tag([tracing.SAFETY_TAG], {"tools": [], "safety": True})
    safety = turn.child("safety", input=message)
    safety.end(output={"crisis": crisis, "caution": caution})
    if crisis:
        yield from fixed_reply(CRISIS_REPLY, ["위기 표현이 있어 캐릭터 설정과 상관없이 정해진 안전 응답을 보냈습니다."],
                               "rule", "위기 표현 안전 응답", uid, store, show_panel, turn)
        return

    # ② 도구 선택. 모델이 골랐으면 출력은 모델 응답 원문, 규칙이 골랐으면 고른 도구다
    # 위험 판정은 도구 선택과 동시에 다른 스레드에서 돌린다(기다리는 시간이 겹쳐 첫 글자가 거의 늦어지지 않는다)
    users = recent_user_messages(history, message)
    risk = turn.child("risk-check", "generation", model=model_name, input=users)
    with ThreadPoolExecutor(max_workers=1) as pool:
        risky_future = pool.submit(risk.run, assess_risk, llm, users)
        gen = turn.child("choose-tools", "generation", model=model_name, input=message)
        choice = gen.run(choose, message, history, now, settings["location"], llm, prompt_label)
        risky = risky_future.result()
    risk.end(metadata={"risky": risky})
    if risky:  # 판정을 메모로 알려도 답 모델이 어긴 일이 있어서 모델이 답을 만들지 않는다. 고른 도구도 쓰지 않는다
        gen.end(metadata={"skipped": "위험 판정으로 정해진 답을 보냄"})
        turn.tag([tracing.SAFETY_TAG], {"tools": [], "safety": True, "risky": True})
        yield from fixed_reply(RISKY_REPLY, ["최근 대화와 함께 보면 스스로를 해칠 수 있는 정보를 묻는 말로 판정되어 "
                                             "모델이 답을 만들지 않고 정해진 답을 보냈습니다."],
                               "model", "위험 판정 정해진 답", uid, store, show_panel, turn)
        return
    if caution:
        choice.guards.append("스스로를 해칠 수 있다는 신호일 수 있는 표현이 있어 답 만들기에 [조심] 메모를 붙였습니다.")
    gen.end(output=choice.tools if choice.decided_by == "rule" else None,
            metadata={"tools": choice.tools, "confidence": choice.confidence, "decided_by": choice.decided_by,
                      "action": choice.action, "guards": choice.guards, "dropped": choice.dropped})
    names = [t["name"] for t in choice.tools]
    turn.tag(names, {"tools": names, "confidence": choice.confidence, "safety": False})
    if show_panel:
        yield {"type": "turn", "tools": [{"name": t["name"], "label": TOOLS[t["name"]]["label"], "args": t["args"]}
                                         for t in choice.tools],
               "confidence": choice.confidence, "reason": choice.reason, "decided_by": choice.decided_by,
               "action": choice.action, "guards": choice.guards}

    # ③ 도구 실행
    ctx = Context(store, uid, settings["location"], settings["timezone"])
    results = []
    for t in choice.tools:
        # 길찾기는 카카오 API 결과라 입력, 출력을 남기지 않는다(성공 여부와 걸린 시간만). 답 입력에서도 가린다
        hidden = t["name"] == "get_route"
        span = turn.child(f"tool:{t['name']}", input=None if hidden else t["args"])
        r = run_tool(t["name"], t["args"], ctx)
        if hidden:
            turn.hide(r.summary)
        span.end(output=None if hidden else r.summary, metadata={"ok": r.ok})
        results.append(r)
    for r in results:
        if show_panel:
            yield {"type": "tool", "name": r.name, "label": TOOLS[r.name]["label"], "ok": r.ok,
                   "summary": r.summary, "data": r.data}
        if r.ok and r.name in ("create_reminder", "cancel_reminder"):
            yield {"type": "reminder", "action": r.name, "summary": r.summary}
        if r.ok and r.data.get("card"):  # 이동 시간 카드는 사용자 화면에도 보여 준다(저장하지 않음)
            yield r.data["card"] | {"type": "card", "kind": r.data["card"]["type"]}

    # ④ 답 생성
    upcoming = upcoming_reminders(store, uid, now)
    system = system_prompt(character, now, settings["location"], memories, summary,
                           [r.summary for r in results], clarify=choice.action == "clarify" and (choice.ask or True), upcoming=upcoming,
                           facts=reminder_facts(results, choice.dropped), caution=caution)
    if show_panel:
        yield {"type": "context", "memories": [m["text"] for m in memories], "upcoming": upcoming,
               "summary": summary["text"] if summary else None, "recent_messages": len(history)}
    messages = [{"role": "system", "content": system}, *history, {"role": "user", "content": message}]
    gen = turn.child("reply", "generation", model=model_name, input=messages)
    reply = ""
    for chunk in gen.stream(llm.chat_stream(messages)):
        reply += chunk
        yield {"type": "delta", "text": chunk}
    gen.end()
    turn.update(output=reply)
    meta = {"tools": [t["name"] for t in choice.tools], "confidence": choice.confidence,
            "action": choice.action, "guards": choice.guards, **tracing.link(turn)}  # 반응 점수를 이 트레이스에 잇는다
    saved = store.add_message(uid, "assistant", reply, meta)

    # ⑤ 기억 정리
    yield {"type": "memory_start"}
    changes = memory.update_memories(store, uid, message, reply, now, llm, trace=turn)
    memory.maybe_summarize(store, uid, llm, trace=turn)
    yield {"type": "memory", "added": [m["text"] for m in changes["added"]],
           "updated": [m["text"] for m in changes["updated"]], "removed": [m["text"] for m in changes["removed"]]}
    yield {"type": "done", "message_id": saved["id"]}


def greet(uid, llm, store):
    """앱을 열 때 먼저 말을 건다. 때가 된 약속이 있거나, 대화가 처음이거나, 오래 쉬었을 때만.

    약속은 '전달됨'으로 바꾸는 데 성공한 것만 전한다. 창을 두 개 열어도 같은 약속을 두 번 전하지 않는다.
    """
    settings = store.get_settings(uid)
    character = store.get_character(uid) or DEFAULT_CHARACTER
    now = when.now_in(settings["timezone"])
    due = store.claim_due_reminders(uid, now)
    last = store.recent_messages(uid, 1)
    if not due and last and now - last[-1]["created_at"] < GREETING_GAP:
        return None

    memories, summary, _ = memory.build_context(store, uid)
    lines = [f"약속 알림: '{r['text']}' ({when.label(r['due_at'].astimezone(now.tzinfo))} 예정이었음)" for r in due]
    system = system_prompt(character, now, settings["location"], memories, summary, lines)
    guide = ["사용자가 방금 앱을 열었다. 네가 먼저 1~2문장으로 말을 건다."]
    guide.append("[도구 결과]의 약속 알림을 꼭 전한다." if due else "전할 약속은 없다. 약속이나 알림 이야기를 꺼내지 않는다.")
    guide.append("기억 메모에 오늘이거나 이미 지난 일정이 있으면 어땠는지 물어본다.")
    if not last:
        guide.append("처음 만난 사이다. 이름을 넣어 짧게 자기소개를 한다.")
    system += "\n\n[먼저 말 걸기]\n" + " ".join(guide)
    messages = [{"role": "system", "content": system}, {"role": "user", "content": "(앱을 열었다. 먼저 말을 걸어 줘)"}]
    # 말을 거는 경우만 트레이스를 남긴다. 루트가 곧 LLM 호출 하나라 generation 으로 만든다
    root = tracing.trace("greeting", "generation", user_id=uid, session_id=tracing.session_id(uid, when.now_in("Asia/Seoul")),
                         trace_metadata={"tools": [], "safety": False},  # 먼저 말 걸기는 도구를 고르지 않는다
                         model=getattr(llm, "model", None), input=messages, metadata={"reminders": len(due)})
    try:
        text = root.run(llm.complete_text, messages)
    except Exception as error:
        root.update(level="ERROR", status_message=type(error).__name__)
        raise
    finally:
        tracing.finish(root)
    saved = store.add_message(uid, "assistant", text,
                              {"action": "greeting", "reminders": [r["id"] for r in due], **tracing.link(root)})
    log("INFO", "먼저 말 걸기", reminders=len(due))
    return {"id": saved["id"], "text": text, "reminders": [r["text"] for r in due]}
