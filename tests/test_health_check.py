"""능동 헬스체크 스크립트의 판정과 설정 쓰기만 시험한다. Docker 는 부르지 않는다."""
import argparse
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import health_check as hc  # noqa: E402


def test_parse_server_reads_weight():
    assert hc.parse_server("blue:8080=3") == hc.Server("blue:8080", 3)
    assert hc.parse_server("green:8080").weight == 1


@pytest.mark.parametrize("text", ["blue", "blue:8080=x", "blue:8080=0"])
def test_parse_server_rejects_bad_input(text):
    with pytest.raises(argparse.ArgumentTypeError):
        hc.parse_server(text)


def test_removed_only_after_consecutive_fails():
    s = hc.Server("green:8080")
    assert hc.record(s, False, fails=2, passes=2) is False
    assert hc.record(s, True, fails=2, passes=2) is False  # 성공이 한 번 끼면 처음부터 다시 센다
    assert hc.record(s, False, fails=2, passes=2) is False
    assert hc.record(s, False, fails=2, passes=2) is True
    assert s.healthy is False


def test_back_only_after_consecutive_passes():
    s = hc.Server("green:8080", healthy=False)
    assert hc.record(s, True, fails=2, passes=2) is False
    assert hc.record(s, False, fails=2, passes=2) is False
    assert hc.record(s, True, fails=2, passes=2) is False
    assert hc.record(s, True, fails=2, passes=2) is True
    assert s.healthy is True


def test_all_down_keeps_previous_list():
    blue, green = hc.Server("blue:8080"), hc.Server("green:8080")
    previous = [blue]
    blue.healthy = green.healthy = False
    assert hc.choose_active([blue, green], previous) is previous
    green.healthy = True
    assert hc.choose_active([blue, green], previous) == [green]


def test_render_conf_lists_given_servers_with_weight():
    text = hc.render_conf([hc.Server("blue:8080", 3), hc.Server("green:8080")])
    assert "    server blue:8080 weight=3;" in text
    assert "    server green:8080;" in text
    assert "proxy_pass http://active_app;" in text


def fake_docker(returncodes):
    calls = []

    def run(*args, timeout=10):
        calls.append(args)
        return subprocess.CompletedProcess(args, returncodes.pop(0))

    return run, calls


def test_apply_reverts_and_skips_reload_when_config_test_fails(tmp_path):
    conf = tmp_path / "default.conf"
    conf.write_text("old", encoding="utf-8")
    run, calls = fake_docker([1])
    assert hc.apply(conf, "new", "bg-nginx", run=run) is False
    assert conf.read_text(encoding="utf-8") == "old"
    assert calls == [("exec", "bg-nginx", "nginx", "-t")]


def test_apply_reloads_after_config_test_passes(tmp_path):
    conf = tmp_path / "default.conf"
    conf.write_text("old", encoding="utf-8")
    run, calls = fake_docker([0, 0])
    assert hc.apply(conf, "new", "bg-nginx", run=run) is True
    assert conf.read_text(encoding="utf-8") == "new"
    assert calls[-1] == ("exec", "bg-nginx", "nginx", "-s", "reload")


def test_probe_counts_timeout_as_failure():
    def run(*args, timeout=10):
        raise subprocess.TimeoutExpired(args, timeout)

    assert hc.probe("bg-nginx", hc.Server("green:8080"), 1, run=run) is False
