"""메시지마다 쓸 도구를 고르는 도구 선택기.

1. 모델이 메시지와 최근 대화를 읽고 도구를 고른다(약속 저장은 최대 5개, 다른 도구는 최대 2개). 확신도(0~1)와 이유를 함께 낸다.
2. 안전장치: 확신도가 기준보다 낮으면 도구를 쓰지 않고 되묻는다(코드 규칙).
   메시지에 적힌 지난 날짜를 모델이 오늘이나 앞날로 옮기면 적힌 날짜로 되돌리고, 한도를 넘어 뺀 도구는 따로 돌려준다.
fake 모드처럼 모델을 쓰지 않을 때나 모델 응답을 읽지 못했을 때는 키워드 규칙이 같은 형식으로 답한다.
"""
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from app import when
from app.logs import log
from app.tools import CITIES, TOOLS

# 약속 저장은 다른 도구와 따로 센다. 일정표 사진 한 장에 일정이 여러 개 있다(2개 한도일 때 세 번째 일정이 약속으로 저장되지 않았다)
MAX_REMINDERS = 5
CONFIDENCE_THRESHOLD = 0.6
MAX_TOOLS = 2

KEYWORDS = {
    "get_weather": ["날씨", "비 와", "비와", "비 올", "비올", "우산", "더워", "추워", "덥", "춥", "기온", "눈 와", "눈 올", "뭐 입", "쌀쌀", "따뜻"],
    "get_air_quality": ["미세먼지", "초미세", "공기", "마스크", "환기", "황사"],
    "get_holidays": ["공휴일", "휴일", "연휴", "빨간 날", "빨간날", "쉬는 날"],
}
# "토요일 날씨 알려 줘"는 약속이 아니다. 약속은 "~라고 알려 줘", "잊지 않게", "리마인드"처럼 부탁하는 말이 있어야 한다.
ASK_PATTERN = re.compile(r"(라고|으라고|하라고)\s*(꼭\s*)?(알려|말해)|잊지 않게|리마인드|깨워")
CANCEL_WORDS = ["취소", "지워", "없던 걸로", "안 알려 줘도", "안 알려줘도"]
LIST_PATTERN = re.compile(r"(약속|알림|리마인드|스케줄).*(뭐|목록|있어|있나|있지|있었|보여|확인|알아|아는)")
# 이동 시간: "얼마나 걸려", "몇 분", "갈 수 있어" 같은 말과 도착지가 함께 있어야 한다("감기 걸려"는 아니다)
ROUTE_ASK = re.compile(r"얼마나\s*걸|몇\s*분|몇\s*시간|걸려\?|걸리|갈\s*수\s*있|도착할\s*수|어떻게\s*가")
PLACE = r"(?<!\w)([가-힣A-Za-z][가-힣A-Za-z0-9]*)"   # 앞이 글자·숫자가 아닌 곳에서 시작("7시까지"의 '시'는 장소가 아니다)
NOT_PLACE = {"지하철", "버스", "택시", "걸어서", "도보", "차", "자전거", "몇", "언제", "어디"}
ROUTE_FROM = re.compile(PLACE + r"(?:에서|부터)(?=\s)")
ROUTE_TO = [re.compile(PLACE + r"\s*(?:→|->)\s*" + PLACE),                       # 낙성대→인왕산
            re.compile(PLACE + r"(?:까지|으로|로)(?=\s|$|\?)"),                      # 경복궁역까지
            re.compile(PLACE + r"\s+(?:가면|가려면|가는\s*데|갈\s*수|가는\s*거)")]     # 인왕산 가면
DEPART = re.compile(r"\d{1,2}\s*시(?:\s*\d{1,2}\s*분|\s*반)?\s*(?:에\s*)?(?:퇴근|출발|나가|끝나)")
ARRIVE = re.compile(r"\d{1,2}\s*시(?:\s*\d{1,2}\s*분|\s*반)?\s*까지")


