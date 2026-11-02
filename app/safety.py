"""모델 판단과 상관없이 코드 규칙으로 처리하는 안전장치."""
import re

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


def risk_hints(text):
    return sum(bool(p.search(text)) for p in HINT_PATTERNS)


def is_crisis(text):
    return any(p.search(text) for p in CRISIS_PATTERNS) or risk_hints(text) >= 2


def has_risk_hint(text):
    """위기 표현은 아니지만 조심해서 답해야 할 표현이 있는지."""
    return risk_hints(text) >= 1


def is_sensitive(text):
    return any(p.search(text) for p in SENSITIVE_PATTERNS)
