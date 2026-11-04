"""평가 데이터의 메시지를 내 컴퓨터에서 띄운 서버에 하나씩 보내 트레이스를 모은다.

data/eval/tool_selection.jsonl 과 data/eval/safety.jsonl 의 message 를 차례로 /api/chat 에 보낸다.
항목마다 먼저 DELETE /api/data 로 대화 · 기억 · 약속을 비워서 앞 항목이 다음 답에 섞이지 않게 한다.
history 가 있는 항목은 그 안의 사용자 말을 먼저 보내고 나서 본 메시지를 보낸다.

실행:
  python scripts/send_eval_messages.py                       # 앞에서 5개 (기본값)
  python scripts/send_eval_messages.py --skip 5 --limit 100  # 6번째부터 끝까지

진짜 LLM 으로 띄운 서버면 한 턴에 모델을 세 번쯤 부른다(비용 발생).
답 내용은 출력하지 않는다. 보낸 항목 수, 실패한 항목 수와 id 만 출력한다.
길찾기 항목은 서버에서 get_route 를 실행한다. 카카오 결과를 남기지 않으려면 KAKAO_REST_KEY 없이 띄운 서버에 보낸다.
"""
import argparse
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

import httpx

ROOT = Path(__file__).resolve().parent.parent
FILES = ("data/eval/tool_selection.jsonl", "data/eval/safety.jsonl")
LOCAL_HOSTS = ("localhost", "127.0.0.1")


def load_items():
    items = []
    for name in FILES:
        items += [json.loads(line) for line in (ROOT / name).read_text(encoding="utf-8").splitlines() if line.strip()]
    return items


def send(client, message):
    """메시지 하나를 보내고 응답 스트림을 끝까지 읽는다(기억 정리까지 끝나야 스트림이 닫힌다). 오류 없이 done 이 왔으면 True."""
    done = False
    with client.stream("POST", "/api/chat", json={"message": message}) as response:
        if response.status_code != 200:
            return False
        for line in response.iter_lines():
            if not line.strip():
                continue
            kind = json.loads(line).get("type")
            if kind == "error":
                return False
            done = done or kind == "done"
    return done


def run_item(client, item):
    if client.delete("/api/data").status_code != 200:
        return False
    for turn in item.get("history") or []:
        if turn["role"] == "user" and not send(client, turn["content"]):
            return False
    return send(client, item["message"])


def main():
    parser = argparse.ArgumentParser(description="평가 데이터 메시지로 트레이스 모으기")
    parser.add_argument("--limit", type=int, default=5, help="보낼 항목 수 (기본 5)")
    parser.add_argument("--skip", type=int, default=0, help="앞에서 건너뛸 항목 수")
    parser.add_argument("--url", default="http://localhost:8080", help="서버 주소. 항목마다 데이터를 지우므로 내 컴퓨터만 받는다")
    args = parser.parse_args()
    if urlparse(args.url).hostname not in LOCAL_HOSTS:
        sys.exit("내 컴퓨터에서 띄운 서버(localhost, 127.0.0.1)에만 보낸다. 항목마다 그 사용자의 데이터를 지우기 때문이다.")

    items = load_items()
    picked = items[args.skip:args.skip + args.limit]
    failed = []
    with httpx.Client(base_url=args.url, timeout=120) as client:
        for item in picked:
            try:
                ok = run_item(client, item)
            except (httpx.HTTPError, json.JSONDecodeError):
                ok = False
            if not ok:
                failed.append(item["id"])

    end = args.skip + len(picked)
    print(f"전체 {len(items)}개 중 {args.skip + 1}~{end}번: 보냄 {len(picked)}, 실패 {len(failed)}"
          + (f" ({', '.join(failed)})" if failed else ""))
    if end < len(items):
        print(f"이어 보내려면 --skip {end}")


if __name__ == "__main__":
    main()
