"""테스트는 실제 AI를 부르지 않는다. 가짜 응답(fake)과 서버 메모리 저장소로 실행한다.

실제 AI 호출은 비용이 들고, 느리고, 같은 질문에도 답이 달라서 통과와 실패를 판정할 수 없다.
"""
import os
import uuid

import pytest

os.environ.update({"LLM_PROVIDER": "fake", "LLM_API_KEY": "", "STORAGE": "memory", "AUTH": "none",
                   "LANGFUSE_PUBLIC_KEY": "", "LANGFUSE_SECRET_KEY": "",  # 테스트 데이터를 Langfuse 로 보내지 않는다
                   "TOSS_CLIENT_KEY": "", "TOSS_SECRET_KEY": "",  # 진짜 토스 승인 클라이언트를 만들지 않는다(결제 테스트는 가짜를 끼운다)
                   "SIGNUP_DAILY_LIMIT": "1000000"})  # 테스트는 새 사용자를 많이 만든다. 하루 상한 시험은 test_signup_limit.py 가 따로
os.environ.setdefault("ENV", "test")


@pytest.fixture(autouse=True)
def plenty_of_credits():
    """크레딧이 생기기 전부터 있던 테스트는 개발용 사용자로 /api/chat 을 여러 번 부른다.
    잔액 0 에서 402 가 나지 않게 테스트마다 넉넉히 채운다(참조는 매번 새 값이라 쌓인다).
    크레딧 자체를 확인하는 테스트(test_credits.py)는 테스트마다 새 사용자를 써서 이 값의 영향을 받지 않는다."""
    from app.auth import LOCAL_USER
    from app.main import store
    store.add_credit(LOCAL_USER, 1000, "purchase", f"test-topup:{uuid.uuid4().hex}")
