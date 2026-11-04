"""토스페이먼츠 결제 승인 API 를 부르는 작은 클라이언트. 시크릿 키를 쓰는 곳은 이 파일 하나뿐이다.

근거 문서(v2)
- 결제 승인과 Payment 객체: https://docs.tosspayments.com/reference
- 인증 헤더와 멱등키: https://docs.tosspayments.com/reference/using-api/authorization
- 에러 코드: https://docs.tosspayments.com/reference/error-codes

- 인증은 Basic. 시크릿 키 뒤에 콜론(:)을 붙여 base64 로 바꾼다. 콜론이 없으면 토스가 거절한다.
- 같은 주문은 늘 같은 멱등키(confirm-주문번호)로 보낸다. 같은 키로 다시 보내면 토스가 다시 처리하지 않고
  첫 응답을 그대로 돌려준다(문서 기준). 승인은 됐는데 우리 기록이 실패했을 때 다시 시도하는 길로 쓴다.
- 결과는 셋 중 하나로 정리해 돌려준다. 시크릿 키와 paymentKey 는 돌려주지도 로그에 남기지도 않는다.
  done     승인됨          {"result": "done", "approved_at": datetime 또는 None}
  rejected 토스가 확실히 거절 {"result": "rejected", "code", "message"}
  unknown  결과를 모름     {"result": "unknown", "code"}  시간 초과, 연결 실패, 5xx, 처리 중
"""
import base64
from datetime import datetime

import httpx

CONFIRM_URL = "https://api.tosspayments.com/v1/payments/confirm"

# 4xx 라도 결과를 모르는 것으로 보는 코드. 이때는 주문을 대기로 두고 같은 멱등키로 다시 시도하게 한다.
# - IDEMPOTENT_REQUEST_PROCESSING(409): 같은 멱등키의 첫 요청이 아직 처리 중
# - ALREADY_PROCESSED_PAYMENT(400): 이미 처리된 결제. 승인됐을 수도 있어서 실패로 바꾸지 않는다
UNKNOWN_CODES = {"IDEMPOTENT_REQUEST_PROCESSING", "ALREADY_PROCESSED_PAYMENT"}


def idempotency_key(order_id):
    return f"confirm-{order_id}"


def parse_approved_at(value):
    """approvedAt 은 ISO 8601(예 2022-01-01T00:00:00+09:00). 읽지 못하면 None."""
    try:
        return datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


class TossPayments:
    def __init__(self, secret_key, transport=None, timeout=30):
        """transport 는 테스트가 가짜 응답(httpx.MockTransport)을 끼울 때만 쓴다."""
        token = base64.b64encode(f"{secret_key}:".encode("utf-8")).decode("ascii")
        self.http = httpx.Client(timeout=timeout, transport=transport,
                                 headers={"Authorization": f"Basic {token}", "Content-Type": "application/json"})

    def confirm(self, payment_key, order_id, amount):
        """결제 승인. amount 는 화면이 보낸 값이 아니라 서버에 저장한 주문 금액을 넘긴다."""
        try:
            r = self.http.post(CONFIRM_URL, json={"paymentKey": payment_key, "orderId": order_id, "amount": amount},
                               headers={"Idempotency-Key": idempotency_key(order_id)})
        except httpx.HTTPError as error:
            return {"result": "unknown", "code": type(error).__name__}
        try:
            body = r.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}

        if r.status_code == 200:
            # 승인된 Payment 객체. 주문 번호와 금액이 우리 주문과 같은지 한 번 더 본다
            if body.get("status") == "DONE" and body.get("orderId") == order_id and body.get("totalAmount") == amount:
                return {"result": "done", "approved_at": parse_approved_at(body.get("approvedAt"))}
            return {"result": "unknown", "code": f"UNEXPECTED_{body.get('status') or 'BODY'}"}

        code = str(body.get("code") or f"HTTP_{r.status_code}")[:80]
        if r.status_code >= 500 or r.status_code in (409, 429) or code in UNKNOWN_CODES:
            return {"result": "unknown", "code": code}
        return {"result": "rejected", "code": code, "message": str(body.get("message") or "")[:200]}
