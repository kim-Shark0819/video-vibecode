"""vpoc1 설정값 — PRD 6.8. 전부 환경변수에서 읽는다(render.py → ConfigMap).

여기에만 os.getenv 를 둔다. 다른 모듈은 이 상수를 읽는다.
"""

from __future__ import annotations

import os


def _flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


AWS_REGION = os.getenv("AWS_REGION", "ap-northeast-2")

# 기능 스위치. VIDEO_GEN_ENABLED 가 꺼진 배포에서는 이 값과 무관하게 라우트가 없다.
VPOC_ENABLED = _flag("VPOC_ENABLED", "true")
RAW_ERRORS = _flag("VPOC_RAW_ERRORS", "true")
SMOKE_ENABLED = _flag("VPOC_SMOKE_ENABLED", "false")
# ④단계 "이 샷만 의견 반영해 다시 만들기". 끄면 새 액션 3개가 404 가 되고 화면 버튼도 사라진다.
# 문제가 생겼을 때 이 값만 꺼서 이전 동작으로 되돌린다(regen-plan G6).
REGEN_ENABLED = _flag("VPOC_REGEN_ENABLED", "true")

MAIN_BUCKET = os.getenv("S3_BUCKET", "")

TEXT_MODEL = os.getenv(
    "VPOC_TEXT_MODEL", "global.anthropic.claude-sonnet-4-5-20250929-v1:0"
)
TEXT_REGION = os.getenv("VPOC_TEXT_REGION", AWS_REGION)
# ② 분석(웹툰 이미지 읽기)은 이 계정에서 쓸 수 있는 가장 좋은 Claude 로 한다(설계자 2026-10-13).
# 2026-10-13 실측: 이 계정은 Opus 4.6 · Sonnet 4.6 · Sonnet 4.5 만 호출 가능(Opus 4.7+/5.x 는 미허용).
# 설계자 2026-10-14: 가장 분석이 좋은 모델을 쓴다. 같은 웹툰 장면 비교(2026-10-14): Fable 5.1 ≥ Opus 5.5
# >> GPT-6 Astra(이미지를 거의 읽지 못함). 와이낫 계정은 5.x 한도가 0이라 와이랩 계정에서 임시로 빌린다
# (BROKER_*). 빌리기가 안 되면 아래 예비 모델(와이낫 계정)로 자동으로 넘어간다.
ANALYZE_MODEL = os.getenv("VPOC_ANALYZE_MODEL", "global.anthropic.claude-fable-5-1")
# 샷마다 맡은 웹툰 장면을 보고 분위기 · 배경 · 인물별 행동 · 상황을 적는 모델(설계자 2026-10-13).
# Opus 5.5 는 이 계정에서 약관 동의 전이라(2026-10-13) 지금은 Opus 4.6. 동의 후 이 값만 바꾼다.
BRIEF_MODEL = os.getenv("VPOC_BRIEF_MODEL", "global.anthropic.claude-fable-5-1")
# 임시(2026-10-14): 와이낫 계정에서 아직 못 쓰는 모델은 와이랩 계정 역할을 빌려 부른다. 비우면 끈다.
# 와이낫 계정 한도가 열리면 VPOC_BEDROCK_BROKER_ROLE_ARN="" 로 끄고 와이랩 역할을 지운다.
BROKER_ROLE_ARN = os.getenv(
    # 임시로 와이랩 계정 역할을 빌린다(설계자 2026-10-14 승인). account-check: ignore
    "VPOC_BEDROCK_BROKER_ROLE_ARN", "arn:aws:iam::892278727066:role/whynot-bedrock-broker"  # account-check: ignore
).strip()
BROKER_EXTERNAL_ID = os.getenv("VPOC_BEDROCK_BROKER_EXTERNAL_ID", "whynot-vpoc-broker")
BROKER_REGION = os.getenv("VPOC_BEDROCK_BROKER_REGION", "ap-northeast-2")
BROKER_MODEL_MARKERS = ("claude-fable-", "claude-opus-5", "claude-sonnet-5", "gpt-6")
# Seedance 네이티브 오디오(설계자 2026-10-14 "영상에 소리를 넣는다"). 끄려면 VPOC_ARK_GENERATE_AUDIO=false.
ARK_GENERATE_AUDIO = _flag("VPOC_ARK_GENERATE_AUDIO", "true")
# 키 장면(콘티) 방식(설계자 2026-10-14): 5초마다 키 장면을 그리고 Seedance 시작 · 끝 장면으로 잇는다.
# Seedance 2.0/2.5 의 시작 · 끝 장면 모드는 4초 이상이라 3초 간격은 불가(실측). 기본 30초 = 키 장면 7장.
KEYFRAME_INTERVAL_S = _int("VPOC_KEYFRAME_INTERVAL_S", 5)
KEYFRAME_TOTAL_CHOICES = (20, 30, 40, 60)
KEYFRAME_TOTAL_DEFAULT = 30
# 영상 1개 한 번에(single) 방식(설계자 2026-10-19): 샷별로 나눠 만들지 않고, ② 에서 고른 길이(초)의
# 영상 하나만 만든다. 길이는 고른 모델이 한 번에 만들 수 있는 범위(VIDEO_DURATION_RANGE · fal caps)
# 안에서 고른다 — 20/30/40/60 고정 선택지는 없앴다. ③ 키 장면 콘티는 그대로 두고, 완성 키 장면을
# 참고/시작·끝 이미지로 쓴다. 키 장면 수 = clamp(ceil(초 / KEYFRAME_INTERVAL_S) + 1, 2, SINGLE_MAX_KEYFRAMES).
SINGLE_MIN_KEYFRAMES = 2
SINGLE_MAX_KEYFRAMES = 4
# 길이를 못 고른 옛/깨진 값일 때의 기본값은 min(SINGLE_DURATION_DEFAULT, 모델 최대). routes 가 계산한다.
SINGLE_DURATION_DEFAULT = _int("VPOC_SINGLE_DURATION_DEFAULT", 10)
SEEDREAM_ROUGH_MODEL = os.getenv("VPOC_SEEDREAM_ROUGH_MODEL", "dola-seedream-5-0-flash-260915")
SEEDREAM_FINAL_MODEL = os.getenv("VPOC_SEEDREAM_FINAL_MODEL", "dola-seedream-5-0-pro-260628")
SEEDREAM_USD = _float("VPOC_SEEDREAM_USD", 0.035)
# 한 샷에 Seedance 로 보내는 웹툰 장면 이미지 수 / 설명을 쓸 때 Claude 에 보여 주는 장면 수.
PANELS_PER_SHOT_REFS = _int("VPOC_PANELS_PER_SHOT_REFS", 3)
PANELS_PER_SHOT_BRIEF = _int("VPOC_PANELS_PER_SHOT_BRIEF", 6)
# 앞 모델이 실패(혼잡 · 시간 초과 · 미허용 · 도구 미사용)하면 차례로 바꿔 다시 부른다.
TEXT_FALLBACK_MODELS = [
    item.strip()
    for item in os.getenv(
        "VPOC_TEXT_FALLBACK_MODELS",
        "global.anthropic.claude-opus-5-5,global.anthropic.claude-opus-4-6-v1,"
        "global.anthropic.claude-sonnet-4-6,global.anthropic.claude-sonnet-4-5-20250929-v1:0",
    ).split(",")
    if item.strip()
]

