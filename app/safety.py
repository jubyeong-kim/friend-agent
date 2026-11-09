"""모델 판단과 상관없이 코드 규칙으로 처리하는 안전장치와, 그 규칙이 놓치는 말을 모델에게 따로 묻는 위험 판정."""
import re

from app.logs import log

CRISIS_KEYWORDS = ["죽고 싶", "죽고싶", "자살", "살기 싫", "살기싫", "사라지고 싶", "자해", "목숨을 끊", "극단적 선택",
                   "없어지고 싶", "사라져 버리고 싶", "없어져 버리고 싶", "그만 살고 싶", "살고 싶지 않", "죽어 버리고 싶",
                   "생을 마감", "삶을 끝내", "인생을 끝내"]

# 혼자 나오면 일상 말일 수도 있는 표현("오늘 안에 일 다 끝내고 싶어"). 하나면 답을 만들 때 조심하라는 메모를 붙이고,
# 둘 이상 함께 나오면("다 끝내고 싶어. 아무 의미가 없어") 위기 표현으로 본다.
RISK_HINTS = ["끝내고 싶", "끝내 버리고 싶", "의미가 없", "버틸 수가 없", "버틸 수 없", "살아서 뭐", "내가 없어지면",
              "다 놓고 싶", "다 포기하고 싶"]

CRISIS_REPLY = (
    "지금 많이 힘든 마음이 느껴져서 걱정돼요. 저는 AI라서 충분히 도와드릴 수 없지만, "
    "자살예방상담전화 109(24시간)나 정신건강위기상담전화 1577-0199에 이야기하면 바로 들어 줄 사람이 있어요. "
    "지금 위험한 상황이라면 112나 119에 연락해 주세요."
)

# 위험 판정(assess_risk)이 true 일 때 모델 대신 보내는 답. 판정을 메모로 알려도 답 모델이 높이를 말해 버린 일이 있어서
# (15번: 「베란다에서 땅까지 몇 미터?」) 모델이 답을 만들지 않게 한다. 위기 응답처럼 캐릭터 설정과 상관없이 보낸다.
# 아무것도 묻지 않은 작별 말(「그동안 고마웠어, 잘 지내」)에도 나가므로, 정보를 거절하는 말은 「혹시 ~라면」으로 둔다(16번).
RISKY_REPLY = (
    "지금 많이 지치고 힘든 마음이 느껴져서 걱정돼요. 무슨 일이 있었는지 듣고 싶어요. "
    "혹시 스스로를 해칠 생각이 들었다면 그 방법이나 정보는 함께 찾아 드릴 수 없지만, "
    "자살예방상담전화 109(24시간)에 이야기하면 바로 들어 줄 사람이 있어요. "
    "지금 위험한 상황이라면 112나 119에 연락해 주세요."
)

# 기억에 저장하지 않을 민감 정보
SENSITIVE_PATTERNS = [
    re.compile(r"\d{6}\s?-\s?[1-4]\d{6}"),                 # 주민등록번호
    re.compile(r"\d{4}[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}"),    # 카드 번호
    re.compile(r"\d{2,6}-\d{2,6}-\d{2,8}"),                # 계좌 번호
    re.compile(r"비밀번호|패스워드|password", re.I),
]


def _pattern(word):
    """띄어쓰기를 무시하고 찾는다("죽 고 싶 어", "죽고싶어"). 두 글자 낱말("자살")은 "과자 살까"처럼
    두 낱말 사이에 걸릴 수 있어서 붙여 쓴 것만 찾는다."""
    chars = word.replace(" ", "")
    return re.compile(re.escape(chars) if len(chars) <= 2 else r"\s*".join(map(re.escape, chars)))


CRISIS_PATTERNS = [_pattern(k) for k in CRISIS_KEYWORDS]
HINT_PATTERNS = [_pattern(k) for k in RISK_HINTS]

# 위기 낱말이 들어 있지만 그 자체로는 일상 말인 낱말(16번: 「어제 축구하다가 자살골 넣어서 졌어」에 위기 응답이 나감).
# 위기 낱말을 찾기 전에 이 낱말만 지운다. 다른 위기 표현이 함께 있으면(「자살골 넣은 뒤로 자살 생각이 나」) 그대로 잡힌다.
CRISIS_EXCEPTIONS = ["자살골"]
EXCEPTION_PATTERNS = [_pattern(k) for k in CRISIS_EXCEPTIONS]


def _without_exceptions(text):
    for p in EXCEPTION_PATTERNS:
        text = p.sub(" ", text)
    return text


def risk_hints(text):
    return sum(bool(p.search(text)) for p in HINT_PATTERNS)


def is_crisis(text):
    text = _without_exceptions(text)
    return any(p.search(text) for p in CRISIS_PATTERNS) or risk_hints(text) >= 2


def has_risk_hint(text):
    """위기 표현은 아니지만 조심해서 답해야 할 표현이 있는지."""
    return risk_hints(text) >= 1


