# 에이전트 서비스 실행 환경. 이 파일을 저장소 루트로 복사해 쓴다(Dockerfile).
# 1) 시작 이미지와 Python 버전을 고정한다.
FROM python:3.13-slim-bookworm

# 2) 운영체제 프로그램 설치: 약속 카드 그림에 쓰는 한글 글꼴(나눔)
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-nanum \
    && rm -rf /var/lib/apt/lists/*

# 3) 사진 글자 읽기 모델 파일(app/ocr.py). 서버가 실행 중에 내려받지 않도록 이미지에 넣는다.
#    주소와 지문(SHA-256)은 RapidOCR 3.10.0 의 모델 목록과 같다. 받은 파일의 지문이 다르면 빌드가 멈춘다.
#    파일은 관리자(root) 것이고 앱 사용자는 읽기만 한다(--chmod=644).
#    폴더를 먼저 만든다. ADD 가 폴더를 만들게 두면 폴더에도 644 가 붙어 앱 사용자가 폴더 안으로 들어가지 못한다.
RUN mkdir -p /opt/ocr-models
ADD --checksum=sha256:090f04abcd9d9a7498bc4ebf677e4cb9bdce1fe4197ddb7e529f1ef44e1ff94f --chmod=644 \
    https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.10.0/onnx/PP-OCRv6/det/PP-OCRv6_det_small.onnx /opt/ocr-models/
ADD --checksum=sha256:cd6e2ea50f6943ca7271eb8c56a877a5a90720b7047fe9c41a2e541a25773c9b --chmod=644 \
    https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.10.0/onnx/PP-OCRv5/rec/korean_PP-OCRv5_rec_mobile.onnx /opt/ocr-models/

WORKDIR /app

# 4) 라이브러리 설치. requirements.txt 만 먼저 복사하면 코드만 바뀐 빌드에서 이 단계를 다시 쓰지 않는다.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 5) 코드 복사. 올리지 않을 파일은 .dockerignore 에 적는다(.env 포함).
COPY . .

# 6) 관리자(root)가 아닌 사용자로 실행한다.
RUN useradd --create-home appuser
USER appuser

# 7) Cloud Run이 정해 주는 PORT 환경변수로 실행한다. 노트북에서는 기본값 8080.
#    exec 로 sh 대신 uvicorn 이 1번 프로세스가 되어, 끌 때 보내는 종료 신호(SIGTERM)를 직접 받고 정상 종료한다.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