def router_prompt():
    tools = "\n".join(f"- {name}: {t['desc']}. 인자 {t['args']}" for name, t in TOOLS.items())
    return "\n".join([
        "너는 AI 친구 서비스에서 사용자 메시지에 답하기 전에 필요한 도구를 고르는 선택기다.",
        "쓸 수 있는 도구:",
        tools,
        "",
        "규칙:",
        "- 도구가 필요 없는 일상 대화면 tools 를 빈 배열로 둔다. 대부분의 메시지는 여기에 해당한다.",
        f"- 꼭 필요한 도구만 고른다. create_reminder 는 약속마다 하나씩 최대 {MAX_REMINDERS}개, 다른 도구는 합쳐서 최대 {MAX_TOOLS}개다. "
        "묻지 않은 정보의 도구는 덧붙이지 않는다(날씨만 물으면 get_weather 하나).",
        "- 날씨나 미세먼지 이야기를 해도 예보를 묻는 게 아니라 경험이나 하소연이면 도구를 쓰지 않는다.",
        "- location 은 메시지에 도시 이름이 있으면 그 도시, 없으면 사용자 지역이다.",
        "- date 는 [달력]을 보고 정한 YYYY-MM-DD 날짜다. 날짜 표현이 없으면 오늘이다.",
        "- create_reminder 는 사용자가 나중에 알려 달라고 분명히 부탁할 때만 고른다. 일정이 있다는 이야기만 하면 고르지 않는다.",
        "- create_reminder 의 text 는 알려 줄 내용만 짧게 쓴다. 예: '우산 챙기기', '일찍 자기'.",
        "- create_reminder 의 due_at 은 [지금] 시각과 시간대를 기준으로 계산한다. 시각 없이 날짜만 있으면 09:00, 아침 08:00, 점심 12:00, 저녁 19:00, 밤 21:00 으로 한다.",
        "- 사용자가 자기 약속, 알림, 일정을 묻거나 확인하려 하면 list_reminders 를 고른다. 예: '내일 약속 있어?', '내 약속에 대해 아는 거 있어?'.",
        "- 어디서 어디까지 얼마나 걸리는지, 몇 시까지 갈 수 있는지 물으면 get_route 를 고른다. 걸리는 시간을 짐작해서 답하지 않게 하기 위해서다.",
        "- get_route 의 origin, destination 은 말한 역이나 장소 이름을 그대로 쓴다. 예: '낙성대역', '인왕산'. 출발지를 말하지 않았으면 최근 대화에서 찾고, 없으면 빈 문자열로 둔다.",
        "- get_route 는 한 번만 고른다. 출발 시각과 도착 시각을 둘 다 말했으면 한 호출의 depart_at, arrive_by 에 함께 넣는다.",
        "- get_route 의 depart_at 은 '6시에 퇴근하고'처럼 출발 시각을 말했을 때만 그 시각으로 쓴다('6시'는 보통 18:00). 말하지 않았으면 비워 둔다(지금 출발로 계산된다). arrive_by 는 '7시까지'처럼 도착해야 할 시각을 말했을 때만 쓴다. 둘 다 [지금] 기준 ISO 시각이다.",
        "- 다녀온 이야기('어제 인왕산 가는 데 2시간 걸렸어')처럼 지난 경험이면 get_route 를 고르지 않는다.",
        "- 최근 대화를 보고 '그럼 모레는?' 같은 이어지는 질문의 도구와 인자를 정한다.",
        "- confidence 는 고른 결과가 맞을 확신을 0과 1 사이 숫자로 쓴다. 도구가 필요 없다고 판단했을 때도 그 판단의 확신을 쓴다(일상 대화면 높게).",
        "- reason 은 고른 이유를 한국어 한 문장으로 쓴다.",
        '- 반드시 JSON 하나로만 답한다: {"tools": [{"name": "도구 이름", "args": {인자}}], "confidence": 0과 1 사이 숫자, "reason": "이유"}',
    ])


@dataclass
class ToolChoice:
    tools: list                          # [{"name": ..., "args": {...}}]
    confidence: float
    reason: str
    decided_by: str                      # model, rule
    action: str = "answer"               # answer, clarify
    guards: list = field(default_factory=list)
    dropped: list = field(default_factory=list)  # 한도를 넘어 쓰지 않은 도구. 답이 처리하지 못했다고 말하게 한다


# ── 키워드 규칙 ─────────────────────────────────────────────────────

