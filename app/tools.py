"""캐릭터가 쓸 수 있는 도구.

외부 도구(날씨, 미세먼지, 공휴일)는 모두 키 없이 부를 수 있는 공개 API를 쓴다.
이동 시간 도구만 카카오맵 REST API 키(KAKAO_REST_KEY)가 필요하다. 키가 없으면 그 도구만 꺼진다.
약속 도구는 외부를 부르지 않고 저장소에만 쓴다.
각 도구는 모델이 읽을 한 문장 요약(summary)과 화면에 보여 줄 원본(data)을 함께 돌려준다.
"""
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import httpx

from app import when
from app.logs import log

TIMEOUT = 3.0  # 외부 API 한 번에 기다리는 최대 시간(초)

# 지역 이름 → 좌표. 공개 지오코딩 API는 한국어 지명을 잘못 찾는 경우가 있어 주요 도시를 직접 적어 둔다.
CITIES = {
    "서울": (37.5665, 126.9780), "부산": (35.1796, 129.0756), "인천": (37.4563, 126.7052),
    "대구": (35.8714, 128.6014), "대전": (36.3504, 127.3845), "광주": (35.1595, 126.8526),
    "울산": (35.5384, 129.3114), "세종": (36.4800, 127.2890), "수원": (37.2636, 127.0286),
    "성남": (37.4200, 127.1267), "고양": (37.6584, 126.8320), "용인": (37.2411, 127.1776),
    "창원": (35.2280, 128.6811), "청주": (36.6424, 127.4890), "전주": (35.8242, 127.1480),
    "천안": (36.8151, 127.1139), "포항": (36.0190, 129.3435), "제주": (33.4996, 126.5312),
    "서귀포": (33.2541, 126.5600), "강릉": (37.7519, 128.8761), "춘천": (37.8813, 127.7298),
    "원주": (37.3422, 127.9202), "여수": (34.7604, 127.6622), "경주": (35.8562, 129.2247),
    "목포": (34.8118, 126.3922), "속초": (38.2070, 128.5918),
}

WEATHER_CODES = [
    (0, "맑음"), (3, "구름 조금"), (48, "안개"), (57, "이슬비"), (67, "비"),
    (77, "눈"), (82, "소나기"), (86, "눈 소나기"), (99, "뇌우"),
]


@dataclass
class ToolResult:
    name: str
    ok: bool
    summary: str
    data: dict = field(default_factory=dict)


@dataclass
class Context:
    """도구가 실행될 때 필요한 사용자 정보."""
    store: object
    user_id: str
    location: str
    timezone: str


def weather_text(code):
    for limit, text in WEATHER_CODES:
        if code <= limit:
            return text
    return "알 수 없음"


def resolve_city(name, default):
    name = (name or "").replace("시", "").strip() or default
    for city in CITIES:
        if city in name:
            return city
    return None


def day_word(offset):
    return {0: "오늘", 1: "내일", 2: "모레"}.get(offset, f"{offset}일 뒤")


def day_index(args, ctx, last):
    """args 의 date(YYYY-MM-DD)를 오늘로부터 며칠 뒤인지로 바꾼다. 예보 범위(0~last일) 밖이면 None."""
    today = when.now_in(ctx.timezone).date()
    try:
        offset = (date.fromisoformat(str(args.get("date") or today.isoformat())) - today).days
    except ValueError:
        offset = 0
    return offset if 0 <= offset <= last else None


# ── 외부 정보 도구 ────────────────────────────────────────────────

