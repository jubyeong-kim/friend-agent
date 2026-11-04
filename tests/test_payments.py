"""크레딧 결제 테스트. 실제 토스페이먼츠를 부르지 않는다.

승인 API 는 httpx.MockTransport 로 만든 가짜 응답으로 바꿔 끼운다(가짜 LLM 과 같은 방식, main.get_payments).
가짜 응답 모양은 토스 코어 API 문서의 Payment 객체(status, approvedAt, orderId, totalAmount)를 따른다.
진짜 클라이언트 코드(app/payments.py)를 그대로 지나가므로 Basic 헤더와 멱등키를 만드는 것까지 시험된다.
중복 승인과 금액 위조 시험은 다음 단계에서 따로 한다.
"""
import base64
import json
import logging
import threading
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


# ── 12-7 중복 결제와 위조 막기 ─────────────────────────────────────────
# 실제 토스는 부르지 않는다. 가짜 승인이 몇 번 불렸는지(toss["calls"])로 「토스까지 갔는지」를 본다.

PAYMENT_KEY = "tgen_fake_payment_key_12_7"   # 가짜 값


def done_payment(body):
    """승인된 Payment 객체 모양의 가짜 응답(코어 API 문서의 칸만)."""
    return {"paymentKey": body["paymentKey"], "orderId": body["orderId"], "status": "DONE", "method": "카드",
            "totalAmount": body["amount"], "approvedAt": "2026-11-04T15:00:00+09:00"}


def swap_toss(handler):
    """이 테스트에서만 가짜 승인의 답을 바꾼다. toss 픽스처가 끝날 때 함께 되돌린다."""
    app.dependency_overrides[main.get_payments] = lambda: TossPayments(FAKE_SECRET, transport=httpx.MockTransport(handler))


def new_order(product_id="credits_550"):
    return client.post("/api/orders", json={"product_id": product_id}).json()


def confirm(order, amount=None, payment_key=PAYMENT_KEY):
    return client.post("/api/payments/confirm", json={"payment_key": payment_key, "order_id": order["order_id"],
                                                      "amount": order["amount"] if amount is None else amount})


def purchases(uid):
    return [c for c in store.recent_credits(uid, 50) if c["reason"] == "purchase"]


def test_same_confirm_twice_adds_credits_once(uid, toss):
    """같은 승인 요청을 두 번(성공 화면 새로고침): 두 번째는 already 이고 토스를 다시 안 부른다. 크레딧과 원장 줄은 한 번만."""
    order = new_order("credits_100")
    first, second = confirm(order), confirm(order)
    assert first.json() == {"credits": 100, "balance": 100, "already": False}
    assert second.status_code == 200 and second.json() == {"credits": 100, "balance": 100, "already": True}
    assert len(toss["calls"]) == 1
    assert [(c["delta"], c["reference"]) for c in purchases(uid)] == [(100, f"order:{order['order_id']}")]


def test_two_confirms_at_once_add_credits_once(uid, toss):
    """두 요청이 동시에 승인 단계에 들어가 둘 다 토스를 불러도 크레딧은 한 번만 쌓이고 주문은 완료 하나다.
    (진짜 토스는 같은 멱등키의 두 번째에 409 를 줄 수 있다. 여기서는 둘 다 성공하는 더 나쁜 경우로 본다)"""
    order = new_order()
    together = threading.Barrier(2, timeout=5)

    def handler(request):
        together.wait()   # 두 요청이 모두 토스 호출 안에 들어온 뒤에 답한다
        return httpx.Response(200, json=done_payment(json.loads(request.content)))

    fake = TossPayments(FAKE_SECRET, transport=httpx.MockTransport(handler))
    req = main.ConfirmRequest(payment_key=PAYMENT_KEY, order_id=order["order_id"], amount=order["amount"])
    results, errors = [], []

    def run():
        try:
            results.append(main.confirm_payment(req, user=User(uid, anonymous=True), toss=fake))
        except Exception as error:  # 스레드 안 오류도 테스트가 보게
            errors.append(error)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and len(results) == 2
    assert len(purchases(uid)) == 1 and store.credit_balance(uid) == 550
    assert store.get_order(uid, order["order_id"])["status"] == "done"