def find_city(text):
    return next((c for c in CITIES if c in text), None)


def reminder_text(text):
    """'목요일 아침에 우산 챙기라고 알려 줘' → '우산 챙기기'처럼 알려 줄 내용만 남긴다."""
    t = re.sub(r"(다음\s*주\s*)?[월화수목금토일]요일|오늘|내일|모레|글피|새벽|아침|오전|점심|오후|저녁|밤", " ", text)
    t = re.sub(r"\d+\s*(분|시간)\s*(뒤|후|있다가)|\d{1,2}\s*시(\s*\d{1,2}\s*분|\s*반)?", " ", t)
    t = re.sub(r"\s*(꼭\s*)?(알려\s*줘|알려\s*주라|알려\s*줄래|리마인드\s*해\s*줘|깨워\s*줘|말해\s*줘)[.!?~ ]*$", "", t)
    t = re.sub(r"^[\s,]*(에는|에)\s+", "", t.strip())
    t = re.sub(r"(으라고|라고)$", "기", t.strip())   # 챙기라고 → 챙기기, 먹으라고 → 먹기
    t = re.sub(r"(다고|하고)$", "", t)
    return re.sub(r"\s+", " ", t).strip(" ,.") or text.strip()


def holiday_range(message, now):
    """'12월에 연휴 있어?' → 그 달의 첫날과 마지막 날. 달이 없으면 빈 인자(앞으로 1년)."""
    m = re.search(r"(\d{1,2})\s*월", message)
    if not m or not 1 <= int(m.group(1)) <= 12:
        return {}
    month = int(m.group(1))
    year = now.year if month >= now.month else now.year + 1
    last = (date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)).day
    return {"from": f"{year}-{month:02d}-01", "to": f"{year}-{month:02d}-{last}"}


def route_args(message, now):
    """'6시 퇴근하고 낙성대에서 인왕산 가면 몇 시간 걸려?' → 출발지, 도착지, 출발/도착 시각. 이동 시간 질문이 아니면 None."""
    if not ROUTE_ASK.search(message):
        return None
    args = {"origin": "", "destination": ""}
    found = (m for pattern in ROUTE_TO for m in pattern.finditer(message) if m.group(m.lastindex) not in NOT_PLACE)
    if m := next(found, None):
        args["destination"] = m.group(m.lastindex)
        if m.lastindex == 2:
            args["origin"] = m.group(1)
    if not args["destination"]:
        return None
    if not args["origin"] and (m := ROUTE_FROM.search(message)):
        args["origin"] = m.group(1)
    return args | route_clocks(message, now)


def route_clocks(message, now):
    """'6시에 퇴근하고 … 7시까지' → {"depart_at": 18:00, "arrive_by": 19:00} (ISO). 말하지 않은 것은 뺀다."""
    found = {}
    for key, pattern in (("depart_at", DEPART), ("arrive_by", ARRIVE)):
        if (m := pattern.search(message)) and (t := when.parse_when(m.group(0), now)):
            found[key] = t.isoformat()
    return found


def rule_choose(message, now, location):
    picked = []
    if any(w in message for w in CANCEL_WORDS) and re.search(r"약속|알림|리마인드", message):
        picked.append({"name": "cancel_reminder", "args": {"query": reminder_text(message.replace("취소", ""))}})
    elif LIST_PATTERN.search(message):
        picked.append({"name": "list_reminders", "args": {}})
    elif ASK_PATTERN.search(message) and (due := when.parse_when(message, now)):
        picked.append({"name": "create_reminder", "args": {"text": reminder_text(message), "due_at": due.isoformat()}})
    elif route := route_args(message, now):
        picked.append({"name": "get_route", "args": route})
    else:
        day = (now + timedelta(days=when.day_offset(message, now) or 0)).date().isoformat()
        for name, words in KEYWORDS.items():
            if any(w in message for w in words):
                args = {"location": find_city(message) or location, "date": day}
                if name == "get_holidays":
                    args = holiday_range(message, now)
                picked.append({"name": name, "args": args})
    picked = picked[:MAX_TOOLS]
    if not picked:
        return ToolChoice([], 0.8, "도구 키워드가 없는 일상 대화입니다.", "rule")
    labels = ", ".join(TOOLS[p["name"]]["label"] for p in picked)
    return ToolChoice(picked, 0.75, f"'{labels}' 관련 표현이 들어 있습니다.", "rule")


