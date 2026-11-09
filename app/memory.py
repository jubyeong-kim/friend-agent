"""기억 3층: 최근 대화, 대화 요약, 기억 메모.

- 최근 대화: 메시지 원문. 답을 만들 때 최근 RECENT_TURNS 턴을 넣는다.
- 대화 요약: 요약되지 않은 메시지가 SUMMARIZE_AFTER 개를 넘으면 앞쪽 SUMMARIZE_BATCH 개를 요약에 합친다.
- 기억 메모: 매 턴 끝에 기억할 사실을 뽑아 추가, 수정, 삭제한다. 사용자가 화면에서 보고 지울 수 있다.
"""
import re
from datetime import timedelta

from app import tracing, when
from app.logs import log
from app.safety import is_sensitive

RECENT_TURNS = 10            # 답에 넣는 최근 대화(턴 = 사용자 + 캐릭터 메시지 한 쌍)
SUMMARIZE_AFTER = 40         # 요약되지 않은 메시지가 이 개수를 넘으면
SUMMARIZE_BATCH = 20         # 앞쪽 이만큼을 요약에 합친다
MAX_MEMORIES_IN_PROMPT = 30
KINDS = ("fact", "preference", "event")

EXTRACT_PROMPT = """너는 AI 친구 서비스의 기억 정리 담당이다. 방금 오간 대화에서 다음 대화에도 쓸 만한 사용자에 대한 사실을 정리한다.
- kind 는 fact(사실: 이름, 직업, 가족, 반려동물), preference(취향), event(날짜가 있는 일정이나 사건) 중 하나다.
- event 에는 대화에 나온 날짜를 [지금] 기준의 실제 날짜로 바꿔 함께 적는다. [달력]을 보고 요일을 맞춘다. 예: "10월 16일(금) 자격증 시험".
- 사용자가 날을 콕 집어 말하지 않았으면(다음 주, 이번 주말, 다음 달 등) 하루를 골라 지어내지 않는다. [달력]에서 그 말에 해당하는 기간을 찾아 기간 그대로 적는다.
  예: 오늘이 3월 2일(월)일 때 "이번 주말에 캠핑 가" → "3월 7일(토)~8일(일) 캠핑", "다음 주에 이사해" → "3월 9일(월)~15일(일) 중 이사", "다음 달부터 요가 다녀" → "4월부터 요가 다님".
- event 에 시각이 나왔으면 날짜 뒤에 24시간 형식(HH:MM)으로 꼭 함께 적는다. "공부 끝나면"처럼 조건도 있으면 남긴다. 예: "10월 21일(수) 18:00 공부 끝나고 인왕산 가기".
- 이미 있는 기억과 같은 내용이면 추가하지 않는다.
- 사는 곳, 직업, 학교처럼 이미 있는 기억이 바뀌었으면(이사, 이직, 졸업 등) add 하지 말고 [이미 있는 기억]의 그 id 로 update 해서 지금 사실 하나로 바꾼다. 같은 내용을 add 와 update 에 함께 넣지 않는다.
  예: 기억 "id=7 (fact) 사용자는 부산에 산다"가 있을 때 "나 광주로 이사했어" → {"add": [], "update": [{"id": 7, "text": "사용자는 광주에 산다"}], "remove": []}
- 사용자가 틀렸다거나 잊으라고 하면 remove 한다.
- 인사, 감탄, 그날의 기분처럼 다음 대화에 쓸모없는 것은 저장하지 않는다.
- 다만 "요즘 ~에 빠졌어", "~에 꽂혔어"처럼 요즘 즐기는 음식, 취미, 콘텐츠는 그날의 기분이 아니라 취향이므로 preference 로 저장한다. 예: "요즘 마라탕에 꽂혔어" → {"kind": "preference", "text": "요즘 마라탕을 즐겨 먹는다"}
- 사용자가 나중에 알려 달라고 한 일은 약속 기능이 따로 저장하므로 기억에 넣지 않는다.
- 주민등록번호, 카드 번호, 계좌 번호, 비밀번호, 정확한 집 주소는 절대 저장하지 않는다.
- 저장할 것이 없으면 모두 빈 배열로 둔다.
반드시 JSON 하나로만 답한다: {"add": [{"kind": "fact", "text": "..."}], "update": [{"id": 0, "text": "..."}], "remove": [0]}"""