def test_changed_amount_is_refused_before_toss(uid, toss):
    """주소의 금액을 바꿔 보내면 400. 가짜 승인은 한 번도 안 불리고, 주문은 대기, 원장은 그대로. 맞는 금액으로는 그 뒤에도 승인된다."""
    order = new_order("credits_1200")
    r = confirm(order, amount=100)
    assert r.status_code == 400
    assert toss["calls"] == []
    assert store.get_order(uid, order["order_id"])["status"] == "pending"
    assert store.recent_credits(uid, 10) == []
    assert confirm(order).status_code == 200 and store.credit_balance(uid) == 1200


def test_someone_elses_order_is_not_found(uid, toss):
    """다른 사용자가 남의 주문 번호로 승인 · 실패를 보내면 승인은 404, 실패 표시는 아무것도 안 바꾼다. 가짜 승인은 안 불리고 주인 주문은 그대로."""
    order = new_order()
    app.dependency_overrides[main.current_user] = lambda: User(f"intruder-{uuid.uuid4().hex}", anonymous=True)
    assert confirm(order).status_code == 404
    client.post("/api/payments/fail", json={"order_id": order["order_id"], "code": "PAY_PROCESS_CANCELED"})
    assert toss["calls"] == []
    assert store.get_order(uid, order["order_id"])["status"] == "pending"
    assert store.credit_balance(uid) == 0


def answer_timeout(request, body):
    raise httpx.ReadTimeout("가짜 시간 초과", request=request)


def answer_already_processed(request, body):
    return httpx.Response(400, json={"code": "ALREADY_PROCESSED_PAYMENT", "message": "이미 처리된 결제 입니다."})


def answer_not_done(request, body):
    return httpx.Response(200, json={**done_payment(body), "status": "IN_PROGRESS"})


def answer_other_amount(request, body):
    return httpx.Response(200, json={**done_payment(body), "totalAmount": body["amount"] + 1})


@pytest.mark.parametrize("answer", [answer_timeout, answer_already_processed, answer_not_done, answer_other_amount])
def test_unknown_result_keeps_order_pending_then_retry_works(uid, toss, answer):
    """기존 실패 테스트(403 · 500 · 409)에 없는 「결과 모름」 경우: 502 · 주문 대기 · 크레딧 0.
    그 뒤 다시 보내면 같은 멱등키로 승인되고 크레딧은 한 번만."""
    order = new_order()
    keys = []

    def handler(request):
        keys.append(request.headers["idempotency-key"])
        return answer(request, json.loads(request.content))

    swap_toss(handler)
    assert confirm(order).status_code == 502
    assert store.get_order(uid, order["order_id"])["status"] == "pending" and store.credit_balance(uid) == 0

    def approve(request):
        keys.append(request.headers["idempotency-key"])
        return httpx.Response(200, json=done_payment(json.loads(request.content)))

    swap_toss(approve)
    assert confirm(order).json() == {"credits": 550, "balance": 550, "already": False}
    assert keys == [f"confirm-{order['order_id']}"] * 2 and len(purchases(uid)) == 1


def test_rejected_order_is_not_sent_again(uid, toss):
    """토스가 거절해 실패가 된 주문을 다시 보내면 409. 토스를 다시 부르지 않고 크레딧은 0."""
    order = new_order()
    toss["reply"] = (403, {"code": "REJECT_CARD_COMPANY", "message": "카드사에서 거절했어요"})
    assert confirm(order).status_code == 400
    toss["reply"] = None
    assert confirm(order).status_code == 409
    assert len(toss["calls"]) == 1 and store.credit_balance(uid) == 0