IMAGE_MODEL = os.getenv("VPOC_IMAGE_MODEL", "stability.sd3-5-large-v1:0")
IMAGE_REGION = os.getenv("VPOC_IMAGE_REGION", "us-west-2")

VIDEO_MODEL = os.getenv("VPOC_VIDEO_MODEL", "luma.ray-v2:0")
VIDEO_REGION = os.getenv("VPOC_VIDEO_REGION", "us-west-2")
VIDEO_BUCKET = os.getenv("VPOC_VIDEO_BUCKET", "")

# ② 영상 모델 고르기(설계자 2026-10-07, dev 만). 꺼져 있으면 항상 Luma Ray2 다.
MODEL_PICKER_ENABLED = _flag("VPOC_MODEL_PICKER_ENABLED", "false")
# BytePlus ModelArk(Seedance). 키는 Secret 에서만 온다. 모델 ID 가 비면 그 모델은 고를 수 없다.
ARK_BASE_URL = os.getenv("ARK_BASE_URL", "https://ark.ap-southeast.bytepluses.com/api/v3").rstrip("/")
ARK_API_KEY = os.getenv("ARK_API_KEY", "")
# mini · fast 는 설계자 지시(2026-10-07)로 뺐다.
ARK_MODEL_IDS = {
    "seedance-2-0": os.getenv("VPOC_ARK_MODEL_2_0", "dreamina-seedance-2-0-260128"),
    "seedance-2-5": os.getenv("VPOC_ARK_MODEL_2_5", "dreamina-seedance-2-5-260628"),
}
ARK_RESOLUTION = os.getenv("VPOC_ARK_RESOLUTION", "720p")
# 생성 영상 1초당 USD (참고 영상 없이 첫 프레임만 보내는 우리 경로 기준).
#
# 출처: 설계자가 준 BytePlus 콘솔 견적 화면 7장(2026-10-07). 견적은 "참고 영상 길이 + 생성 15초"
# 합계다. 2.5·480P 에서 참고 5초 $1.54, 30초 $2.77 → 참고 1초 $0.0492, 생성 1초 $0.0863,
# 참고/생성 비 0.57. 같은 비를 다른 칸에 적용해 생성 1초 = 견적 ÷ (15 + 5×0.57) = 견적 ÷ 17.85.
# 추정값이다 — 실제 청구(usage 토큰)와 다르면 이 표를 고친다.
ARK_USD_PER_SEC_TABLE = {
    "seedance-2-0": {"480p": 0.0605, "720p": 0.1305, "1080p": 0.3221, "4k": 0.6532},
    "seedance-2-5": {"480p": 0.0863, "720p": 0.1938},
}
# 표를 덮어쓸 때만 쓴다(현재 해상도의 1초 단가). 비면 표 값.
ARK_USD_PER_SEC = {
    "seedance-2-0": os.getenv("VPOC_ARK_USD_PER_SEC_2_0", ""),
    "seedance-2-5": os.getenv("VPOC_ARK_USD_PER_SEC_2_5", ""),
}

