"""크레딧 결제 테스트. 실제 토스페이먼츠를 부르지 않는다.

승인 API 는 httpx.MockTransport 로 만든 가짜 응답으로 바꿔 끼운다(가짜 LLM 과 같은 방식, main.get_payments).
가짜 응답 모양은 토스 코어 API 문서의 Payment 객체(status, approvedAt, orderId, totalAmount)를 따른다.
진짜 클라이언트 코드(app/payments.py)를 그대로 지나가므로 Basic 헤더와 멱등키를 만드는 것까지 시험된다.
중복 승인과 금액 위조 시험은 다음 단계에서 따로 한다.
"""
import base64
import json
import logging
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

from app import config, main
from app.auth import User
from app.main import app, store
from app.payments import CONFIRM_URL, TossPayments

client = TestClient(app)
FAKE_SECRET = "test_gsk_fake_secret_for_tests"   # 가짜 문자열. 실제 키가 아니다


@pytest.fixture
def uid():
    """이 테스트에서만 쓰는 새 사용자(test_credits.py 와 같은 방식)."""
    new = f"pay-test-{uuid.uuid4().hex}"
    app.dependency_overrides[main.current_user] = lambda: User(new, anonymous=True)
    yield new
    app.dependency_overrides.pop(main.current_user, None)


@pytest.fixture
def toss():
    """가짜 토스. 받은 요청을 calls 에 모으고, reply 로 응답(상태 코드, 본문)을 바꿀 수 있다."""
    fake = {"calls": [], "reply": None}

    def handler(request):
        body = json.loads(request.content)
        fake["calls"].append({"url": str(request.url), "headers": request.headers, "body": body})
        if fake["reply"]:
            status, payload = fake["reply"]
            return httpx.Response(status, json=payload)
        return httpx.Response(200, json={"paymentKey": body["paymentKey"], "orderId": body["orderId"],
                                         "status": "DONE", "method": "카드", "totalAmount": body["amount"],
                                         "approvedAt": "2026-11-04T15:00:00+09:00"})

    app.dependency_overrides[main.get_payments] = lambda: TossPayments(FAKE_SECRET, transport=httpx.MockTransport(handler))
    yield fake
    app.dependency_overrides.pop(main.get_payments, None)


def test_products_come_from_server_table():
    """상품표·잔액 띠 기준·1개 가격은 서버 값이다(화면은 계산하지 않는다)."""
    r = client.get("/api/products").json()
    assert [(p["id"], p["amount"], p["credits"]) for p in r["products"]] == [
        ("credits_100", 1000, 100), ("credits_550", 5000, 550), ("credits_1200", 10000, 1200)]
    assert [p["unit_price"] for p in r["products"]] == [10, 9.09, 8.33]
    assert r["products"][0]["name"] == "하루친구 크레딧 100개"
    assert (r["low_credits"], r["credits_per_message"]) == (10, 1)


def test_order_amount_is_set_by_server(uid, toss):
    """화면이 금액을 같이 보내도 서버는 상품표 금액으로 대기 주문을 만든다. 주문 번호는 토스 규칙에 맞는 무작위 값."""
    r = client.post("/api/orders", json={"product_id": "credits_550", "amount": 1})
    assert r.status_code == 200
    order = r.json()
    assert (order["amount"], order["credits"], order["order_name"]) == (5000, 550, "하루친구 크레딧 550개")
    assert order["order_id"].startswith("ord_") and len(order["order_id"]) == 36
    saved = store.get_order(uid, order["order_id"])
    assert (saved["status"], saved["amount"], saved["credits"]) == ("pending", 5000, 550)
    assert store.get_order("someone-else", order["order_id"]) is None   # 남의 주문은 보이지 않는다
    assert client.post("/api/orders", json={"product_id": "credits_999"}).status_code == 400


def test_paid_order_adds_credits(uid, toss):
    """정상 결제 한 건: 주문 → 승인 → 주문 완료 + 원장에 purchase 한 줄 + 잔액이 늘어난다."""
    client.get("/api/me")   # 가입 무료 20
    order = client.post("/api/orders", json={"product_id": "credits_550"}).json()
    r = client.post("/api/payments/confirm",
                    json={"payment_key": "tgen_fake_payment_key", "order_id": order["order_id"], "amount": 5000})
    assert r.status_code == 200 and r.json() == {"credits": 550, "balance": 570, "already": False}

    # 토스에 보낸 요청: 승인 주소, Basic 인증(시크릿 키 + 콜론), 멱등키, 서버에 저장한 금액
    [call] = toss["calls"]
    assert call["url"] == CONFIRM_URL
    assert call["headers"]["authorization"] == "Basic " + base64.b64encode(f"{FAKE_SECRET}:".encode()).decode()
    assert call["headers"]["idempotency-key"] == f"confirm-{order['order_id']}"
    assert call["body"] == {"paymentKey": "tgen_fake_payment_key", "orderId": order["order_id"], "amount": 5000}

    saved = store.get_order(uid, order["order_id"])
    assert (saved["status"], saved["payment_key"]) == ("done", "tgen_fake_payment_key")
    assert saved["approved_at"].isoformat() == "2026-11-04T15:00:00+09:00"
    bought = [c for c in store.recent_credits(uid, 10) if c["reason"] == "purchase"]
    assert [(c["delta"], c["reference"]) for c in bought] == [(550, f"order:{order['order_id']}")]


