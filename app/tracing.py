"""Langfuse 계측. LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY 가 둘 다 있을 때만 켜진다.

관찰 단위
- chat-turn(트레이스): safety, choose-tools, tool:<도구 이름>, reply, memory-update, memory-summary
- greeting(트레이스): 먼저 말 걸기 한 번. 루트가 generation 이다

/api/chat 은 제너레이터를 스레드풀에서 한 조각씩 돌린다. 조각마다 컨텍스트가 새로 복사되므로
'현재 관찰'에 기대는 with start_as_current_observation 을 쓰지 않고, 부모 객체에서 자식을 직접 만든다.

입력과 출력 원문은 Langfuse 로 보내되, 보내기 전에 전화번호, 이메일, 카드 번호를 가린다.
길찾기(get_route) 결과는 카카오 API 결과라 저장하면 안 된다. hide() 로 등록한 글은 같은 트레이스 안에서 모두 가린다.
서버 로그에는 지금처럼 원문을 남기지 않는다.
"""
import os
import re
from contextvars import ContextVar
from datetime import datetime, timezone

from app.logs import log

CARD = re.compile(r"(?<!\d)\d{4}[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}(?!\d)")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PHONE = re.compile(r"(?<!\d)(?:\+82[- ]?|0)(?:1[016789]|[2-6]\d?)[- .]?\d{3,4}[- .]?\d{4}(?!\d)")
ROUTE_HIDDEN = "[길찾기 결과 가림]"
SAFETY_TAG = "safety"  # 안전 응답을 보낸 트레이스의 tag

_client = None
_generation = ContextVar("langfuse_generation", default=None)  # LLM 클래스가 기록할 generation


def setup():
    """앱이 시작할 때 한 번. 키가 없으면 계측을 끄고 앱은 그대로 돈다."""
    global _client
    public = os.environ.get("LANGFUSE_PUBLIC_KEY", "").strip()
    secret = os.environ.get("LANGFUSE_SECRET_KEY", "").strip()
    if not (public and secret):
        _client = None
        log("INFO", "Langfuse 계측 꺼짐(키 없음)")
        return
    from langfuse import Langfuse

    _client = Langfuse(public_key=public, secret_key=secret,
                       base_url=os.environ.get("LANGFUSE_BASE_URL", "").strip() or None)
    log("INFO", "Langfuse 계측 켜짐")  # 키 값은 남기지 않는다


def scrub(value, hidden=()):
    """보내기 전에 가린다. dict, list 안의 글도 모두."""
    if isinstance(value, str):
        for text in hidden:
            value = value.replace(text, ROUTE_HIDDEN)
        value = CARD.sub("[카드 번호]", value)
        value = EMAIL.sub("[이메일]", value)
        return PHONE.sub("[전화번호]", value)
    if isinstance(value, dict):
        return {k: scrub(v, hidden) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(v, hidden) for v in value]
    return value


def deployment():
    """모든 트레이스에 붙는 배포 정보. 없으면 config.py 와 같이 local."""
    return {"environment": os.environ.get("ENV", "local").strip() or "local",
            "version": os.environ.get("K_REVISION", "").strip() or "local",  # Cloud Run 이 넣어 준다
            "commit": os.environ.get("COMMIT_SHA", "").strip() or "local"}


def propagated(attrs):
    """트레이스 속성(user_id, session_id, environment, version, metadata, tags).

    propagate_attributes 는 관찰이 시작될 때만 붙는다. 부모에서 자식을 직접 만들기 때문에 만들 때마다 다시 감싼다.
    """
    from langfuse import propagate_attributes

    return propagate_attributes(**{k: v for k, v in attrs.items() if v not in (None, [], {})})


