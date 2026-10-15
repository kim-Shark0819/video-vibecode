FROM python:3.13.7-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 10001 appuser

COPY src/ ./src/

# 이 이미지가 어떤 소스로 만들어졌는지 이미지 안에 굽는다 (F-23/F-24).
#
# ## 왜 env 나 ConfigMap 이 아니라 build-arg 인가
#
# 이 값의 목적은 "이 결과물을 만든 코드"를 특정하는 것이다. k8s env 로 넣으면 매니페스트를
# 고쳐서 값과 이미지를 어긋나게 만들 수 있다. 이미지에 구우면 분리되지 않는다.
# 프론트엔드가 ASSET_VERSION 을 app.js 에 심는 것과 같은 이유다.
#
# 이 파이프라인은 로컬 워킹 디렉터리를 배포하므로 SOURCE_REVISION 에 `-dirty` 가 붙는 경우가
# 많다. 그것을 감추지 않는다 — 커밋으로 되짚을 수 없는 결과라는 사실 자체가 기록해야 할 정보다.
ARG IMAGE_TAG=dev
ARG SOURCE_STAMP=unknown
ARG SOURCE_ARCHIVE=unknown
ARG SOURCE_GIT=unknown
ENV IMAGE_TAG=${IMAGE_TAG} \
    SOURCE_REVISION=${SOURCE_STAMP} \
    SOURCE_ARCHIVE=${SOURCE_ARCHIVE} \
    SOURCE_GIT=${SOURCE_GIT}

USER 10001
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)" || exit 1

CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "--threads", "4", "--timeout", "60", "--access-logfile", "-", "--error-logfile", "-", "src.server:app"]
