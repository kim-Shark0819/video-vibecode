"""vpoc2 설정. os.getenv 는 이 파일에만 둔다."""
from __future__ import annotations

import os


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return float(raw) if raw not in (None, "") else default


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw not in (None, "") else default


ENABLED = _bool("VPOC2_ENABLED", False)
# 목 모드: 모델을 부르지 않고 가짜 결과를 만든다. 로컬 개발·테스트 전용.
MOCK = _bool("VPOC2_MOCK", False)
RAW_ERRORS = _bool("VPOC2_RAW_ERRORS", True)

# 저장소. 목 모드에서 LOCAL_DIR 이 있으면 S3 대신 로컬 폴더를 쓴다.
MAIN_BUCKET = os.getenv("VPOC2_MAIN_BUCKET") or os.getenv("S3_BUCKET", "")
MAIN_REGION = os.getenv("VPOC2_MAIN_REGION") or os.getenv("AWS_REGION", "ap-northeast-2")
# 영상 버킷은 vpoc1 과 같은 버킷(VPOC_VIDEO_BUCKET)을 쓴다. 접두사가 달라 섞이지 않는다.
VIDEO_BUCKET = os.getenv("VPOC2_VIDEO_BUCKET") or os.getenv("VPOC_VIDEO_BUCKET", "")
LOCAL_DIR = os.getenv("VPOC2_LOCAL_DIR", "")

# 모델 (PRD 5.0 실측값. 조용히 바꾸지 않는다)
TEXT_MODEL = os.getenv("VPOC2_TEXT_MODEL", "global.anthropic.claude-sonnet-4-5-20250929-v1:0")
TEXT_REGION = os.getenv("VPOC2_TEXT_REGION", "ap-northeast-2")
IMAGE_MODEL = os.getenv("VPOC2_IMAGE_MODEL", "stability.sd3-5-large-v1:0")
IMAGE_REGION = os.getenv("VPOC2_IMAGE_REGION", "us-west-2")
VIDEO_MODEL = os.getenv("VPOC2_VIDEO_MODEL", "luma.ray-v2:0")
VIDEO_REGION = os.getenv("VPOC2_VIDEO_REGION", "us-west-2")

# 영상 규격 (설계자 결정 2026-09-30: 15초 = 5초 × 3샷, 테스트 540p, 720p 월 1편)
SHOTS = 3
SHOT_SECONDS = 5
ASPECT = "16:9"
DEFAULT_RESOLUTION = "540p"
HQ_RESOLUTION = "720p"
HQ_MONTHLY_LIMIT = _int("VPOC2_HQ_MONTHLY_LIMIT", 1)
MAX_INFLIGHT = _int("VPOC2_MAX_INFLIGHT", 1)
MAX_PROMPT_CHARS = 1200

# 단가 (USD)
USD_PER_SEC = {"540p": 0.75, "720p": 1.50}
USD_PER_IMAGE = _float("VPOC2_USD_PER_IMAGE", 0.08)
TEXT_USD_PER_MTOK_IN = _float("VPOC2_TEXT_USD_PER_MTOK_IN", 3.0)
TEXT_USD_PER_MTOK_OUT = _float("VPOC2_TEXT_USD_PER_MTOK_OUT", 15.0)

# 작업·조회 주기
TASK_STALE_SECONDS = 600
POLL_MIN_SECONDS = 10
PRESIGN_SECONDS = 3600
# 목 모드에서 샷 하나가 끝나는 데 걸리는 시간
MOCK_SHOT_SECONDS = _float("VPOC2_MOCK_SHOT_SECONDS", 3.0)


def shot_usd(resolution: str) -> float:
    return round(USD_PER_SEC[resolution] * SHOT_SECONDS, 2)


def video_usd(resolution: str) -> float:
    return round(shot_usd(resolution) * SHOTS, 2)