SUMMARY_PROMPT = """아래는 사용자와 AI 친구의 이전 대화다. 다음 대화에 이어 쓸 수 있도록 무슨 이야기를 했는지 요약한다.
기존 요약이 있으면 거기에 이어 붙여 하나의 요약으로 만든다. 10문장 이내로 쓴다."""

# fake 모드(모델 없음)에서 쓰는 간단한 기억 규칙
RULES = [
    (re.compile(r"내 이름은\s*([가-힣A-Za-z]{2,10}?)(?:이야|야|예요|이에요|입니다|이고|고)?(?=[\s.,!~]|$)"), "fact", "사용자 이름은 {0}"),
    (re.compile(r"([가-힣A-Za-z]{1,10}?)\s*(?:을|를)\s*(?:정말\s*|진짜\s*|엄청\s*)?좋아해"), "preference", "{0}{1} 좋아함"),
    (re.compile(r"(고양이|강아지|반려견|반려묘|동생|언니|오빠|형|누나)\s*이름은\s*([가-힣A-Za-z]{1,10}?)(?:이야|야|예요|이에요|입니다)?(?=[\s.,!~]|$)"), "fact", "{0} 이름은 {1}"),
    (re.compile(r"((?:오늘|내일|모레|[월화수목금토일]요일)[에는]*\s*(?:\d{1,2}\s*시\s*(?:반|\d{1,2}\s*분)?에?\s*)?[가-힣 ]{1,15}(?:시험|면접|발표|여행|약속))"), "event", "{0}"),
]
DAY_WORD = re.compile(r"(다음\s*주\s*)?(오늘|내일\s*모레|내일|모레|글피|[월화수목금토일]요일)[에는]*\s*")
CLOCK = re.compile(r"\d{1,2}\s*시\s*(?:반|\d{1,2}\s*분)?에?\s*")


def same_text(text):
    """끝의 마침표 · 공백만 다른 글은 같은 기억으로 본다."""
    return str(text).strip().rstrip(".").strip()


# 날을 콕 집지 않은 기간 말: (말, 이번 주 월요일부터 며칠 뒤에 시작, 며칠 동안).
# 지시문에 기간으로 적으라고 해도 모델이 「다음 주에 수술」을 「11월 18일(수) 수술」처럼 하루를 골라 적었다(4/4).
VAGUE_PERIODS = [
    (re.compile(r"다음\s*주\s*말"), 12, 2),
    (re.compile(r"이번\s*주\s*말"), 5, 2),
    (re.compile(r"다음\s*주(?!\s*(?:말|부터|까지))"), 7, 7),
]
EXACT_DAY = re.compile(r"\d{1,2}\s*일|[월화수목금토일]요일|오늘|내일|모레|글피")
DATE_IN_TEXT = re.compile(r"\d{1,2}월\s*\d{1,2}일(?:\([월화수목금토일]\))?")


def period_text(text, message, now):
    """사용자가 '다음 주' · '이번 주말'처럼 기간만 말했는데 일정 메모에 날짜가 하나만 적혔으면 그 날짜를 기간으로 바꾼다.

    '다음 주 수요일', '내일'처럼 날을 집은 말이 있으면 건드리지 않는다.
    """
    if EXACT_DAY.search(message) or "~" in text:
        return text
    found = next(((start, days) for pattern, start, days in VAGUE_PERIODS if pattern.search(message)), None)
    dates = DATE_IN_TEXT.findall(text)
    if not found or len(dates) != 1:
        return text
    first = now.date() - timedelta(days=now.weekday()) + timedelta(days=found[0])
    last = first + timedelta(days=found[1] - 1)
    end = f"{last.day}일({when.WEEKDAYS[last.weekday()]})" if last.month == first.month else when.date_label(last)
    return text.replace(dates[0], f"{when.date_label(first)}~{end}", 1)


