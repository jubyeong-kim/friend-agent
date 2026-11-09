"""공휴일 도구: 연휴 일수 계산, 좁은 기간 · 지난 기간 받아 주기.

공휴일 자료는 공개 API(date.nager.at)를 부르지 않고 아래 표로 바꿔 끼운다. 날짜는 그 API 의 2026 · 2027 한국 자료에서 골랐다.
'오늘'은 staging 기준 측정 날짜인 2026-11-09(월)로 고정한다.
"""
from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from app import tools, when
from app.main import store

KST = ZoneInfo("Asia/Seoul")
FAKE = {
    # 2025 공개 자료: 어린이날과 부처님 오신 날이 같은 날인데 대체공휴일(5/6)이 없고, 아직 공휴일이 아니던 제헌절이 들어 있다
    2025: [(date(2025, 5, 5), "어린이날"), (date(2025, 5, 5), "부처님 오신 날"), (date(2025, 7, 17), "제헌절"),
           (date(2025, 12, 25), "크리스마스")],
    2026: [(date(2026, 1, 1), "새해"), (date(2026, 5, 5), "어린이날"), (date(2026, 9, 24), "추석"),
           (date(2026, 9, 25), "추석"), (date(2026, 9, 26), "추석"), (date(2026, 12, 25), "크리스마스")],
    # 2027 공개 자료: 대체공휴일 8/16 을 '광복절', 5/3 을 '노동절'로 적었고, 성탄절(토) 대체공휴일 12/27 은 빠져 있다
    2027: [(date(2027, 1, 1), "새해"), (date(2027, 2, 6), "설날"), (date(2027, 2, 8), "설날"), (date(2027, 2, 9), "설날"),
           (date(2027, 5, 3), "노동절"), (date(2027, 5, 5), "어린이날"), (date(2027, 6, 6), "현충일"),
           (date(2027, 8, 16), "광복절"), (date(2027, 9, 14), "추석"), (date(2027, 9, 15), "추석"), (date(2027, 9, 16), "추석"),
           (date(2027, 12, 25), "크리스마스")],
    2028: [(date(2028, 1, 1), "새해")],
}


@pytest.fixture(autouse=True)
def fixed_calendar(monkeypatch):
    monkeypatch.setattr(tools, "holidays_of", lambda year: FAKE.get(year, []))
    monkeypatch.setattr(when, "now_in", lambda tz: datetime(2026, 11, 9, 12, 0, tzinfo=KST))


def ask(**args):
    return tools.get_holidays(args, tools.Context(store, "local-user", "서울", "Asia/Seoul"))


def test_friday_holiday_says_three_days_with_weekend():
    r = ask(**{"from": "2026-12-01", "to": "2026-12-31"})
    assert "12월 25일(금) 크리스마스(주말과 이어 12월 25일(금)~12월 27일(일) 사흘 연휴)" in r.summary
    assert r.data["breaks"] == [{"from": "2026-12-25", "to": "2026-12-27", "days": 3}]


def test_narrow_range_without_holiday_adds_next_holiday():
    # 기준 측정: '다음 공휴일에 며칠 쉬어?'를 11/9~11/22 로 조회해 '없음'만 받았다
    r = ask(**{"from": "2026-11-09", "to": "2026-11-22"})
    assert r.summary.startswith("2026-11-09부터 2026-11-22까지 공휴일이 없습니다.")
    assert "그 뒤 첫 공휴일은 12월 25일(금) 크리스마스(주말과 이어 12월 25일(금)~12월 27일(일) 사흘 연휴)" in r.summary
    assert r.data["holidays"] == [] and r.data["next"]["holidays"] == [{"date": "2026-12-25", "name": "크리스마스"}]


def test_past_range_also_gives_a_year_later():
    # 기준 측정: '내년 1월 1일에 연휴야?'를 2026-01-01(목)로 조회했다. 2027-01-01 은 금요일
    r = ask(**{"from": "2026-01-01", "to": "2026-01-01"})
    assert "이미 지난 기간" in r.summary and "그때 공휴일: 1월 1일(목) 새해(하루 쉼)" in r.summary
    assert "1년 뒤 같은 기간 2027-01-01~2027-01-01 공휴일: 1월 1일(금) 새해(주말과 이어 1월 1일(금)~1월 3일(일) 사흘 연휴)" in r.summary


def test_weekday_holiday_is_one_day_not_long_weekend():
    r = ask(**{"from": "2027-05-01", "to": "2027-05-31"})
    assert "5월 5일(수) 어린이날(하루 쉼)" in r.summary and "연휴" not in r.summary