# ── 모델 ────────────────────────────────────────────────────────────

def model_choose(message, history, now, location, llm):
    recent = "\n".join(f"{m['role']}: {m['content']}" for m in history[-4:]) or "(없음)"
    user = (f"[지금] {now.isoformat()} ({when.WEEKDAYS[now.weekday()]}요일)\n[달력]\n{when.calendar_hint(now)}\n"
            f"[사용자 지역] {location}\n[최근 대화]\n{recent}\n\n[메시지]\n{message}")
    data = llm.complete_json(router_prompt(), user)
    tools = []
    for t in data.get("tools") or []:
        if isinstance(t, dict) and t.get("name") in TOOLS:
            tools.append({"name": t["name"], "args": t.get("args") if isinstance(t.get("args"), dict) else {}})
    confidence = max(0.0, min(1.0, float(data.get("confidence", 0))))
    result = ToolChoice(tools, confidence, str(data.get("reason", "")), "model")

    # 안전장치: 지난 연도로 적힌 날짜는 올해로 고친다
    for t in result.tools:
        for key in ("date", "due_at", "from", "to", "depart_at", "arrive_by"):
            if key in t["args"]:
                t["args"][key], fixed = when.fix_year(t["args"][key], now)
                if fixed:
                    result.guards.append(f"{t['name']} 의 {key} 연도가 지난 연도라 {now.year}년으로 고쳤습니다.")

    # 안전장치: 같은 도구를 같은 인자로 두 번 고르면 하나만 남긴다. 이동 시간은 인자가 달라도 하나로 합친다
    # (실습에서 출발 시각용, 도착 시각용으로 나눠 두 번 골라 카드가 두 장 나왔다)
    merged = []
    for t in result.tools:
        same = next((m for m in merged if m["name"] == t["name"] and (t["name"] == "get_route" or m["args"] == t["args"])), None)
        if same is None:
            merged.append(t)
            continue
        for key, value in t["args"].items():
            if value and not same["args"].get(key):
                same["args"][key] = value
        result.guards.append(f"{t['name']} 를 두 번 골라 하나로 합쳤습니다.")
    result.tools = merged
    for t in result.tools:
        if t["name"] == "get_route":
            fix_route_clock(t["args"], message, now, result.guards)
        elif t["name"] == "create_reminder":
            keep_written_date(t["args"], message, now, result.guards)
    result.tools, result.dropped = limit_tools(result.tools, result.guards)
    return result


def fix_route_clock(args, message, now, guards):
    """메시지에 '6시에 퇴근', '7시까지'가 있는데 모델이 그 시각을 빠뜨리거나 지금 시각을 넣었으면 메시지의 시각으로 고친다."""
    for key, said in route_clocks(message, now).items():
        try:
            got = datetime.fromisoformat(str(args.get(key) or ""))
            got = got if got.tzinfo else got.replace(tzinfo=now.tzinfo)
        except ValueError:
            got = None   # 비었거나 못 읽는 값
        if got is None or (abs((got - now).total_seconds()) < 300 and got != datetime.fromisoformat(said)):
            args[key] = said
            guards.append(f"get_route 의 {key} 를 메시지에 나온 시각 {said[11:16]}(으)로 고쳤습니다.")