@pytest.mark.parametrize("status, payload, http, order_status", [
    (403, {"code": "REJECT_CARD_COMPANY", "message": "카드사에서 거절했어요"}, 400, "failed"),       # 확실한 거절
    (500, {"code": "FAILED_INTERNAL_SYSTEM_PROCESSING", "message": "내부 오류"}, 502, "pending"),  # 결과 모름
    (409, {"code": "IDEMPOTENT_REQUEST_PROCESSING", "message": "처리 중"}, 502, "pending"),        # 결과 모름
])
def test_confirm_failure_sets_order_status(uid, toss, status, payload, http, order_status):
    """토스가 거절하면 주문은 실패, 결과를 모르면 대기로 둔다. 어느 쪽이든 크레딧은 늘지 않는다."""
    order = client.post("/api/orders", json={"product_id": "credits_100"}).json()
    toss["reply"] = (status, payload)
    r = client.post("/api/payments/confirm",
                    json={"payment_key": "tgen_fake_payment_key", "order_id": order["order_id"], "amount": 1000})
    assert r.status_code == http
    assert store.get_order(uid, order["order_id"])["status"] == order_status
    assert store.credit_balance(uid) == 0


def test_fail_page_marks_pending_order_failed(uid, toss):
    """실패 주소로 돌아오면 대기 주문을 실패로 바꾼다."""
    order = client.post("/api/orders", json={"product_id": "credits_100"}).json()
    r = client.post("/api/payments/fail", json={"order_id": order["order_id"], "code": "PAY_PROCESS_CANCELED"})
    assert r.status_code == 200
    assert store.get_order(uid, order["order_id"])["status"] == "failed"
    assert toss["calls"] == []


def test_config_has_client_key_but_never_secret(monkeypatch):
    """/api/config 에는 클라이언트 키만 있고 시크릿 키는 어디에도 없다."""
    monkeypatch.setattr(main.settings, "toss_client_key", "test_gck_fake_client_for_tests")
    monkeypatch.setattr(main.settings, "toss_secret_key", FAKE_SECRET)
    r = client.get("/api/config")
    assert r.json()["payments_enabled"] is True
    assert r.json()["toss_client_key"] == "test_gck_fake_client_for_tests"
    assert FAKE_SECRET not in r.text


@pytest.mark.parametrize("client_key, secret_key, outcome", [
    ("", "", "off"),                                           # 둘 다 없음 → 결제만 꺼짐
    ("test_gck_fake", "", "off"),                              # 하나만 있음 → 결제만 꺼짐
    ("test_gck_fake", "test_gsk_fake", "on"),
    ("test_gck_fake", "live_gsk_fake", "stop"),                # 라이브 키 → 앱 시작을 멈춤
    ("live_gck_fake", "test_gsk_fake", "stop"),
    ("test_gsk_fake", "test_gck_fake", "stop"),                # 클라이언트 칸에 시크릿 키 → 화면에 나가므로 멈춤
])
def test_toss_key_guard(monkeypatch, client_key, secret_key, outcome):
    monkeypatch.setenv("TOSS_CLIENT_KEY", client_key)   # 빈 값도 넣어 두면 .env 가 덮어쓰지 않는다
    monkeypatch.setenv("TOSS_SECRET_KEY", secret_key)
    if outcome == "stop":
        with pytest.raises(SystemExit):
            config.load_settings()
    else:
        assert config.load_settings().payments_enabled is (outcome == "on")


def test_access_log_hides_payment_key():
    """성공 주소의 접속 로그 한 줄에서 paymentKey 값이 가려진다(주문 번호와 금액은 남음)."""
    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 0, '%s - "%s %s HTTP/%s" %d',
                               ("1.2.3.4:5", "GET", "/payments/success?paymentType=NORMAL&orderId=ord_x&paymentKey=tgen_secret_value&amount=5000", "1.1", 200), None)
    main.HidePaymentKey().filter(record)
    line = record.getMessage()
    assert "tgen_secret_value" not in line and "paymentKey=hidden" in line and "amount=5000" in line