# fal.ai — 키 하나로 수백 개의 영상 모델(Kling · MiniMax Hailuo · Wan · Veo …)을 큐 API 로 부른다
# (설계자 2026-10-18). Seedance(ark.py)와 같은 "외부 영상 공급자" 경로를 탄다. 키는 Secret 에서만
# 온다(FAL_KEY). 사용자가 ② 화면에서 "fal 에서 모델 가져오기"로 등록한 모델만 ② 목록에 나타난다.
FAL_KEY = os.getenv("FAL_KEY", "")
# 큐 API(제출·조회)와 모델 카탈로그/스키마 API 는 호스트가 다르다. 둘 다 env 로 덮어쓸 수 있다.
FAL_QUEUE_URL = os.getenv("FAL_QUEUE_URL", "https://queue.fal.run").rstrip("/")
FAL_API_URL = os.getenv("FAL_API_URL", "https://api.fal.ai/v1").rstrip("/")
# 모델 스키마 공개 주소(인증 불필요). `/v1/models?expand=openapi-3.0` 이 실제로는
# `{"error":{"code":"expansion_failed"}}` 를 돌려주므로(실측 2026-10-18), 그때 이 주소로 대체한다.
# `?endpoint_id=<id>` 를 붙여 GET 하면 정상 OpenAPI 3 를 준다.
FAL_OPENAPI_URL = os.getenv("FAL_OPENAPI_URL", "https://fal.ai/api/openapi/queue/openapi.json").rstrip("/")
# fal 모델 ID 형식: "fal:<endpoint_id>" (예: "fal:fal-ai/kling-video/v3/pro/image-to-video").
# ARK 모델 ID("seedance-2-0" 등)와 겹치지 않게 접두사로 가른다.
FAL_ID_PREFIX = "fal:"
# 영상 단계에서 실제로 쓸 수 있는 fal 카테고리. 나머지는 카탈로그 목록에만 보이고 등록은 거절된다.
FAL_VIDEO_CATEGORIES = ("text-to-video", "image-to-video")
# 카탈로그 조회에서 함께 보여 주는(목록만, 영상 단계 사용 불가) 카테고리.
FAL_BROWSE_CATEGORIES = (
    "text-to-video",
    "image-to-video",
    "video-to-video",
    "image-to-3d",
    "text-to-3d",
)