def keep_written_date(args, message, now, guards):
    """메시지에 적힌 날짜가 이미 지났는데 모델이 그 일정을 오늘이나 앞날로 옮겼으면 적힌 날짜로 되돌린다.

    production 에서 10월 21일, 23일 일정표 사진을 27일에 보냈더니 모델이 둘 다 27일 같은 시각으로 옮겨 저장했다.
    되돌린 시각은 create_reminder 가 '이미 지난 시각'으로 거절한다.
    """
    try:
        due = datetime.fromisoformat(str(args.get("due_at") or ""))
    except ValueError:
        return  # 못 읽는 값은 create_reminder 가 거절한다
    due = (due if due.tzinfo else due.replace(tzinfo=now.tzinfo)).astimezone(now.tzinfo)
    written = when.written_dates(message, now)
    if not written or due.date() in {day for day, _ in written}:
        return  # 적힌 날짜가 없거나, 적힌 날짜 중 하나를 그대로 썼다
    day = written_day(str(args.get("text") or ""), written)
    if day is None or day >= now.date() or due.date() < day:
        return  # 어느 줄의 날짜인지 모르거나, 아직 오지 않은 날이거나('30일 회의 하루 전에'), 앞날로 옮긴 게 아니다
    args["due_at"] = due.replace(year=day.year, month=day.month, day=day.day).isoformat()
    guards.append(f"create_reminder 의 날짜를 메시지에 적힌 {day.month}월 {day.day}일로 되돌렸습니다(이미 지난 날짜).")


def written_day(text, written):
    """약속 내용('정기 모임')의 낱말이 가장 많이 든 줄의 날짜. 맞는 줄이 없으면 적힌 날짜가 하나뿐일 때만 그 날짜. 정할 수 없으면 None."""
    words = [w for w in re.findall(r"[가-힣A-Za-z0-9]+", text) if len(w) >= 2]
    scores = [(sum(w in line for w in words), day) for day, line in written]
    best = max(score for score, _ in scores)
    days = {day for score, day in scores if score == best}
    return days.pop() if len(days) == 1 else None


def limit_tools(tools, guards):
    """약속 저장은 최대 MAX_REMINDERS 개, 다른 도구는 합쳐서 최대 MAX_TOOLS 개만 남긴다. (남긴 것, 뺀 것)을 돌려준다.

    뺀 것을 말없이 버리면 답이 그 약속까지 저장했다고 말할 수 있다. 그래서 뺀 것을 돌려주고 메모를 남긴다.
    """
    kept, dropped = [], []
    for t in tools:
        reminder = t["name"] == "create_reminder"
        same_kind = sum((k["name"] == "create_reminder") == reminder for k in kept)
        (kept if same_kind < (MAX_REMINDERS if reminder else MAX_TOOLS) else dropped).append(t)
    if dropped:
        guards.append(f"도구 한도(약속 {MAX_REMINDERS}개, 다른 도구 {MAX_TOOLS}개)를 넘어 {len(dropped)}개를 쓰지 않았습니다.")
    return kept, dropped


def choose(message, history, now, location, llm):
    if getattr(llm, "can_reason", False):
        try:
            result = model_choose(message, history, now, location, llm)
        except Exception as error:  # 모델 응답이 형식에 맞지 않으면 규칙으로 대신한다
            log("WARNING", "모델 도구 선택 실패, 키워드 규칙으로 대신함", error=type(error).__name__)
            result = rule_choose(message, now, location)
            result.guards.append("모델 응답을 읽지 못해 키워드 규칙으로 골랐습니다.")
    else:
        result = rule_choose(message, now, location)

    # 안전장치: 약속을 묻는 말인데 모델이 아무 도구도 못 골라 되묻게 될 때는, 되묻는 대신 약속 목록을 본다.
    # 되물으면 저장된 약속이 있어도 "모른다"고 답하게 된다(실습에서 확신도 0.00 으로 확인).
    if not result.tools and result.confidence < CONFIDENCE_THRESHOLD and LIST_PATTERN.search(message):
        result.tools = [{"name": "list_reminders", "args": {}}]
        result.confidence = CONFIDENCE_THRESHOLD
        result.guards.append("약속을 묻는 말이라 되묻지 않고 약속 목록을 봅니다.")

    # 안전장치: 확신이 낮으면 도구를 쓰지 않고 되묻는다
    if result.confidence < CONFIDENCE_THRESHOLD:
        result.action = "clarify"
        result.guards.append(f"확신도 {result.confidence:.2f}가 기준 {CONFIDENCE_THRESHOLD} 미만이라 도구를 쓰지 않고 되묻습니다.")
        result.tools, result.dropped = [], []

    log("INFO", "도구 선택", tools=[t["name"] for t in result.tools], confidence=round(result.confidence, 2),
        decided_by=result.decided_by, action=result.action)
    return result