def object_particle(word):
    """받침이 있으면 '을', 없으면 '를'."""
    last = word[-1]
    if "가" <= last <= "힣":
        return "을" if (ord(last) - 0xAC00) % 28 else "를"
    return "을(를)"


def is_answered(messages, i):
    """i 번째 사용자 메시지 바로 뒤에 캐릭터의 답이 저장돼 있는지. 먼저 말 걸기는 답으로 치지 않는다."""
    nxt = messages[i + 1] if i + 1 < len(messages) else None
    return bool(nxt) and nxt["role"] == "assistant" and nxt["meta"].get("action") != "greeting"


def answered_only(messages):
    """답이 저장되지 않은 사용자 메시지를 뺀다.

    요청이 시간 제한이나 네트워크 문제로 끊기면 사용자 말만 저장되고 답은 저장되지 않는다.
    그 말이 문맥에 남으면 다음 답이 앞의 말에 대답하느라 엉뚱해진다.
    """
    return [m for i, m in enumerate(messages) if m["role"] != "user" or is_answered(messages, i)]


def build_context(store, uid):
    """답을 만들 때 넣을 기억 메모, 대화 요약, 최근 대화를 꺼낸다. 최근 대화는 답까지 저장된 것만 넣는다."""
    memories = store.list_memories(uid)[-MAX_MEMORIES_IN_PROMPT:]
    summary = store.get_summary(uid)
    after = summary["covers_until"] if summary else 0
    recent = answered_only([m for m in store.recent_messages(uid, RECENT_TURNS * 2) if m["id"] > after])
    return memories, summary, recent


def memory_block(memories, summary):
    parts = []
    if memories:
        parts.append("[기억 메모: 사용자에 대해 알고 있는 것]\n" + "\n".join(f"- {m['text']}" for m in memories))
    if summary:
        parts.append(f"[이전 대화 요약]\n{summary['text']}")
    return "\n\n".join(parts)


def event_text(found, now):
    """'내일 6시에 면접' → '10월 22일(목) 18:00 면접'. 모델이 하는 일(날짜를 실제 날짜로)을 규칙으로 흉내 낸다."""
    day = (now + timedelta(days=when.day_offset(found, now) or 0)).date()
    t = when.parse_when(found, now) if CLOCK.search(found) else None  # '3시 70분'처럼 없는 시각이면 None
    clock = t.strftime("%H:%M ") if t else ""
    rest = CLOCK.sub("", DAY_WORD.sub("", found, count=1), count=1).strip()
    return f"{when.date_label(day)} {clock}{rest}"


def rule_extract(message, now):
    found = []
    for pattern, kind, template in RULES:
        if kind == "event" and "?" in message:  # "내일 약속 있어?"는 일정을 알려 준 게 아니라 묻는 말
            continue
        for m in pattern.finditer(message):
            groups = [g.strip() for g in m.groups()]
            if "{1}" in template and len(groups) == 1:
                groups.append(object_particle(groups[0]))
            text = event_text(groups[0], now) if kind == "event" else template.format(*groups)
            found.append({"kind": kind, "text": text})
    return {"add": found, "update": [], "remove": []}


