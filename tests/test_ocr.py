"""사진 글자 읽기(/api/ocr).

대부분은 가짜 엔진을 끼워 모델 파일 없이 돈다(CI 에는 모델 파일이 없다).
모델 파일이 있으면(OCR_MODEL_DIR, README 「단계별로 켜는 기능」) 일정표 사진을 진짜로 읽어 보는 테스트도 돈다.
"""
import asyncio
import io
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import main, ocr
from app.config import ROOT

client = TestClient(main.app)


def png(width=40, height=20):
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buffer, format="PNG")
    return buffer.getvalue()


class FakeEngine:
    """RapidOCR 대신. 받은 사진을 모아 두고, 정해 둔 글 덩어리(txts)를 돌려준다."""

    def __init__(self, txts):
        self.txts = txts
        self.images = []

    def __call__(self, image):
        self.images.append(image)
        return SimpleNamespace(txts=self.txts)


@pytest.fixture
def fake_engine(monkeypatch):
    def use(txts):
        engine = FakeEngine(txts)
        monkeypatch.setattr(ocr, "engine", lambda: engine)
        return engine
    return use


@pytest.fixture
def ocr_on(monkeypatch):
    monkeypatch.setattr(main.settings, "ocr_enabled", True)
    # 테스트마다 새 자물쇠. asyncio 자물쇠는 처음 기다린 이벤트 루프에 묶이는데, 테스트는 루프를 새로 만든다
    monkeypatch.setattr(main, "ocr_turn", asyncio.Lock())


def test_lines_are_trimmed_and_blank_lines_dropped(fake_engine):
    fake_engine([" 10월 21일 수요일 19:00 ", "", "  ", "정기 모임"])
    assert ocr.read_text(png()) == "10월 21일 수요일 19:00\n정기 모임"


def test_no_text_found_gives_empty_text(fake_engine):
    fake_engine(None)  # RapidOCR 는 글자를 못 찾으면 txts 가 None
    assert ocr.read_text(png()) == ""


def test_photo_over_5mb_is_refused(fake_engine):
    fake_engine(["x"])
    with pytest.raises(ocr.OcrError, match="사진이 5MB보다 큽니다"):
        ocr.read_text(b"0" * (ocr.MAX_BYTES + 1))


def test_not_a_photo_is_refused(fake_engine):
    fake_engine(["x"])
    with pytest.raises(ocr.OcrError, match="사진 파일을 읽을 수 없습니다"):
        ocr.read_text(b"not a photo")


def test_sideways_phone_photo_is_turned_upright(fake_engine):
    # 휴대폰이 옆으로 누운 그대로 저장하고 「90도 돌려서 볼 것」(EXIF Orientation=6)만 적어 둔 사진
    exif = Image.Exif()
    exif[0x0112] = 6
    buffer = io.BytesIO()
    Image.new("RGB", (40, 20), "white").save(buffer, format="JPEG", exif=exif)
    engine = fake_engine(["x"])
    ocr.read_text(buffer.getvalue())
    assert engine.images[0].size == (20, 40)


@pytest.mark.parametrize("fmt", ["PNG", "JPEG"])
def test_big_photo_is_shrunk_to_max_side(fake_engine, fmt):
    buffer = io.BytesIO()
    Image.new("RGB", (4032, 3024), "white").save(buffer, format=fmt)
    engine = fake_engine(["x"])
    ocr.read_text(buffer.getvalue())
    assert engine.images[0].size == (ocr.MAX_SIDE, ocr.MAX_SIDE * 3 // 4)


def test_small_photo_is_not_enlarged(fake_engine):
    engine = fake_engine(["x"])
    ocr.read_text(png(1200, 830))
    assert engine.images[0].size == (1200, 830)


def test_missing_model_files_are_reported_without_downloading(monkeypatch, tmp_path):
    monkeypatch.setenv("OCR_MODEL_DIR", str(tmp_path))  # 빈 폴더
    ocr.engine.cache_clear()
    try:
        with pytest.raises(ocr.OcrError, match="사진에서 글자를 읽는 프로그램이 서버에 없습니다"):
            ocr.read_text(png())
    finally:
        ocr.engine.cache_clear()


def test_api_returns_text(ocr_on, fake_engine):
    fake_engine(["내일 오후 3시 치과"])
    r = client.post("/api/ocr", files={"photo": ("a.png", png(), "image/png")})
    assert r.status_code == 200 and r.json() == {"text": "내일 오후 3시 치과"}


def test_api_keeps_error_message(ocr_on, fake_engine):
    fake_engine(["x"])
    r = client.post("/api/ocr", files={"photo": ("a.png", b"not a photo", "image/png")})
    assert r.status_code == 500 and r.json()["detail"] == "사진 파일을 읽을 수 없습니다."


async def post_photos(count):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as c:
        replies = await asyncio.gather(*[c.post("/api/ocr", files={"photo": ("a.png", b"x", "image/png")})
                                         for _ in range(count)])
    return [r.json() for r in replies]


def test_reading_a_photo_does_not_stop_other_requests(ocr_on, monkeypatch):
    def slow_read(data):
        time.sleep(0.5)  # 몇 초 걸리는 글자 읽기 대신
        return "글"
    monkeypatch.setattr(ocr, "read_text", slow_read)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as c:
            started = time.perf_counter()
            photo = asyncio.create_task(c.post("/api/ocr", files={"photo": ("a.png", b"x", "image/png")}))
            await asyncio.sleep(0.1)  # 사진 읽기가 시작된 뒤에 /health 를 부른다
            health = await c.get("/health")
            health_done = time.perf_counter() - started
            return health.status_code, health_done, (await photo).json()

    status, health_done, photo = asyncio.run(run())
    # 사진 읽기가 서버를 막으면 /health 는 사진 읽기가 끝난 0.5초 뒤에야 끝난다. 안 막으면 0.1초쯤
    assert status == 200 and health_done < 0.4 and photo == {"text": "글"}


def test_photos_are_read_one_at_a_time(ocr_on, monkeypatch):
    running, most = 0, 0
    guard = threading.Lock()

    def slow_read(data):
        nonlocal running, most
        with guard:
            running += 1
            most = max(most, running)
        time.sleep(0.2)
        with guard:
            running -= 1
        return "글"
    monkeypatch.setattr(ocr, "read_text", slow_read)

    assert asyncio.run(post_photos(3)) == [{"text": "글"}] * 3
    assert most == 1


def test_reads_sample_schedule_with_real_models():
    try:
        ocr.engine()
    except ocr.OcrError:
        pytest.skip("모델 파일이 없다(OCR_MODEL_DIR). README 대로 받으면 돈다")
    text = ocr.read_text((ROOT / "data" / "samples" / "schedule.png").read_bytes())
    for line in ["산책 동아리 10월 일정", "10월 21일 수요일 19:00", "회비 납부 마감", "10월 25일 일요일 10:00"]:
        assert line in text.splitlines()
