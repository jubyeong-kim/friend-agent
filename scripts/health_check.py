"""로드밸런서(Nginx) 뒤 서버를 먼저 확인해, 고장 난 서버는 빼고 살아나면 다시 넣는 스크립트(능동 헬스체크).

무료 Nginx 는 실제 요청이 실패해야 서버를 뺀다. 이 스크립트는 정해진 간격으로 각 서버의 /health 를 불러 보고,
연속 실패가 정한 횟수가 되면 그 서버를 upstream 에서 빼고, 연속 성공이 정한 횟수가 되면 다시 넣는다.
설정 파일을 다시 쓴 뒤 nginx -t 가 통과할 때만 reload 하고, 통과하지 못하면 이전 설정으로 되돌린다.
서버들은 Docker 네트워크 안에 있으므로 /health 는 Nginx 컨테이너 안에서 wget 으로 부른다.
모든 서버가 고장이면 마지막 목록을 그대로 둔다(목록이 비면 요청을 보낼 곳이 없다).

실행:
  python scripts/health_check.py --conf <Nginx 설정 파일> blue:8080=3 green:8080   # 서버:포트=가중치, 기본 가중치 1
  python scripts/health_check.py --conf <Nginx 설정 파일> blue:8080 green:8080 --interval 5 --timeout 1 --fails 2 --passes 2
시작하면 설정 파일을 주어진 서버 목록으로 다시 쓴다. 멈추려면 Ctrl+C. 멈추면 요약을 보여 준다.
"""
import argparse
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass
class Server:
    address: str  # 예: blue:8080
    weight: int = 1
    healthy: bool = True
    ok_streak: int = 0
    fail_streak: int = 0


def parse_server(text):
    """'blue:8080=3' 을 Server('blue:8080', 3) 로 바꾼다. 가중치를 빼면 1."""
    address, _, weight = text.partition("=")
    if ":" not in address:
        raise argparse.ArgumentTypeError(f"서버는 이름:포트 로 적는다: {text}")
    try:
        weight = int(weight) if weight else 1
    except ValueError:
        raise argparse.ArgumentTypeError(f"가중치는 숫자로 적는다: {text}") from None
    if weight < 1:
        raise argparse.ArgumentTypeError(f"가중치는 1 이상: {text}")
    return Server(address, weight)


def record(server, ok, fails, passes):
    """확인 결과 하나를 더한다. 이번 결과로 빠지거나 다시 들어갈 차례가 되면 True."""
    if ok:
        server.ok_streak += 1
        server.fail_streak = 0
        if not server.healthy and server.ok_streak >= passes:
            server.healthy = True
            return True
    else:
        server.fail_streak += 1
        server.ok_streak = 0
        if server.healthy and server.fail_streak >= fails:
            server.healthy = False
            return True
    return False


def choose_active(servers, previous):
    """요청을 보낼 서버 목록. 모두 고장이면 이전 목록을 그대로 쓴다."""
    return [s for s in servers if s.healthy] or previous


def render_conf(active):
    lines = ["upstream active_app {", "    zone active_app 64k;"]
    for s in active:
        weight = f" weight={s.weight}" if s.weight != 1 else ""
        lines.append(f"    server {s.address}{weight};")
    lines += ["}", "", "server {", "    listen 80;", "    location / {", "        proxy_pass http://active_app;", "    }", "}", ""]
    return "\n".join(lines)


def docker(*args, timeout=10):
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


def probe(nginx, server, timeout, run=docker):
    """Nginx 컨테이너 안에서 서버의 /health 를 부른다. timeout 초 안에 정상(2xx)으로 답하면 True."""
    try:
        result = run("exec", nginx, "wget", "-q", "-T", str(timeout), "-O", "/dev/null",
                     f"http://{server.address}/health", timeout=timeout + 5)
    except subprocess.TimeoutExpired:
        return False
    return result.returncode == 0


def apply(conf, text, nginx, run=docker):
    """설정을 쓰고 nginx -t 가 통과하면 reload 한다. 통과하지 못하면 이전 내용으로 되돌리고 False."""
    before = conf.read_text(encoding="utf-8")
    conf.write_text(text, encoding="utf-8", newline="\n")
    if run("exec", nginx, "nginx", "-t").returncode != 0:
        conf.write_text(before, encoding="utf-8", newline="\n")
        return False
    return run("exec", nginx, "nginx", "-s", "reload").returncode == 0