def is_fal(model: str) -> bool:
    """fal 모델 ID("fal:<endpoint_id>") 인지."""
    return str(model or "").startswith(FAL_ID_PREFIX)


def fal_endpoint_id(model: str) -> str:
    """fal 모델 ID 에서 endpoint_id 만 떼어 낸다. fal 이 아니면 빈 문자열."""
    return str(model or "")[len(FAL_ID_PREFIX):] if is_fal(model) else ""

# ③ 영상 설정 — 사용자가 화질과 샷 길이를 고른다(설계자 2026-10-07).
# 샷 길이 상한: Seedance 2.0 15초 · 2.5 15초 · Luma Ray2 5초. 하한은 4초.
# 2.0 은 처음에 30초로 두었으나 BytePlus 가 16초 이상을 "duration not valid"(t2v · r2v 모두)로
# 거절했다(운영 2026-10-13, 20초 한 번에 이어 만들기 실패 → 실측 4~15초 통과, 16·20·30초 거절).
VIDEO_RESOLUTIONS = {
    "luma-ray2": ["540p", "720p"],
    "seedance-2-0": ["480p", "720p", "1080p"],
    "seedance-2-5": ["480p", "720p"],
}
VIDEO_DURATION_RANGE = {
    "luma-ray2": (5, 5),
    "seedance-2-0": (4, 15),
    "seedance-2-5": (4, 15),
}

RESOLUTION = os.getenv("VPOC_RESOLUTION", "540p")
DURATION = os.getenv("VPOC_DURATION", "5s")
ASPECT = os.getenv("VPOC_ASPECT", "16:9")
DURATION_S = _int("VPOC_DURATION_S", 5)

# 실측(2026-09-23 C8): "On-demand model inference concurrent requests for Luma Ray V2" = 1.
# 쿼터가 1이라 동시 제출도 1이다. 2 이상을 넣으면 ThrottlingException 이 난다.
MAX_INFLIGHT = max(1, _int("VPOC_MAX_INFLIGHT", 1))
# Seedance(BytePlus) 동시 제출 수. 설계자 2026-10-13: 샷은 1번부터 **하나씩 순서대로** 만든다
# (동시 생성 아님). 값을 올리면 동시에 보내고, 한도에 걸리면(HTTP429) 대기열로 돌린다.
ARK_MAX_INFLIGHT = max(1, _int("VPOC_ARK_MAX_INFLIGHT", 1))
MAX_SHOTS = _int("VPOC_MAX_SHOTS", 6)
MIN_SHOTS = 3

# 비용 상한은 없다(2026-09-23 사용자 결정). 기록만 하고 제출을 막지 않는다.
# 여기에 상한 상수를 다시 넣지 않는다 — 넣으면 ledger 에 검사가 따라 들어온다.
VIDEO_USD_PER_SEC = {"540p": 0.75, "720p": 1.5}
IMAGE_USD = _float("VPOC_IMAGE_USD", 0.08)
TEXT_USD_PER_MTOK_IN = _float("VPOC_TEXT_USD_PER_MTOK_IN", 3.0)
TEXT_USD_PER_MTOK_OUT = _float("VPOC_TEXT_USD_PER_MTOK_OUT", 15.0)
# Opus 4.5 이후 단가(USD / 1M 토큰). 관리자 "모델 관리" 비용에 모델별로 계상한다.
OPUS_USD_PER_MTOK_IN = _float("VPOC_OPUS_USD_PER_MTOK_IN", 5.0)
OPUS_USD_PER_MTOK_OUT = _float("VPOC_OPUS_USD_PER_MTOK_OUT", 25.0)