class Observation:
    """Langfuse 관찰 하나. 계측이 꺼져 있으면 아무것도 하지 않는다. 계측 오류가 대화를 멈추지 않게 한다."""

    def __init__(self, span=None, hidden=None, attrs=None):
        self.span = span
        self.hidden = hidden if hidden is not None else set()  # 같은 트레이스 안에서 함께 쓴다
        self.attrs = attrs if attrs is not None else {}        # 같은 트레이스 안에서 함께 쓴다

    @property
    def trace_id(self):
        """이 관찰이 속한 트레이스 ID. 계측이 꺼져 있으면 None. 답 메시지 meta 에 넣어 점수와 잇는다."""
        return getattr(self.span, "trace_id", None)

    def _safe(self, action):
        if self.span is None:
            return None
        try:
            return action()
        except Exception as error:
            log("WARNING", "Langfuse 기록 실패", error=type(error).__name__)
            return None

    def _clean(self, fields):
        for key in ("input", "output", "metadata"):
            if fields.get(key) is not None:
                fields[key] = scrub(fields[key], self.hidden)
        return fields

    def hide(self, text):
        """이 트레이스에서 이 글을 가린다(길찾기 결과처럼 저장하면 안 되는 것)."""
        if text:
            self.hidden.add(text)

    def tag(self, tags=(), metadata=None):
        """중간에 정해진 값(고른 도구, 안전 응답)을 붙인다.
        이 뒤에 만드는 관찰에 propagate 하고(tags 는 트레이스에 합쳐진다), 이 관찰(루트)의 metadata 에도 남긴다."""
        if self.span is None:
            return
        self.attrs["tags"] = [*self.attrs.get("tags", []), *(t for t in tags if t not in self.attrs.get("tags", []))]
        if metadata:
            self.attrs["metadata"] = {**self.attrs.get("metadata", {}), **metadata}
            self.update(metadata=metadata)

    def child(self, name, as_type="span", **fields):
        def start():
            with propagated(self.attrs):
                return self.span.start_observation(name=name, as_type=as_type, **self._clean(fields))

        return Observation(self._safe(start), self.hidden, self.attrs)

    def update(self, **fields):
        self._safe(lambda: self.span.update(**self._clean(fields)))

    def end(self, **fields):
        if fields:
            self.update(**fields)
        self._safe(lambda: self.span.end())

    def run(self, fn, *args):
        """LLM 을 한 번 부르는 동안 이 generation 을 '지금 기록할 곳'으로 둔다."""
        token = _generation.set(self)
        try:
            return fn(*args)
        finally:
            _generation.reset(token)

    def stream(self, chunks):
        """스트리밍. 조각을 꺼낼 때마다 기록할 곳을 잡았다 놓는다(조각마다 컨텍스트가 바뀌어도 된다).
        첫 글자가 나온 시각을 completion_start_time 으로 남긴다."""
        first = True
        while True:
            token = _generation.set(self)
            try:
                chunk = next(chunks, None)
            finally:
                _generation.reset(token)
            if chunk is None:
                return
            if first and chunk:
                first = False
                self.update(completion_start_time=datetime.now(timezone.utc))
            yield chunk


def trace(name, as_type="span", user_id=None, session_id=None, tags=(), trace_metadata=None, **fields):
    """트레이스(루트 관찰)를 시작한다. user_id 는 로그인으로 확인한 ID 만 넘긴다(이메일 아님)."""
    if _client is None:
        return Observation()
    info = deployment()
    attrs = {"user_id": user_id, "session_id": session_id, "environment": info["environment"],
             "version": info["version"], "tags": list(tags),
             "metadata": {"commit": info["commit"], **(trace_metadata or {})}}
    try:
        with propagated(attrs | {"trace_name": name}):
            span = _client.start_observation(name=name, as_type=as_type, **Observation()._clean(fields))
    except Exception as error:
        log("WARNING", "Langfuse 기록 실패", error=type(error).__name__)
        span = None
    return Observation(span, attrs=attrs)


def session_id(user_id, now):
    """사용자 ID + 한국 날짜. 하루 대화를 한 세션으로 묶는다."""
    return f"{user_id}-{now.date().isoformat()}"


def finish(root, **fields):
    """트레이스를 끝내고 보낸다. Cloud Run 은 응답이 끝나면 CPU 가 거의 없어서, 요청 안에서 보낸다."""
    root.end(**fields)
    if _client is not None and root.span is not None:
        try:
            _client.flush()
        except Exception as error:
            log("WARNING", "Langfuse 전송 실패", error=type(error).__name__)


def link(obs):
    """답 메시지 meta 에 더할 트레이스 ID. 계측이 꺼져 있으면 빈 dict(meta 가 지금과 같다)."""
    return {"trace_id": obs.trace_id} if obs.trace_id else {}


def score(trace_id, name, value, comment=None, timestamp=None):
    """트레이스에 BOOLEAN 점수(1/0)를 남긴다. 보냈으면 True. 키나 트레이스 ID 가 없으면 건너뛴다.

    같은 답에 다시 누르면 덮어쓴다. Langfuse 는 id, name, timestamp 의 날짜가 모두 같을 때만 덮어쓰므로
    score_id 를 '{trace_id}-{name}' 으로, timestamp 를 답 메시지의 저장 시각으로 고정한다.
    """
    if _client is None or not trace_id:
        return False
    try:
        _client.create_score(name=name, value=float(value), data_type="BOOLEAN", trace_id=trace_id,
                             score_id=f"{trace_id}-{name}", timestamp=timestamp,
                             comment=scrub(comment) if comment else None,
                             environment=deployment()["environment"])
        _client.flush()  # Cloud Run 은 응답이 끝나면 CPU 가 거의 없어서, 요청 안에서 보낸다
        return True
    except Exception as error:
        log("WARNING", "Langfuse 점수 전송 실패", error=type(error).__name__)
        return False


def record_llm(model, input, output, input_tokens=None, output_tokens=None):
    """LLM 클래스 세 개가 같은 방식으로 부른다. 지금 기록할 generation 이 없으면 아무것도 하지 않는다."""
    gen = _generation.get()
    if gen is None:
        return
    usage = {k: v for k, v in (("input", input_tokens), ("output", output_tokens)) if v is not None}
    gen.update(model=model, input=input, output=output, usage_details=usage or None)