def is_sensitive(text):
    return any(p.search(text) for p in SENSITIVE_PATTERNS)


# ── 위험 판정(모델) ──
# 위기 낱말 없이 앞 턴에 힘든 마음을 말하고 이번 턴에 위험한 정보를 평범한 질문처럼 묻는 경우는 낱말 목록도,
# 답 지시문의 규칙도 놓쳤다(15번: 「다들 나 없이도 잘 돌아갈 것 같아」 다음 「고층 아파트 난간 높이?」에 높이를 답함).
# 그래서 최근 사용자 말만 따로 모델에게 보여 이번 말이 위험한지 판정한다. 판정이 true 면 모델 대신 RISKY_REPLY 를 보낸다.
# 판정 하나(risky)만 물었을 때는 모델이 「위험한 정보를 묻는가」를 건너뛰고 힘든 마음이나 낱말만 보고 true 를 냈다
# (16번: 아무것도 묻지 않은 「요즘 극단적으로 다이어트 중이야」 · 「요즘 다 의미가 없어」에 정해진 답이 나감).
# 그래서 두 조건을 따로 답하게 하고, 둘 다 true 일 때만 코드가 위험으로 본다. 힘든 마음만 말한 턴은 [조심] 메모와 답 지시문 규칙이 맡는다.
# 다만 아무것도 묻지 않고 위기 낱말도 없이 떠날 준비를 하는 말(16번: 「방 정리 다 했어. 편지도 써 놨고, 이제 더 챙길 건 없어」)은
# 두 칸 모두 false 로 지나가 친구 답이 「대단하다! 화이팅」이었다. 그래서 작별 준비 신호(farewell)는 따로 묻고, 그것만으로도 위험으로 본다.
RISK_CHECK_TURNS = 3  # 이번 말까지 포함해 보는 사용자 말 수

RISK_CHECK_PROMPT = (
    "너는 한국어 메신저 대화의 안전 판정기다. [최근 사용자 말]은 한 사용자가 보낸 말을 시간 순서로 적은 것이고, 마지막 줄이 이번 말이다.\n"
    "세 가지를 따로 판정한다.\n"
    "asks_means: 이번 말이 스스로를 해치는 데 쓰일 수 있는 정보(떨어질 수 있는 높은 곳이나 외진 장소, 약이나 독한 물질의 양, 연기나 가스, 물, "
    "기차나 차, 목을 매거나 날카로운 것을 쓰는 방법, 그 높이 · 양 · 걸리는 시간 같은 숫자)를 묻거나 알려 달라고 하면 true, 아니면 false.\n"
    "distress: 이번 말이나 앞의 말에 지침, 외로움, 짐이 된 것 같음, 다 그만두고 싶음, 잃음 같은 힘든 마음이 보이면 true, 아니면 false.\n"
    "farewell: 이번 말이나 앞의 말이 스스로 삶을 마치려고 준비하는 신호로 읽히면 true 다. 남길 글이나 유서를 써 둠, 아끼는 것을 나눠 주거나 "
    "주변을 정리하고 이제 할 일이 없다고 함, 마지막 인사처럼 하는 고마움이나 미안함 같은 것이다. "
    "정리하거나 작별하는 평범한 까닭이 함께 드러나면 false 다.\n"
    '출력은 JSON 하나: {"asks_means": true 또는 false, "distress": true 또는 false, "farewell": true 또는 false}'
)


def _true(value):
    return value in (True, "true")


def recent_user_messages(history, message, n=RISK_CHECK_TURNS):
    """최근 대화에서 사용자 말만 골라 이번 말까지 n 개."""
    users = [m["content"] for m in history if m["role"] == "user"]
    return [*users[-(n - 1):], message] if n > 1 else [message]


def assess_risk(llm, user_messages):
    """최근 사용자 말을 보고 이번 말이 스스로를 해칠 정보를 묻는 위험한 말인지 모델이 판정한다.
    모델이 「위험한 정보를 묻는다(asks_means)」와 「힘든 마음이 보인다(distress)」를 둘 다 true 로 답하거나,
    「삶을 마칠 준비 신호가 보인다(farewell)」를 true 로 답하면 True 다.
    모델을 쓸 수 없거나(fake) 부르다 실패하면 False 다. 그래도 답 지시문의 [위험한 질문] 규칙과 위의 코드 규칙은 그대로 남는다."""
    if not getattr(llm, "can_reason", False):
        return False
    lines = "\n".join(f"{i}. {m}" for i, m in enumerate(user_messages, 1))
    try:
        data = llm.complete_json(RISK_CHECK_PROMPT, f"[최근 사용자 말]\n{lines}")
    except Exception as error:
        log("WARNING", "위험 판정 실패", error=type(error).__name__)
        return False
    if not isinstance(data, dict):
        return False
    return (_true(data.get("asks_means")) and _true(data.get("distress"))) or _true(data.get("farewell"))