def update_memories(store, uid, user_message, reply, now, llm, trace=None):
    """방금 턴에서 기억할 것을 뽑아 저장한다. 바뀐 내용을 돌려준다(화면 표시용)."""
    existing = store.list_memories(uid)
    gen = (trace or tracing.Observation()).child("memory-update", "generation", model=getattr(llm, "model", None),
                                                 input={"user": user_message, "assistant": reply})
    if getattr(llm, "can_reason", False):
        listed = "\n".join(f"- id={m['id']} ({m['kind']}) {m['text']}" for m in existing) or "(없음)"
        user = f"[지금] {now:%Y-%m-%d %H:%M}\n[달력]\n{when.calendar_hint(now)}\n[이미 있는 기억]\n{listed}\n\n[대화]\nuser: {user_message}\nassistant: {reply}"
        try:
            plan = gen.run(llm.complete_json, EXTRACT_PROMPT, user)
        except Exception as error:
            log("WARNING", "기억 정리 실패", error=type(error).__name__)
            gen.end(level="ERROR", status_message=type(error).__name__)
            return {"added": [], "updated": [], "removed": []}
    else:
        plan = rule_extract(user_message, now)
        gen.update(output=plan)

    def clean(text, kind):
        text = str(text or "").strip()
        return (period_text(text, user_message, now) if kind == "event" else text)[:200]

    known_ids = {m["id"]: m for m in existing}
    known_texts = {same_text(m["text"]) for m in existing}
    updates = []
    for item in plan.get("update") or []:
        old = known_ids.get(item.get("id"))
        text = clean(item.get("text"), old and old["kind"])
        if old and text and not is_sensitive(text):
            updates.append((item["id"], text))
    # 모델이 바뀐 사실을 update 하면서 같은 글을 add 에도 넣어 기억이 두 개 생기곤 했다(「대학원생이다」 4/4).
    # update 로 바뀔 글은 add 하지 않는다.
    known_texts |= {same_text(text) for _, text in updates}
    changes = {"added": [], "updated": [], "removed": []}
    for item in plan.get("add") or []:
        kind = item.get("kind") if item.get("kind") in KINDS else "fact"
        text = clean(item.get("text"), kind)
        if not text or same_text(text) in known_texts or is_sensitive(text):
            continue
        changes["added"].append(store.add_memory(uid, kind, text))
    for memory_id, text in updates:
        store.update_memory(uid, memory_id, text)
        changes["updated"].append({"id": memory_id, "text": text})
    for memory_id in plan.get("remove") or []:
        if memory_id in known_ids:
            store.delete_memory(uid, memory_id)
            changes["removed"].append({"id": memory_id, "text": known_ids[memory_id]["text"]})
    counts = {key: len(items) for key, items in changes.items()}
    if any(counts.values()):
        log("INFO", "기억 정리", **counts)
    gen.end(metadata=counts)
    return changes


def maybe_summarize(store, uid, llm, trace=None):
    """요약되지 않은 메시지가 많으면 앞쪽을 요약에 합친다. 요약했으면 True."""
    summary = store.get_summary(uid)
    pending = store.messages_after(uid, summary["covers_until"] if summary else 0)
    if len(pending) <= SUMMARIZE_AFTER:
        return False
    batch = pending[:SUMMARIZE_BATCH]
    talk = "\n".join(f"{m['role']}: {m['content']}" for m in batch)
    previous = f"[기존 요약]\n{summary['text']}\n\n" if summary else ""
    gen = (trace or tracing.Observation()).child("memory-summary", "generation", model=getattr(llm, "model", None),
                                                 metadata={"covered": len(batch)})
    if getattr(llm, "can_reason", False):
        text = gen.run(llm.complete_text, [{"role": "system", "content": SUMMARY_PROMPT},
                                           {"role": "user", "content": f"{previous}[대화]\n{talk}"}])
    else:
        text = ((summary["text"] + " ") if summary else "") + f"메시지 {len(batch)}개를 주고받았다."
        gen.update(input=talk, output=text)
    gen.end()
    store.save_summary(uid, text[:2000], batch[-1]["id"])
    log("INFO", "대화 요약 갱신", covered=len(batch))
    return True
