"""사진 속 글자 읽기(OCR). 일정표나 공지 사진을 보내면 글자를 읽어 대화 입력창에 넣어 준다.

RapidOCR(onnxruntime)로 PaddleOCR 공개 모델 두 개를 차례로 돌린다(둘 다 Apache-2.0).
1. 글자 덩어리 위치 찾기: PP-OCRv6_det_small
2. 한국어 글자 인식: korean_PP-OCRv5_rec_mobile (한글 · 숫자 · 영어)
RapidOCR 기본값인 「뒤집힌 줄 판별」(중국어로 학습한 cls 모델)은 끈다. 바르게 찍은 한국어 줄을 뒤집혔다고 보고
거꾸로 돌려 읽는 일이 잦았다(저장소 일정표 사진 9줄 중 4줄). 대신 거꾸로(180도) 든 사진은 읽지 못한다.

모델 파일(합쳐서 약 23MB)은 서버가 실행 중에 내려받지 않는다. 서버는 이미지를 만들 때 Dockerfile 이
/opt/ocr-models 에 넣어 두고, 노트북에서는 README 대로 같은 파일을 받아 OCR_MODEL_DIR 로 폴더를 알려 준다.
파일이 없으면 내려받으러 가지 않고 바로 오류를 낸다.
"""
import io
import os
import time
from functools import lru_cache
from pathlib import Path

from app.logs import log

MAX_BYTES = 5 * 1024 * 1024  # 사진 한 장 최대 5MB
# 긴 변을 이 크기로 줄여 읽는다. 폰 사진(4032x3024)을 그대로 읽으면 CPU 1개에서 약 8초 · 최대 약 840MB,
# 1600 으로 줄이면 약 5초 · 약 575MB 였다(로컬 Docker, 시험 그림 한 장). 대신 띄어쓰기가 몇 곳 빠질 수 있다.
MAX_SIDE = 1600
# RapidOCR 설정 이름(Det 위치 찾기 · Rec 인식)마다 쓸 모델 파일. 파일 이름과 내려받을 주소는 Dockerfile 과 맞춘다.
MODEL_FILES = {
    "Det": "PP-OCRv6_det_small.onnx",
    "Rec": "korean_PP-OCRv5_rec_mobile.onnx",
}


class OcrError(Exception):
    pass


@lru_cache(maxsize=1)
def engine():
    """모델을 처음 쓸 때 한 번만 불러와 계속 쓴다. 파일이 없으면 오류를 내고, 다음 요청 때 다시 찾아본다.
    폴더는 여기서 읽는다. 노트북의 .env 는 이 파일을 불러온 뒤에 읽히기 때문이다."""
    model_dir = Path(os.getenv("OCR_MODEL_DIR", "/opt/ocr-models"))
    missing = [name for name in MODEL_FILES.values() if not (model_dir / name).is_file()]
    if missing:
        log("ERROR", "글자 읽기 모델 파일이 없습니다.", model_dir=str(model_dir), missing=missing)
        raise OcrError("사진에서 글자를 읽는 프로그램이 서버에 없습니다.")  # tesseract 를 쓰던 때와 같은 문구
    from rapidocr import RapidOCR

    # model_path 를 주면 RapidOCR 는 그 파일을 그대로 쓰고 내려받지 않는다. 글자 목록은 모델 파일 안에 들어 있다.
    params = {f"{part}.model_path": str(model_dir / name) for part, name in MODEL_FILES.items()}
    return RapidOCR(params={"Global.log_level": "warning", "Global.use_cls": False, **params})


def read_text(data: bytes) -> str:
    from PIL import Image, ImageOps, UnidentifiedImageError

    if len(data) > MAX_BYTES:
        raise OcrError("사진이 5MB보다 큽니다.")
    try:
        image = Image.open(io.BytesIO(data))
        scale = MAX_SIDE / max(image.size)
        if scale < 1:
            # JPEG 는 풀 때부터 1/2 · 1/4 · 1/8 크기로 풀 수 있다. 아주 큰 사진도 메모리를 적게 쓴다(다른 형식은 그대로)
            image.draft("RGB", (round(image.width * scale), round(image.height * scale)))
        # 휴대폰 사진은 돌려 찍은 방향을 사진 정보(EXIF)로만 적어 두는 경우가 있다. 그 방향대로 바로 세운다.
        image = ImageOps.exif_transpose(image).convert("RGB")
        image.thumbnail((MAX_SIDE, MAX_SIDE))
    except (UnidentifiedImageError, OSError) as error:
        raise OcrError("사진 파일을 읽을 수 없습니다.") from error
    started = time.perf_counter()
    result = engine()(image)
    # 글자 덩어리 하나가 한 줄이다. RapidOCR 는 위에서 아래, 왼쪽에서 오른쪽 순서로 돌려준다.
    lines = result.txts or ()
    text = "\n".join(line.strip() for line in lines if line.strip())
    # 글 내용은 개인정보라 남기지 않는다. 글자 수 · 줄 수 · 걸린 시간만
    log("INFO", "사진 글자 읽기", chars=len(text), lines=text.count("\n") + 1 if text else 0,
        seconds=round(time.perf_counter() - started, 2))
    return text
