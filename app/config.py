"""설정 읽기. 앱이 시작할 때 한 번 실행된다.

꼭 필요한 값이 없으면 오류 로그를 남기고 바로 종료한다.
값 없이 시작해서 요청마다 오류를 돌려주는 것보다, 배포 단계에서 바로 실패하는 편이 문제를 빨리 찾을 수 있다.
"""
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from app.logs import log

ROOT = Path(__file__).resolve().parent.parent

# LLM 제공자별 기본값. 모델 이름은 바뀔 수 있으므로 LLM_MODEL 환경변수로 덮어쓸 수 있다.
PROVIDERS = {
    "openai": {"base_url": None, "model": "gpt-4o-mini"},
    "gemini": {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai/", "model": "gemini-3.8-flash"},
    "claude": {"base_url": None, "model": "claude-opus-5-5"},
    "fake": {"base_url": None, "model": "fake"},
}

# 크레딧 숫자는 여기 한곳에서만 정한다. 화면은 계산하지 않고 서버가 준 잔액만 보여 준다.
SIGNUP_CREDITS = 20       # 처음 온 사용자에게 한 번 주는 무료 크레딧
SIGNUP_DAILY_LIMIT = 50   # 하루(한국 날짜)에 무료 크레딧을 받는 사람 수 상한. 게스트를 계속 새로 만들어 받는 것을 막는다.
                          # 하루 최대 비용 = 50 × 20 × 메시지 원가. 시험할 때만 환경변수 SIGNUP_DAILY_LIMIT 로 바꾼다
CREDITS_PER_MESSAGE = 1   # /api/chat 메시지 하나에 쓰는 크레딧(위기 표현은 차감하지 않는다)
LOW_CREDITS = 10          # 잔액이 이만큼 이하면 화면이 입력창 위에 「얼마 남지 않았어요」 띠를 보여 준다

# 크레딧 상품표. 금액(원)과 크레딧 수는 여기 한곳에서만 정한다.
# 화면은 /api/products 로 받은 목록만 보여 주고, 주문은 상품 ID 만 보낸다. 금액은 서버가 이 표에서 찾는다.
# name 은 토스 결제창에 보이는 주문 이름이다(최대 100자).
PRODUCTS = {
    "credits_100": {"name": "하루친구 크레딧 100개", "amount": 1000, "credits": 100},
    "credits_550": {"name": "하루친구 크레딧 550개", "amount": 5000, "credits": 550},
    "credits_1200": {"name": "하루친구 크레딧 1,200개", "amount": 10000, "credits": 1200},
}


@dataclass
class Settings:
    provider: str
    api_key: str
    model: str
    base_url: str | None
    env: str
    commit: str
    revision: str
    storage: str              # memory, supabase
    auth: str                 # none, supabase
    supabase_url: str
    supabase_publishable_key: str
    supabase_secret_key: str
    google_login: bool
    show_turn_panel: bool
    ocr_enabled: bool
    toss_client_key: str = ""   # 화면에 줘도 되는 키(/api/config 로 내려 줌)
    toss_secret_key: str = field(default="", repr=False)   # 서버에서만. 화면, /api/config, 로그에 절대 넣지 않는다(repr 에도 안 나옴)
    signup_daily_limit: int = SIGNUP_DAILY_LIMIT

    @property
    def payments_enabled(self):
        """토스 키 두 개가 다 있을 때만 결제를 켠다. 하나라도 없으면 결제만 꺼지고 앱은 뜬다."""
        return bool(self.toss_client_key and self.toss_secret_key)


def load_dotenv(path):
    """노트북에서 실행할 때만 .env 파일을 읽는다. 배포할 때는 .env가 올라가지 않는다."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def env_flag(name, default):
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return default
    return value in ("1", "true", "yes", "on")


def stop(message):
    log("ERROR", message)
    sys.exit(1)


def check_toss_keys(client_key, secret_key):
    """토스페이먼츠 키를 확인한다. 값은 로그에 남기지 않고 이름만 말한다.

    - test_ 로 시작하지 않는 키(라이브 키)면 앱 시작을 멈춘다. 이 앱은 실제 돈이 오가지 않는 테스트 결제만 한다.
    - 클라이언트 키 칸에 시크릿 키(test_sk_, test_gsk_)를 넣었으면 멈춘다. 클라이언트 키는 화면에 그대로 내려가기 때문이다.
    - 주문서형 결제는 결제위젯 연동 키 한 쌍(test_gck_, test_gsk_)을 쓴다. 다른 종류면 경고만 남긴다(섞으면 토스가 INVALID_API_KEY 로 거절).
    """
    for name, value in (("TOSS_CLIENT_KEY", client_key), ("TOSS_SECRET_KEY", secret_key)):
        if value and not value.startswith("test_"):
            stop(f"{name} 가 test_ 로 시작하지 않아 앱을 시작하지 않습니다. 이 앱은 테스트 키로만 결제를 시험합니다(실제 돈이 오가지 않게).")
    if client_key.startswith(("test_sk_", "test_gsk_")):
        stop("TOSS_CLIENT_KEY 에 시크릿 키가 들어 있어 앱을 시작하지 않습니다. 클라이언트 키는 화면에 공개됩니다. 두 값을 바꿔 넣었는지 확인하세요.")
    if (client_key and not client_key.startswith("test_gck_")) or (secret_key and not secret_key.startswith("test_gsk_")):
        log("WARNING", "주문서형 결제는 결제위젯 연동 키 한 쌍(test_gck_, test_gsk_)을 씁니다. 다른 종류의 키면 결제가 거절될 수 있습니다.")


def load_settings():
    load_dotenv(ROOT / ".env")

    provider = os.environ.get("LLM_PROVIDER", "openai").strip().lower()
    if provider not in PROVIDERS:
        stop(f"LLM_PROVIDER 값 '{provider}' 를 알 수 없어 앱을 시작하지 않습니다. openai, gemini, claude, fake 중 하나를 쓰세요.")

    api_key = os.environ.get("LLM_API_KEY", "").strip()
    if provider != "fake" and not api_key:
        stop("LLM_API_KEY 환경변수가 없어 앱을 시작하지 않습니다.")

    env = os.environ.get("ENV", "local").strip()
    storage = os.environ.get("STORAGE", "memory").strip().lower()
    auth = os.environ.get("AUTH", "none").strip().lower()
    supabase_url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
    publishable = os.environ.get("SUPABASE_PUBLISHABLE_KEY", "").strip()
    secret = os.environ.get("SUPABASE_SECRET_KEY", "").strip()

    if storage not in ("memory", "supabase"):
        stop(f"STORAGE 값 '{storage}' 를 알 수 없습니다. memory 또는 supabase 를 쓰세요.")
    if auth not in ("none", "supabase"):
        stop(f"AUTH 값 '{auth}' 를 알 수 없습니다. none 또는 supabase 를 쓰세요.")
    if storage == "supabase" and auth != "supabase":
        stop("STORAGE=supabase 는 AUTH=supabase 와 함께 써야 합니다. 로그인 없이 DB에 저장하면 모든 사용자의 데이터가 한 사람 것으로 섞입니다.")
    if auth == "supabase" and not (supabase_url and publishable):
        stop("AUTH=supabase 인데 SUPABASE_URL 또는 SUPABASE_PUBLISHABLE_KEY 가 없어 앱을 시작하지 않습니다.")
    if storage == "supabase" and not secret:
        stop("STORAGE=supabase 인데 SUPABASE_SECRET_KEY 가 없어 앱을 시작하지 않습니다.")
    toss_client = os.environ.get("TOSS_CLIENT_KEY", "").strip()
    toss_secret = os.environ.get("TOSS_SECRET_KEY", "").strip()
    check_toss_keys(toss_client, toss_secret)

    defaults = PROVIDERS[provider]
    settings = Settings(
        provider=provider,
        api_key=api_key,
        model=os.environ.get("LLM_MODEL", "").strip() or defaults["model"],
        base_url=defaults["base_url"],
        env=env,
        commit=os.environ.get("COMMIT_SHA", "local").strip(),
        revision=os.environ.get("K_REVISION", "local"),  # Cloud Run이 리비전 이름을 넣어 준다
        storage=storage,
        auth=auth,
        supabase_url=supabase_url,
        supabase_publishable_key=publishable,
        supabase_secret_key=secret,
        google_login=env_flag("GOOGLE_LOGIN", False),
        # 개발용 "이번 턴" 패널. production 에서는 기본으로 끈다.
        show_turn_panel=env_flag("SHOW_TURN_PANEL", env != "production"),
        ocr_enabled=env_flag("OCR_ENABLED", False),  # 사진 글자 읽기. tesseract 프로그램이 필요하다
        toss_client_key=toss_client,
        toss_secret_key=toss_secret,
        signup_daily_limit=max(0, int(os.environ.get("SIGNUP_DAILY_LIMIT", "").strip() or SIGNUP_DAILY_LIMIT)),
    )
    log("INFO", "앱을 시작합니다.", provider=settings.provider, model=settings.model, env=settings.env,
        storage=settings.storage, auth=settings.auth, turn_panel=settings.show_turn_panel,
        route_tool=bool(os.environ.get("KAKAO_REST_KEY", "").strip()),  # 키 값이 아니라 있는지만 남긴다
        payments=settings.payments_enabled, signup_daily_limit=settings.signup_daily_limit)
    return settings