# 업로드
MAX_UPLOAD_BYTES = _int("VPOC_MAX_UPLOAD_BYTES", 10 * 1024 * 1024)
MAX_ANALYSIS_IMAGES = 12
TILE_HEIGHT = 1568

# 모델 호출 시간 제한
READ_TIMEOUT_S = _int("VPOC_READ_TIMEOUT_S", 300)
# 분석이 모델을 바꿔 가며 다시 부를 수 있어(최대 수 분) 10분은 짧다(2026-10-13).
# 끊긴 작업을 "다시 실행" 으로 돌리는 시간. 가장 긴 작업(분석 재시도 · 콘티+러프)도 10분 안쪽이다.
TASK_STALL_S = _int("VPOC_TASK_STALL_S", 900)
# 완료를 빨리 알아채도록 5초(설계자 2026-10-13 "샷 1개 시간 줄이기"). 조회 자체는 무료다.
POLL_MIN_INTERVAL_S = 5


def ark_usd_per_sec(model: str, resolution: str = "") -> float | None:
    """그 해상도(기본 ARK_RESOLUTION)의 생성 1초 단가. 모르면 None("단가 미정")."""
    raw = str(ARK_USD_PER_SEC.get(model) or "").strip()
    if raw:
        try:
            return float(raw)
        except ValueError:
            return None
    return (ARK_USD_PER_SEC_TABLE.get(model) or {}).get((resolution or ARK_RESOLUTION).lower())


def default_resolution(model: str) -> str:
    """고르기 전의 화질. 예전 계획(설정 없음)은 이 값으로 읽힌다."""
    if model in ARK_MODEL_IDS:
        return ARK_RESOLUTION
    return RESOLUTION


def shot_usd(model: str = "", resolution: str = "", seconds: float | None = None) -> float:
    """샷 1개 금액. model 이 Seedance 이고 단가가 없으면 0(단가 미정).

    인자를 비우면 예전과 같은 값(Luma · VPOC_RESOLUTION · DURATION_S)이다.
    """
    secs = DURATION_S if seconds is None else float(seconds)
    if model in ARK_MODEL_IDS:
        per_sec = ark_usd_per_sec(model, resolution)
        return round((per_sec or 0.0) * secs, 4)
    return round(VIDEO_USD_PER_SEC.get(resolution or RESOLUTION, 0.75) * secs, 4)


def single_keyframe_count(total_s: int) -> int:
    """영상 1개(single) 방식의 키 장면 수(설계자 2026-10-19).

    clamp(ceil(초 / KEYFRAME_INTERVAL_S) + 1, SINGLE_MIN_KEYFRAMES, SINGLE_MAX_KEYFRAMES).
    """
    import math

    seconds = max(1, int(total_s or 1))
    raw = math.ceil(seconds / max(1, KEYFRAME_INTERVAL_S)) + 1
    return max(SINGLE_MIN_KEYFRAMES, min(SINGLE_MAX_KEYFRAMES, raw))


def single_interval_s(total_s: int) -> float:
    """키 장면 사이 간격(초, 실수). 간격 = 초 / (키 장면 수 - 1)."""
    count = single_keyframe_count(total_s)
    return float(total_s) / max(1, count - 1)


def single_default_duration(low: int, high: int) -> int:
    """고르기 전/범위 밖 값일 때 쓸 길이. min(SINGLE_DURATION_DEFAULT, 최대)이되 최소 이상."""
    return max(int(low), min(int(SINGLE_DURATION_DEFAULT), int(high)))