@pytest.mark.parametrize("broken", ["add_credit", "complete_order"])
def test_record_failure_then_retry_adds_credits_once(uid, toss, monkeypatch, broken):
    """승인 뒤 기록(크레딧 또는 주문 완료)이 한 번 실패하면 500 · 주문 대기.
    다시 보내면 같은 멱등키로 승인하고, 크레딧은 한 번만 쌓이고, 주문은 완료된다."""
    order = new_order()
    real = getattr(store, broken)

    def fail_once(*args, **kwargs):
        monkeypatch.setattr(store, broken, real)   # 다음 호출부터는 정상
        raise RuntimeError("가짜 저장소 오류")

    monkeypatch.setattr(store, broken, fail_once)
    first = confirm(order)
    assert first.status_code == 500
    assert store.get_order(uid, order["order_id"])["status"] == "pending"
    second = confirm(order)
    assert second.status_code == 200 and second.json()["credits"] == 550
    assert len(purchases(uid)) == 1 and store.credit_balance(uid) == 550
    assert store.get_order(uid, order["order_id"])["status"] == "done"
    assert [c["headers"]["idempotency-key"] for c in toss["calls"]] == [f"confirm-{order['order_id']}"] * 2


def test_confirm_log_lines_have_key_but_no_secrets(uid, toss, capsys):
    """승인 API 를 부를 때마다 「결제 승인 호출」 한 줄(주문 번호 · 같은 멱등키 · 결과). 토스를 안 부르면 줄도 없다.
    로그 어디에도 paymentKey · 시크릿 키(원문과 base64) · user_id 가 없다."""
    order = new_order()
    toss["reply"] = (500, {"code": "FAILED_INTERNAL_SYSTEM_PROCESSING", "message": "내부 오류"})
    confirm(order)                       # 1) 결과 모름
    toss["reply"] = None
    confirm(order)                       # 2) 승인
    confirm(order)                       # 3) 이미 완료 → 토스 안 부름
    out = capsys.readouterr().out
    lines = []
    for raw in out.splitlines():
        try:
            lines.append(json.loads(raw))
        except ValueError:
            pass
    calls = [line for line in lines if line.get("message") == "결제 승인 호출"]
    key = f"confirm-{order['order_id']}"
    assert [(c["order_id"], c["idempotency_key"], c["result"]) for c in calls] == [
        (order["order_id"], key, "unknown"), (order["order_id"], key, "done")]
    assert calls[0]["code"] == "FAILED_INTERNAL_SYSTEM_PROCESSING" and "code" not in calls[1]
    basic = base64.b64encode(f"{FAKE_SECRET}:".encode()).decode()
    for hidden in (PAYMENT_KEY, FAKE_SECRET, basic, uid):
        assert hidden not in out


def test_fail_mark_during_confirm_keeps_order_and_ledger_consistent(uid, toss):
    """승인 API 가 답하기 전에 /api/payments/fail 이 같은 주문을 실패로 바꾸는 경우.
    바라는 모습: 크레딧이 쌓였으면 주문은 done, 주문이 failed 면 크레딧은 없음(돈은 받았는데 주문은 실패인 상태가 없어야)."""
    order = new_order()

    def handler(request):
        body = json.loads(request.content)
        # 승인이 처리되는 사이에 같은 사용자의 실패 표시 요청이 먼저 끝난 것처럼
        main.payment_failed(main.FailRequest(order_id=body["orderId"], code="PAY_PROCESS_CANCELED"),
                            user=User(uid, anonymous=True))
        return httpx.Response(200, json=done_payment(body))

    swap_toss(handler)
    confirm(order)
    state = (len(purchases(uid)), store.get_order(uid, order["order_id"])["status"])
    assert state in ((1, "done"), (0, "failed")), f"크레딧 줄 {state[0]}개 · 주문 {state[1]}"