def show(active):
    return ", ".join(s.address + (f"={s.weight}" if s.weight != 1 else "") for s in active)


def main():
    parser = argparse.ArgumentParser(description="능동 헬스체크: 고장 난 서버를 빼고 살아나면 다시 넣기")
    parser.add_argument("servers", nargs="+", type=parse_server, help="서버:포트=가중치 (예: blue:8080=3 green:8080)")
    parser.add_argument("--conf", type=Path, required=True, help="Nginx 컨테이너의 conf.d 에 연결된 설정 파일")
    parser.add_argument("--nginx", default="bg-nginx", help="Nginx 컨테이너 이름, 기본 bg-nginx")
    parser.add_argument("--interval", type=float, default=5, help="확인 간격(초), 기본 5")
    parser.add_argument("--timeout", type=int, default=1, help="응답 기다림(초), 기본 1. 확인 간격보다 짧아야 한다")
    parser.add_argument("--fails", type=int, default=2, help="연속 실패 몇 번이면 뺄지, 기본 2")
    parser.add_argument("--passes", type=int, default=2, help="연속 성공 몇 번이면 다시 넣을지, 기본 2")
    parser.add_argument("--seconds", type=int, default=0, help="최대 실행 시간(초), 기본 0 은 Ctrl+C 까지")
    args = parser.parse_args()
    if not args.conf.is_file():
        parser.error(f"설정 파일이 없다: {args.conf}")
    if not 1 <= args.timeout < args.interval:
        parser.error("응답 기다림은 1초 이상, 확인 간격보다 짧게 정한다")
    if args.fails < 1 or args.passes < 1:
        parser.error("--fails 와 --passes 는 1 이상")

    def now():
        return datetime.now().strftime("%H:%M:%S.%f")[:-3]

    servers = args.servers
    active = servers
    if not apply(args.conf, render_conf(active), args.nginx):
        print(f"{now()}  시작 실패: nginx -t 를 통과하지 못해 이전 설정으로 되돌림")
        return
    print(f"{now()}  설정 반영: {show(active)}")
    print(f"{args.interval:g}초마다 /health 를 {args.timeout}초 기다려 확인합니다. "
          f"연속 실패 {args.fails}번이면 빼고, 연속 성공 {args.passes}번이면 다시 넣습니다. 멈추려면 Ctrl+C.")

    changes = 0
    started = time.time()
    try:
        while not args.seconds or time.time() - started < args.seconds:
            round_started = time.time()
            for s in servers:
                ok = probe(args.nginx, s, args.timeout)
                if record(s, ok, args.fails, args.passes):
                    print(f"{now()}  {s.address}  {'정상 판정' if s.healthy else '고장 판정'}")
                    if not any(x.healthy for x in servers):
                        print(f"{now()}  모든 서버가 고장 — 보낼 곳이 없어지지 않게 목록을 그대로 둔다")
                elif not ok and s.healthy:
                    print(f"{now()}  {s.address}  실패 {s.fail_streak}/{args.fails}")
                elif ok and not s.healthy:
                    print(f"{now()}  {s.address}  성공 {s.ok_streak}/{args.passes}")
            wanted = choose_active(servers, active)
            if [s.address for s in wanted] != [s.address for s in active]:
                if apply(args.conf, render_conf(wanted), args.nginx):
                    active = wanted
                    changes += 1
                    print(f"{now()}  설정 반영: {show(active)}")
                else:
                    print(f"{now()}  설정 실패: nginx -t 를 통과하지 못해 이전 설정으로 되돌림, 다음 확인 때 다시 시도")
            time.sleep(max(0.0, args.interval - (time.time() - round_started)))
    except KeyboardInterrupt:
        pass

    print("\n── 요약 ──")
    print(f"설정을 바꾼 횟수 {changes}번, 걸린 시간 {time.time() - started:.0f}초")
    for s in servers:
        print(f"  {s.address}: {'정상' if s.healthy else '고장'}")
    print(f"지금 요청을 받는 서버: {show(active)}")


if __name__ == "__main__":
    main()