def get_weather(args, ctx):
    city = resolve_city(args.get("location"), ctx.location)
    if not city:
        return ToolResult("get_weather", False, f"'{args.get('location')}' 지역의 날씨는 지원하지 않습니다.")
    offset = day_index(args, ctx, 6)
    if offset is None:
        return ToolResult("get_weather", False, "날씨 예보는 오늘부터 6일 뒤까지만 볼 수 있습니다.")
    lat, lon = CITIES[city]
    r = httpx.get("https://api.open-meteo.com/v1/forecast", timeout=TIMEOUT, params={
        "latitude": lat, "longitude": lon, "timezone": ctx.timezone, "forecast_days": 7,
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"})
    r.raise_for_status()
    d = r.json()["daily"]
    i = offset
    data = {"city": city, "date": d["time"][i], "weather": weather_text(d["weather_code"][i]),
            "min": d["temperature_2m_min"][i], "max": d["temperature_2m_max"][i],
            "rain_probability": d["precipitation_probability_max"][i]}
    summary = (f"{city} {day_word(offset)}({data['date']}) 날씨: {data['weather']}, "
               f"최저 {data['min']:.0f}도 최고 {data['max']:.0f}도, 강수 확률 {data['rain_probability']}%.")
    return ToolResult("get_weather", True, summary, data)


def grade(value, cuts):
    for limit, text in zip(cuts, ["좋음", "보통", "나쁨"]):
        if value <= limit:
            return text
    return "매우 나쁨"


def get_air_quality(args, ctx):
    city = resolve_city(args.get("location"), ctx.location)
    if not city:
        return ToolResult("get_air_quality", False, f"'{args.get('location')}' 지역의 미세먼지는 지원하지 않습니다.")
    offset = day_index(args, ctx, 4)
    if offset is None:
        return ToolResult("get_air_quality", False, "미세먼지 예보는 오늘부터 4일 뒤까지만 볼 수 있습니다.")
    lat, lon = CITIES[city]
    r = httpx.get("https://air-quality-api.open-meteo.com/v1/air-quality", timeout=TIMEOUT, params={
        "latitude": lat, "longitude": lon, "timezone": ctx.timezone, "forecast_days": 5, "hourly": "pm10,pm2_5"})
    r.raise_for_status()
    h = r.json()["hourly"]
    day = h["time"][offset * 24][:10]
    rows = [(p10, p25) for t, p10, p25 in zip(h["time"], h["pm10"], h["pm2_5"]) if t.startswith(day) and p10 is not None]
    if not rows:
        return ToolResult("get_air_quality", False, f"{city} {day_word(offset)} 미세먼지 예보가 아직 없습니다.")
    pm10 = round(sum(r[0] for r in rows) / len(rows))
    pm25 = round(sum(r[1] for r in rows) / len(rows))
    # 환경부 예보 등급 기준(㎍/㎥): 미세먼지 30/80/150, 초미세먼지 15/35/75
    data = {"city": city, "date": day, "pm10": pm10, "pm10_grade": grade(pm10, [30, 80, 150]),
            "pm2_5": pm25, "pm2_5_grade": grade(pm25, [15, 35, 75])}
    summary = (f"{city} {day_word(offset)}({day}) 미세먼지 {data['pm10_grade']}({pm10}㎍/㎥), "
               f"초미세먼지 {data['pm2_5_grade']}({pm25}㎍/㎥). 하루 평균 예보값.")
    return ToolResult("get_air_quality", True, summary, data)


_holiday_cache = {}


def holidays_of(year):
    if year not in _holiday_cache:
        r = httpx.get(f"https://date.nager.at/api/v3/PublicHolidays/{year}/KR", timeout=TIMEOUT)
        r.raise_for_status()
        _holiday_cache[year] = [(date.fromisoformat(h["date"]), h["localName"]) for h in r.json()]
    return _holiday_cache[year]


def get_holidays(args, ctx):
    today = when.now_in(ctx.timezone).date()
    try:
        start = date.fromisoformat(args.get("from") or today.isoformat())
        end = date.fromisoformat(args.get("to") or (today + timedelta(days=365)).isoformat())
    except ValueError:
        start, end = today, today + timedelta(days=365)
    found = []
    for year in range(start.year, end.year + 1):
        found += [(d, n) for d, n in holidays_of(year) if start <= d <= end]
    data = {"from": start.isoformat(), "to": end.isoformat(),
            "holidays": [{"date": d.isoformat(), "name": n} for d, n in found]}
    if not found:
        return ToolResult("get_holidays", True, f"{start}부터 {end}까지 공휴일이 없습니다.", data)
    listed = ", ".join(f"{when.date_label(d)} {n}" for d, n in found[:6])
    more = f" 외 {len(found) - 6}일" if len(found) > 6 else ""
    return ToolResult("get_holidays", True, f"{start}~{end} 공휴일: {listed}{more}.", data)


# ── 이동 시간 도구(카카오맵 REST API) ──────────────────────────────
# 장소 이름을 키워드 검색으로 좌표로 바꾼 뒤 대중교통 경로를 묻는다. 출발 시각은 API에 넣을 수 없어서,
# 걸리는 시간만 받아 출발 시각(말하지 않았으면 지금)에 더해 도착 시각을 계산한다.
# 카카오 안내(데브톡 API FAQ)는 실시간 호출만 허용하고 결과를 따로 저장해 쓰는 것을 막는다. 그래서 캐시 없이 물을 때마다 부른다.

KAKAO = "https://dapi.kakao.com"
# 응답에 노선 색이 없어서 수도권 지하철 노선 색과 버스 종류 색을 직접 적어 둔다
LINE_COLORS = {
    "1호선": "#0052a4", "2호선": "#00a84d", "3호선": "#ef7c1c", "4호선": "#00a5de", "5호선": "#996cac",
    "6호선": "#cd7c2f", "7호선": "#747f00", "8호선": "#e6186c", "9호선": "#bdb092", "신분당": "#d4003b",
    "경의중앙": "#77c4a3", "공항": "#0090d2", "수인분당": "#fabe00", "신림": "#6789ca", "우이신설": "#b0ce18",
}
BUS_COLORS = {"간선": "#0068b7", "지선": "#53b332", "광역": "#e60012", "순환": "#f2b70a", "마을": "#53b332", "직행": "#e60012"}


def kakao_key():
    return os.environ.get("KAKAO_REST_KEY", "").strip()


def kakao(path, params):
    r = httpx.get(KAKAO + path, params=params, timeout=TIMEOUT, headers={"Authorization": f"KakaoAK {kakao_key()}"})
    r.raise_for_status()
    return r.json()


def find_place(name):
    """장소 이름 → 키워드 검색의 첫 결과(place_name, x=경도, y=위도). 못 찾으면 None."""
    docs = kakao("/v2/local/search/keyword.json", {"query": name, "size": 1}).get("documents") or []
    return docs[0] if docs else None


def leg_of(step):
    """경로의 한 구간 → 화면 카드에 그릴 {mode, name, minutes, color}."""
    p = step.get("properties") or {}
    minutes = max(1, round((p.get("time") or 0) / 60))
    if p.get("type") == "WALKING":
        return {"mode": "walk", "name": "도보", "minutes": minutes}
    vehicle = (p.get("vehicles") or [{}])[0]
    name, kind = str(vehicle.get("name") or ""), str(vehicle.get("type") or "")
    if p.get("type") == "SUBWAY":
        color = next((c for line, c in LINE_COLORS.items() if line in name), "#5d6478")
        return {"mode": "subway", "name": name or "지하철", "minutes": minutes, "color": color}
    return {"mode": "bus", "name": f"{kind} {name}".strip() or "버스", "minutes": minutes, "color": BUS_COLORS.get(kind, "#3d7bd9")}


def read_clock(value, now):
    """모델이 준 ISO 시각이나 'HH:MM'을 사용자 시간대 시각으로 바꾼다. 없거나 못 읽으면 None."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if len(text) <= 5 and ":" in text:
            hour, minute = (int(x) for x in text.split(":"))
            return now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        t = datetime.fromisoformat(text)
    except ValueError:
        return None
    return t.replace(tzinfo=now.tzinfo) if t.tzinfo is None else t.astimezone(now.tzinfo)


def get_route(args, ctx):
    if not kakao_key():
        return ToolResult("get_route", False, "이동 시간 도구가 아직 연결되지 않았습니다(지도 API 키 없음). 걸리는 시간은 짐작하지 말고 모른다고 해야 합니다.")
    origin, dest = (" ".join(str(args.get(k) or "").split())[:40] for k in ("origin", "destination"))
    if not origin or not dest:
        missing = "출발지" if not origin else "도착지"
        return ToolResult("get_route", False, f"{missing}를 몰라 이동 시간을 보지 못했습니다. 어디서 어디로 가는지 물어봐야 합니다.")
    places = {}
    for name in (origin, dest):
        places[name] = find_place(name)
        if not places[name]:
            return ToolResult("get_route", False, f"'{name}' 장소를 지도에서 찾지 못했습니다. 정확한 역이나 장소 이름을 물어봐야 합니다.")
    a, b = places[origin], places[dest]
    data = kakao("/v2/routing/publictraffic", {"start_x": a["x"], "start_y": a["y"], "end_x": b["x"], "end_y": b["y"],
                                               "s_name": a["place_name"], "e_name": b["place_name"]})
    routes = data.get("routes") or []
    if data.get("status") != "OK" or not routes:
        why = {"EQUAL_POINTS": "출발지와 도착지가 같습니다", "NO_RESULTS": "대중교통 경로가 없습니다(걸어갈 만큼 가까울 수 있음)"}
        return ToolResult("get_route", False, f"{a['place_name']} → {b['place_name']}: {why.get(data.get('status'), '대중교통 경로를 찾지 못했습니다')}.")

    best = min(routes, key=lambda r: (r.get("properties") or {}).get("totalTime") or 10 ** 9)
    prop = best["properties"]
    minutes = round(prop["totalTime"] / 60)
    transfers = prop.get("transfers") or 0
    legs = [leg_of(s) for s in best.get("steps") or []]
    rides = " → ".join(leg["name"] for leg in legs if leg["mode"] != "walk")
    now = when.now_in(ctx.timezone)
    depart = read_clock(args.get("depart_at"), now) or now
    arrive = depart + timedelta(minutes=minutes)
    summary = (f"{a['place_name']} → {b['place_name']} 대중교통 약 {minutes}분(환승 {transfers}번{', ' + rides if rides else ''}). "
               f"{depart:%H:%M}에 출발하면 {arrive:%H:%M}쯤 도착.")
    deadline = read_clock(args.get("arrive_by"), now)
    if deadline:
        spare = round((deadline - arrive).total_seconds() / 60)
        summary += f" {deadline:%H:%M}까지는 " + (f"약 {spare}분 여유." if spare >= 0 else f"약 {-spare}분 늦음.")
    summary += " 카카오맵 예상 시간이라 배차 간격이나 막차는 반영되지 않았을 수 있음."
    fare = (prop.get("fare") or {}).get("value")
    card = {"type": "route", "from": a["place_name"], "to": b["place_name"], "minutes": minutes,
            "summary": f"환승 {transfers}번" + (f" · {fare:,}원" if fare else ""), "legs": legs,
            "source": f"카카오맵 · {depart:%H:%M} 출발 기준 예상"}
    return ToolResult("get_route", True, summary, {"card": card, "depart": depart.isoformat(), "arrive": arrive.isoformat()})


# ── 약속 도구 ──────────────────────────────────────────────────────

def create_reminder(args, ctx):
    now = when.now_in(ctx.timezone)
    text = " ".join(str(args.get("text") or "").split())[:100]
    if not text:
        return ToolResult("create_reminder", False, "무엇을 알려 줄지 몰라 약속을 저장하지 않았습니다.")
    due, error = when.check_due(args.get("due_at"), now)
    if error:
        return ToolResult("create_reminder", False, f"약속을 저장하지 않았습니다: {error}")
    reminder = ctx.store.add_reminder(ctx.user_id, text, due)
    local = due.astimezone(now.tzinfo)
    data = {"id": reminder["id"], "text": text, "due_at": local.isoformat(), "duplicate": bool(reminder.get("duplicate"))}
    if data["duplicate"]:
        return ToolResult("create_reminder", True, f"같은 약속이 이미 있어서 새로 저장하지 않았습니다: {when.label(local)}에 '{text}'.", data)
    return ToolResult("create_reminder", True, f"약속을 저장했습니다: {when.label(local)}에 '{text}'.", data)


def list_reminders(args, ctx):
    now = when.now_in(ctx.timezone)
    items = ctx.store.list_reminders(ctx.user_id, status="pending")
    if not items:
        return ToolResult("list_reminders", True, "저장된 약속이 없습니다.", {"reminders": []})
    shown = [f"{when.label(r['due_at'].astimezone(now.tzinfo))} '{r['text']}'" for r in items[:5]]
    return ToolResult("list_reminders", True, "저장된 약속: " + ", ".join(shown) + ".",
                      {"reminders": [{"id": r["id"], "text": r["text"], "due_at": r["due_at"].isoformat()} for r in items]})


def cancel_reminder(args, ctx):
    query = str(args.get("query") or "").strip()
    items = ctx.store.list_reminders(ctx.user_id, status="pending")
    match = next((r for r in items if query and (query in r["text"] or r["text"] in query)), None)
    if not match and len(items) == 1:
        match = items[0]
    if not match:
        return ToolResult("cancel_reminder", False, f"'{query}'에 해당하는 약속을 찾지 못했습니다.")
    ctx.store.cancel_reminder(ctx.user_id, match["id"])
    return ToolResult("cancel_reminder", True, f"약속을 취소했습니다: '{match['text']}'.", {"id": match["id"]})


TOOLS = {
    "get_weather": {
        "run": get_weather, "label": "날씨",
        "desc": "지역의 오늘~6일 뒤 날씨, 기온, 강수 확률. 우산, 옷차림 질문도 여기에 해당",
        "args": '{"location": "도시 이름", "date": "YYYY-MM-DD"}',
    },
    "get_air_quality": {
        "run": get_air_quality, "label": "미세먼지",
        "desc": "지역의 오늘~4일 뒤 미세먼지, 초미세먼지 예보. 마스크, 환기, 야외 운동 질문도 여기에 해당",
        "args": '{"location": "도시 이름", "date": "YYYY-MM-DD"}',
    },
    "get_holidays": {
        "run": get_holidays, "label": "공휴일",
        "desc": "기간 안의 한국 공휴일. 연휴, 빨간 날 질문",
        "args": '{"from": "YYYY-MM-DD", "to": "YYYY-MM-DD"}',
    },
    "get_route": {
        "run": get_route, "label": "이동 시간",
        "desc": "두 장소 사이를 대중교통(지하철, 버스)으로 가는 시간과 경로. '몇 분 걸려', '몇 시까지 갈 수 있어' 질문",
        "args": '{"origin": "출발 장소 이름", "destination": "도착 장소 이름", "depart_at": "출발 ISO 시각(말했을 때만)", "arrive_by": "도착해야 하는 ISO 시각(말했을 때만)"}',
    },
    "create_reminder": {
        "run": create_reminder, "label": "약속 저장",
        "desc": "사용자가 나중에 알려 달라고 한 일을 시각과 함께 저장",
        "args": '{"text": "알려 줄 내용", "due_at": "ISO 8601 시각, 사용자 시간대 오프셋 포함"}',
    },
    "list_reminders": {
        "run": list_reminders, "label": "약속 보기",
        "desc": "저장된 약속 목록을 확인",
        "args": "{}",
    },
    "cancel_reminder": {
        "run": cancel_reminder, "label": "약속 취소",
        "desc": "저장된 약속을 취소",
        "args": '{"query": "취소할 약속 내용의 일부"}',
    },
}


def run_tool(name, args, ctx):
    try:
        return TOOLS[name]["run"](args, ctx)
    except httpx.HTTPError as error:
        log("WARNING", "외부 도구 호출 실패", tool=name, error=type(error).__name__)
        return ToolResult(name, False, f"{TOOLS[name]['label']} 정보를 지금은 불러오지 못했습니다.")
    except (KeyError, TypeError, ValueError) as error:  # 외부 API 응답 형식이 예상과 다를 때 대화 전체가 멈추지 않게
        log("WARNING", "외부 도구 응답 형식 오류", tool=name, error=type(error).__name__)
        return ToolResult(name, False, f"{TOOLS[name]['label']} 정보를 읽지 못했습니다.")