def test_weekend_holiday_without_substitute_says_so():
    # 현충일 · 신정은 대체공휴일 대상이 아니다
    r = ask(**{"from": "2027-06-01", "to": "2027-06-30"})
    assert "6월 6일(일) 현충일(일요일이라 원래 쉬는 날, 대체공휴일 대상이 아니라 따로 더 쉬는 날은 없음)" in r.summary and "연휴" not in r.summary
    new_year = ask(**{"from": "2028-01-01", "to": "2028-01-31"})
    assert "1월 1일(토) 새해(토요일이라 원래 쉬는 날, 대체공휴일 대상이 아니라 따로 더 쉬는 날은 없음)" in new_year.summary


def test_mislabeled_substitute_goes_back_to_real_date():
    # 공개 자료의 '광복절 2027-08-16(월)'은 대체공휴일이다. 광복절은 8/15(일)
    r = ask(**{"from": "2027-08-01", "to": "2027-08-31"})
    assert "8월 15일(일) 광복절(대체공휴일 8월 16일(월), 주말과 이어 8월 14일(토)~8월 16일(월) 사흘 연휴)" in r.summary
    assert "8월 16일(월) 광복절" not in r.summary


def test_missing_substitute_is_added():
    # 성탄절이 토요일이면 다음 첫 평일(월)이 대체공휴일인데 공개 자료에 없다
    r = ask(**{"from": "2027-12-01", "to": "2027-12-31"})
    assert "12월 25일(토) 크리스마스(대체공휴일 12월 27일(월), 주말과 이어 12월 25일(토)~12월 27일(월) 사흘 연휴)" in r.summary
    # 기간이 성탄절에서 끝나도 같은 연휴의 대체공휴일을 말한다
    short = ask(**{"from": "2027-12-20", "to": "2027-12-25"})
    assert "대체공휴일 12월 27일(월)" in short.summary


def test_two_holidays_on_one_day_get_a_substitute():
    assert (date(2025, 5, 6), "어린이날 대체공휴일") in tools.korean_holidays(2025)


def test_holiday_before_it_became_one_is_dropped():
    assert all(n != "제헌절" for _, n in tools.korean_holidays(2025))


def test_labor_day_substitute_is_left_unknown():
    # 노동절(2026년부터 공휴일) 대체공휴일은 출처마다 달라 정하지 않는다. 공개 자료의 5/3 도 쓰지 않는다
    r = ask(**{"from": "2027-05-01", "to": "2027-05-04"})
    assert "5월 1일(토) 노동절(토요일이라 원래 쉬는 날, 대체공휴일은 출처마다 달라 확인 필요)" in r.summary
    assert "5월 3일" not in r.summary


def test_multi_day_holiday_is_one_long_weekend():
    r = ask(**{"from": "2027-02-01", "to": "2027-02-28"})
    assert r.summary.count("연휴") == 1
    assert "설날(주말과 이어 2월 6일(토)~2월 9일(화) 나흘 연휴)" in r.summary
    midweek = ask(**{"from": "2027-09-01", "to": "2027-09-30"})
    assert "(9월 14일(화)~9월 16일(목) 사흘 연휴)" in midweek.summary and "주말과 이어" not in midweek.summary


def test_empty_args_look_one_year_ahead():
    r = ask()
    assert r.data["from"] == "2026-11-09" and r.data["to"] == "2027-11-09"
    assert r.summary.split(": ", 1)[1].startswith("12월 25일(금) 크리스마스(주말과 이어")


def test_neighbor_year_failure_does_not_break_answer(monkeypatch):
    # 1월 1일의 연휴를 세려면 전해 12월 31일을 본다. 그 해 자료를 못 불러와도 답은 나와야 한다
    def flaky(year):
        if year == 2025:
            raise httpx.ConnectError("down")
        return FAKE.get(year, [])
    monkeypatch.setattr(tools, "holidays_of", flaky)
    r = ask(**{"from": "2026-01-01", "to": "2026-01-01"})
    assert r.ok and "1월 1일(목) 새해(하루 쉼)" in r.summary


def test_a_year_later_failure_keeps_past_result(monkeypatch):
    # 로컬 확인 중 공개 API 가 3초 안에 답하지 않아(ReadTimeout) 도구 전체가 실패한 일이 있었다
    def slow_next_year(year):
        if year == 2027:
            raise httpx.ReadTimeout("slow")
        return FAKE.get(year, [])
    monkeypatch.setattr(tools, "holidays_of", slow_next_year)
    r = ask(**{"from": "2026-01-01", "to": "2026-01-01"})
    assert r.ok and "그때 공휴일: 1월 1일(목) 새해(하루 쉼)" in r.summary
    assert "1년 뒤 같은 기간 2027-01-01~2027-01-01 공휴일은 지금 불러오지 못했습니다." in r.summary
    assert "a_year_later" not in r.data
