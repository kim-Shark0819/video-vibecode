import base64
import contextlib
import hashlib
import hmac
import json
import logging
import os
import subprocess
import re
import shutil
import tempfile
import threading
import time
import urllib.parse
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from flask import Flask, Response, g, jsonify, request
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename

from . import (
    account_guard,
    documents,
    gateway,
    graph_store,
    image_brief,
    image_validation,
    model_registry,
    pdf_export,
    prompt_boost,
    prompt_layers,
    project_tracks,
    prompts_story,
    providers,
    stitcher,
    story_context,
    story_models,
    story_router,
    styles,
    toon_convert,
    vpoc,
    vpoc2,
    video_prompt,
)

# vpoc1(2026-09-23): 구 영상 기능 모듈 묶음을 제거했다. 새 영상 기능은 `vpoc` 패키지
# 하나이고, 등록 지점은 이 파일 맨 아래 한 곳뿐이다. 백업 태그는
# backup/whynot-video-v4-20260923 이며 참조하지 않는다.

# 역할 모듈은 라우터를 통해 쓰는 것이 원칙이다(지시서 1절: 역할끼리 서로 부르지 않고
# 라우터만 부른다). 여기서 직접 쓰는 것은 두 가지 예외뿐이다.
#   _json     : 요약 생성처럼 역할 하나로 끝나는 호출. 라우터를 거칠 분배 판단이 없다.
#   vision    : safety_blocked() 조회. 모델을 부르지 않는 순수 함수다.
from .roles import _json as roles_json
from .roles import vision as roles_vision

SERVICE_NAME = "toon-change-ai-pj-app"
# 이 이미지가 어떤 소스로 만들어졌는지. Dockerfile 이 build-arg 로 굽는다 (F-23/F-24).
#
# **k8s env 로 받지 않는다.** 매니페스트를 고쳐 값과 이미지를 어긋나게 만들 수 있으면
# 이 값의 목적(결과물을 만든 코드를 특정)이 무너진다. 이미지에 구우면 분리되지 않는다.
# dirty 배포는 `<sha>-dirty` 로 남는다. 감추지 않는다 — 커밋으로 되짚을 수 없는 결과라는
# 사실 자체가 기록해야 할 정보다.
APP_IMAGE_TAG = os.getenv("IMAGE_TAG", "dev")
APP_SOURCE_REVISION = os.getenv("SOURCE_REVISION", "unknown")
# 아카이브의 S3 키. 스탬프가 가리키는 바이트를 실제로 되받을 수 있어야 재현이 완결된다.
APP_SOURCE_ARCHIVE = os.getenv("SOURCE_ARCHIVE", "unknown")
# 커밋 맥락(`<sha>` 또는 `<sha>+<미커밋건수>`). 기준값이 아니라 참고용이다.
APP_SOURCE_GIT = os.getenv("SOURCE_GIT", "unknown")

# manifest 저장 실패 횟수. **런타임 실패는 검사 스크립트가 못 잡는다.**
# check-manifest.py 는 배선만 본다. 카운터가 없으면 manifest 가 조용히 빈 채로 쌓이고,
# 그 사실을 몇 주 뒤 실험을 비교하려 할 때 알게 된다. /api/health 로 노출한다.
_manifest_failures = {"count": 0, "lastError": "", "lastAt": ""}
AWS_REGION = os.getenv("AWS_REGION", "ap-northeast-2")
S3_BUCKET = os.getenv("S3_BUCKET", "")
S3_VIDEO_BUCKET = os.getenv("S3_VIDEO_BUCKET", "")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
# 실제 값은 k8s Secret `toon-change-ai-pj-auth`의 `password`에서 주입된다.
# 코드 기본값을 약한 문자열로 두면 Secret 주입이 빠진 환경에서 그 값이 그대로 유효해진다.
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
DEFAULT_COMPANY_CODE = os.getenv("DEFAULT_COMPANY_CODE", "WHYNOT-2026")
# 시드 사용자는 배포 설정(config.env의 SEED_USERS)이 단일 원본이다.
# 코드에 기본 계정을 박아 두면 설정이 빠진 환경에 약한 계정이 조용히 생긴다.
SEED_USERS = os.getenv("SEED_USERS", "")
# 결과물 생성 한도. 형식: `사용자:video=10,asset=50` 이고 사용자끼리는 `;`로 나눈다.
#   USER_QUOTAS=read-only-SAMG:video=10,asset=50
# 여기에 없는 계정은 무제한이다. 기본값을 비워 두는 이유는 SEED_USERS와 같다.
# 코드에 특정 계정 이름을 박아 두면 그 계정이 없는 환경에서도 조용히 남고,
# 한도를 바꾸려고 코드를 고쳐 배포해야 한다.
USER_QUOTAS = os.getenv("USER_QUOTAS", "")
TOKEN_SECRET = os.getenv("TOKEN_SECRET", "local-development-secret-change-me")
TOKEN_TTL_SECONDS = int(os.getenv("TOKEN_TTL_SECONDS", "28800"))
# 로그인 유지(runtime persistLogin, 운영)일 때의 토큰 수명. 8시간이면 다음 날 사이트를 다시 열 때마다
# 로그인 화면이 떴다(설계자 2026-10-13). 화면이 열릴 때마다 /api/auth/renew 로 새로 받으므로
# 이 기간 동안 한 번도 열지 않은 경우에만 다시 로그인한다.
REMEMBER_TOKEN_TTL_SECONDS = int(os.getenv("REMEMBER_TOKEN_TTL_SECONDS", "604800"))
COVER_TEXT_MODEL = os.getenv("COVER_TEXT_MODEL", "global.anthropic.claude-haiku-4-5-20251001-v1:0")
# 영상 프롬프트는 업로드 이미지를 읽어야 해서 비전이 되는 모델을 쓴다. Haiku보다 묘사가 정확하다.
VIDEO_PROMPT_MODEL = os.getenv(
    "VIDEO_PROMPT_MODEL", "global.anthropic.claude-sonnet-4-5-20250929-v1:0"
)
# 그림체 판단은 짧은 분류 작업이라 값싼 모델로 충분하다.
STYLE_ROUTER_MODEL = os.getenv("STYLE_ROUTER_MODEL", "global.anthropic.claude-haiku-4-5-20251001-v1:0")
# 사용자가 적은 짧은 설명을 자세하게 다시 쓰는 단계(prompt_boost). 결과 품질이 여기서 갈리므로
# 가장 좋은 모델을 쓴다. 입력·출력이 짧아(수백 토큰) 비용 부담이 크지 않다.
#
# opus 4.5를 고른 이유는 실측 때문이다. 더 최신인 opus-5 / opus-4-8 / sonnet-5 는
# `temperature`를 거부한다("`temperature` is deprecated for this model").
# gateway.converse가 그 경우 temperature 없이 다시 부르도록 해 두었지만,
# opus-5는 짧은 maxTokens에서 본문 대신 추론 토큰만 소모해 빈 응답이 나왔다.
# 그래서 temperature를 정상적으로 받고 본문을 안정적으로 주는 4.5를 기본값으로 둔다.
PROMPT_BOOST_MODEL = os.getenv(
    "PROMPT_BOOST_MODEL", "global.anthropic.claude-opus-4-5-20251101-v1:0"
)
# 롱테이크: 고를 수 있는 목표 길이(초). 10분까지 지원한다.
LONG_TAKE_TARGETS = [30, 60, 180, 600]
# 이 계정의 Service Quotas: "On-demand model inference concurrent requests for Luma Ray V2" = 1.
# 그래서 컷은 순차로 만든다. 이 값을 올리려면 AWS에 쿼터 증설을 요청해야 한다.
SEQUENCE_CONCURRENCY = int(os.getenv("SEQUENCE_CONCURRENCY", "1"))
SEQUENCE_POLL_SECONDS = int(os.getenv("SEQUENCE_POLL_SECONDS", "20"))
# 컷 하나당 실측 2~5분. 예산을 넉넉히 두되 무한정 기다리지 않는다.
CLIP_BUDGET_SECONDS = int(os.getenv("CLIP_BUDGET_SECONDS", "420"))
SEQUENCE_TIMEOUT_SECONDS = int(os.getenv("SEQUENCE_TIMEOUT_SECONDS", "28800"))
# 스로틀 재시도. 동시 한도가 1이라 앞 컷이 끝나기 전에 부르면 반드시 걸린다.
CLIP_RETRY_ATTEMPTS = int(os.getenv("CLIP_RETRY_ATTEMPTS", "8"))
CLIP_RETRY_BASE_SECONDS = int(os.getenv("CLIP_RETRY_BASE_SECONDS", "20"))
CLIP_RETRY_MAX_SECONDS = int(os.getenv("CLIP_RETRY_MAX_SECONDS", "90"))
# 이미지 모델은 여기에 상수를 두지 않는다. COVER_IMAGE_MODEL / COVER_IMAGE_REGION 은
# model_registry 가 내장 카탈로그(BUILTIN_IMAGE_MODEL_ID / IMAGE_REGION)에서 읽는다.
# 예전에는 이 파일에도 같은 환경변수의 기본값이 적혀 있었고, 두 값이 어긋난 상태에서
# 카탈로그가 비자 관리자가 지정한 적 없는 모델이 호출됐다.
# 영상 모델이 있는 리전. 비동기 출력 버킷도 이 리전에 있어야 한다.
VIDEO_REGION = os.getenv("VIDEO_OUTPUT_REGION", "us-west-2")


MAX_REFERENCE_PIXELS = int(os.getenv("VIDEO_REFERENCE_MAX_PIXELS", "1536"))
# 첫 프레임의 짧은 변 하한 (품질·속도 계획서 Q1-②).
#
# 512 였다. 웹툰 컷은 366x457 처럼 작은 것이 많고, I2V 는 첫 프레임의 질감을 그대로
# 물려받아 결과가 저해상도로 나온다. Wan 480p 는 면적 399,360(짧은 변 480 급),
# 720p 는 921,600(짧은 변 720 급)이라 720 을 하한으로 두면 두 티어 모두에서
# 모델이 확대하지 않아도 된다. 모델이 늘리는 것보다 우리가 LANCZOS 로 올리는 편이
# 링잉이 덜하고, 무엇보다 **어떤 처리를 했는지 우리가 알 수 있다.**
MIN_FIRST_FRAME_PIXELS = int(os.getenv("VIDEO_FIRST_FRAME_MIN_PIXELS", "720"))
MIN_REFERENCE_PIXELS = int(os.getenv("VIDEO_REFERENCE_MIN_PIXELS", "512"))
# 분석용 축소본의 크기. 첫 프레임과 다른 값을 쓰는 이유가 둘 있다.
#
# 1. 비용: 비전 토큰은 픽셀 수에 비례한다(대략 가로*세로/750). 1536px 정사각이면 장당
#    약 3,100 토큰이라 20장이면 6만 토큰이다. 768px 로 줄이면 장당 약 790 토큰으로
#    4분의 1이 된다. 역할 판단("이건 캐릭터 시트인가")에는 768px 이면 충분하다.
# 2. 메모리: 앱 파드 limit 이 작다. 1536px PNG 20장을 동시에 들고 있으면 수십 MB가
#    한 요청에 묶인다. 첫 프레임은 한 장뿐이라 큰 값을 유지해도 된다.
BRIEF_IMAGE_PIXELS = int(os.getenv("VIDEO_BRIEF_MAX_PIXELS", "768"))
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_VIDEO_BYTES = 100 * 1024 * 1024
# 원본 자료 한도. 모델 컨텍스트와 요약 비용을 고려한 값이다.
MAX_SOURCE_CHARS = int(os.getenv("MAX_SOURCE_CHARS", "200000"))
ALLOWED_SOURCE_EXTENSIONS = {".txt", ".md", ".markdown", ".text"}
# 산출물 생성이 이 시간을 넘기면 파드가 교체된 것으로 보고 실패로 정리한다.
DOC_STALE_MINUTES = int(os.getenv("DOC_STALE_MINUTES", "10"))
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
ALLOWED_VIDEO_TYPES = {"video/mp4", "video/webm", "video/quicktime"}
VIDEO_EXTENSIONS = {".mp4", ".webm", ".mov"}
ROLE_ADMIN = graph_store.ROLE_ADMIN
ROLE_MEMBER = graph_store.ROLE_MEMBER
USERNAME_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{2,31}$")
# 가입과 비밀번호 변경이 같은 규칙을 쓰도록 한 곳에 둔다. 따로 적어 두면 한쪽만 고쳐져서
# "가입은 되는데 그 비밀번호로 바꿀 수는 없는" 상태가 된다.
MIN_PASSWORD_LENGTH = int(os.getenv("MIN_PASSWORD_LENGTH", "4"))

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(SERVICE_NAME)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_VIDEO_BYTES
s3 = boto3.client("s3", region_name=AWS_REGION)
_s3_clients: dict[str, object] = {AWS_REGION: s3}


_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{8,80}$")


def current_request_id() -> str:
    return str(getattr(g, "request_id", "") or "")


def response_error(
    message: str,
    status: int,
    code: str,
    *,
    retryable: bool = False,
    action: str = "",
):
    detail = {
        "code": code,
        "message": message,
        "retryable": bool(retryable),
        "requestId": current_request_id(),
    }
    if action:
        detail["action"] = action
    return jsonify({"ok": False, "error": detail}), status


def b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


QUOTA_VIDEO = graph_store.QUOTA_VIDEO
QUOTA_ASSET = graph_store.QUOTA_ASSET
# 화면과 오류 문구에 함께 쓴다. 두 곳에 따로 적으면 한쪽만 고쳐져 서로 다른 이름이 된다.
QUOTA_LABELS = {
    QUOTA_VIDEO: "영상 만들기",
    QUOTA_ASSET: "이미지·콘티·문서 만들기",
}


def parse_user_quotas() -> dict[str, dict]:
    """USER_QUOTAS 설정을 {사용자: {카테고리: 건수}} 로 읽는다.

    형식이 어긋난 조각은 조용히 버리지 않고 경고를 남긴다. 오타 하나로 한도가 사라지면
    무제한으로 열리는데, 그 사실이 아무 곳에도 드러나지 않는 것이 가장 위험하다.
    """
    result: dict[str, dict] = {}
    for chunk in USER_QUOTAS.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        name, _, spec = chunk.partition(":")
        name = name.strip()
        if not name or not spec.strip():
            logger.warning("USER_QUOTAS 조각을 해석할 수 없습니다: %s", chunk)
            continue
        quotas: dict[str, int] = {}
        for pair in spec.split(","):
            pair = pair.strip()
            if not pair:
                continue
            category, _, raw = pair.partition("=")
            category = category.strip()
            if category not in graph_store.QUOTA_CATEGORIES:
                logger.warning("USER_QUOTAS 의 알 수 없는 항목: %s (%s)", category, name)
                continue
            try:
                value = int(raw.strip())
            except ValueError:
                logger.warning("USER_QUOTAS 의 건수가 숫자가 아닙니다: %s (%s)", pair, name)
                continue
            if value < 0:
                logger.warning("USER_QUOTAS 의 건수가 음수입니다: %s (%s)", pair, name)
                continue
            quotas[category] = value
        if quotas:
            result[name] = quotas
    return result


def seed_user_list() -> list[dict]:
    quota_map = parse_user_quotas()
    entries = []
    for chunk in SEED_USERS.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        name, _, password = chunk.partition(":")
        name = name.strip()
        if name:
            entries.append(
                {
                    "username": name,
                    "password": (password or name).strip(),
                    # 한도가 지정되지 않은 계정은 quotas 가 빈 dict 다. merge_user 가
                    # 그것을 받아 속성을 지우므로 무제한으로 돌아간다.
                    "quotas": quota_map.get(name) or {},
                }
            )
    return entries


def issue_token(username: str, role: str, *, remember: bool = False) -> tuple[str, int]:
    expires_at = int(time.time()) + (REMEMBER_TOKEN_TTL_SECONDS if remember else TOKEN_TTL_SECONDS)
    payload = json.dumps(
        {"sub": username, "exp": expires_at}, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    encoded_payload = b64url_encode(payload)
    signature = hmac.new(
        TOKEN_SECRET.encode("utf-8"), encoded_payload.encode("ascii"), hashlib.sha256
    ).digest()
    return f"{encoded_payload}.{b64url_encode(signature)}", expires_at


def verify_token(token: str) -> dict | None:
    """Validate the signature, then re-read the role from the graph.

    The token only carries the subject: roles are always resolved server side so
    a stale or tampered token can never escalate privileges.
    """
    try:
        encoded_payload, encoded_signature = token.split(".", 1)
        expected = hmac.new(
            TOKEN_SECRET.encode("utf-8"), encoded_payload.encode("ascii"), hashlib.sha256
        ).digest()
        if not hmac.compare_digest(expected, b64url_decode(encoded_signature)):
            return None
        payload = json.loads(b64url_decode(encoded_payload))
        if int(payload["exp"]) <= int(time.time()):
            return None
        username = str(payload["sub"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None

    try:
        user = graph_store.get_user(username)
    except graph_store.GraphError:
        return None
    if not user:
        return None
    return {
        "username": user["username"],
        "role": user.get("role") or ROLE_MEMBER,
        "displayName": user.get("displayName") or user["username"],
    }


VIDEO_GEN_ENABLED = os.getenv("VIDEO_GEN_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")
STORY_DEV_ENABLED = os.getenv("STORY_DEV_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
def require_auth(handler):
    @wraps(handler)
    def wrapped(*args, **kwargs):
        authorization = request.headers.get("Authorization", "")
        if not authorization.startswith("Bearer "):
            return response_error("로그인이 필요합니다.", 401, "AUTH_REQUIRED")
        identity = verify_token(authorization[7:].strip())
        if not identity:
            return response_error("세션이 만료되었습니다. 다시 로그인해 주세요.", 401, "INVALID_TOKEN")
        g.username = identity["username"]
        g.role = identity["role"]
        g.display_name = identity["displayName"]
        return handler(*args, **kwargs)

    return wrapped


def require_admin(handler):
    @wraps(handler)
    @require_auth
    def wrapped(*args, **kwargs):
        if g.role != ROLE_ADMIN:
            logger.warning("Admin route refused user=%s role=%s", g.username, g.role)
            return response_error("관리자만 수행할 수 있는 작업입니다.", 403, "ADMIN_ONLY")
        return handler(*args, **kwargs)

    return wrapped


# --------------------------------------------------------------------------- #
# 결과물 생성 한도
# --------------------------------------------------------------------------- #
#
# 세는 단위는 **사용자가 받는 결과물 한 건**이다. 내부에서 모델을 몇 번 부르든 1건이다.
# 콘티 하나가 Bedrock 호출 10회 이상이라, 호출 수로 세면 컷 8개 콘티 한 번에 한도가
# 10 줄어든다. 사용자가 관리하려는 것은 그것이 아니다.
#
# 검사와 차감 시점을 일부러 떼어 놓았다.
#   검사 = 요청을 받은 즉시. 남은 것이 없으면 비싼 작업을 시작하지 않는다.
#   차감 = 결과물이 실제로 만들어진 뒤. 실패한 시도는 한도를 먹지 않는다.
# 영상·콘티·문서는 202를 주고 배경 스레드에서 끝나므로, 차감은 그 완료 지점에 있어야 한다.
# 요청 시점에 차감하면 안전 필터에 걸려 실패한 시도까지 한도를 먹는다.
#
# 남은 위험을 적어 둔다: 검사와 차감 사이에 같은 사용자가 동시에 여러 건을 시작하면
# 한도를 조금 넘길 수 있다. 영상은 모델 동시 한도가 1이라 사실상 막히고, 문서·이미지는
# 한 건이 저렴해 이 정도 오차를 허용한다. 정확히 막으려면 시작 시점에 자리를 예약하고
# 실패 시 되돌려야 하는데, 되돌리기가 빠지면 오히려 한도가 조용히 줄어든다.
def quota_block(category: str):
    """한도가 남아 있지 않으면 429 응답을 돌려준다. 여유가 있으면 None."""
    try:
        remaining = graph_store.quota_remaining(g.username, category)
    except graph_store.GraphError:
        # GraphUnavailable 도 GraphError 의 하위라 함께 잡힌다.
        # 그래프를 못 읽으면 막지 않는다. 이 경로는 사실상 닿지 않는다. verify_token 이
        # 이미 get_user 로 그래프를 읽으므로, 그래프가 죽으면 로그인 자체가 401이 된다.
        # 그럼에도 여기서 예외를 올리면 조회 실패가 생성 실패로 보이게 된다.
        logger.warning("한도를 확인하지 못해 통과시킵니다 user=%s", g.username)
        return None
    if remaining is None or remaining > 0:
        return None

    label = QUOTA_LABELS.get(category, category)
    logger.info("Quota exhausted user=%s category=%s", g.username, category)
    return response_error(
        f"{label} 한도를 모두 사용했습니다. 관리자에게 한도 초기화를 요청해 주세요.",
        429,
        "QUOTA_EXCEEDED",
    )


def require_quota(category: str):
    """생성 진입점에 붙여 요청 시점에 잔여 한도를 확인한다.

    `@require_auth` 안쪽(아래쪽)에 붙여야 한다. g.username 이 필요하기 때문이다.
    호출부에서 if 문으로 검사하게 하지 않고 데코레이터로 둔 이유는, 진입점이 일곱 곳이라
    한 곳을 빠뜨리면 그 기능만 무제한이 되고 그것을 알아차릴 방법이 없기 때문이다.
    """

    def decorator(handler):
        @wraps(handler)
        def wrapped(*args, **kwargs):
            blocked = quota_block(category)
            if blocked:
                return blocked
            return handler(*args, **kwargs)

        return wrapped

    return decorator


def spend_quota(username: str, category: str, action: str, ref_id: str = "") -> None:
    """결과물이 실제로 만들어졌을 때 1건 차감한다.

    배경 스레드에서도 부르므로 g 에 의존하지 않고 사용자 이름을 인자로 받는다.
    """
    graph_store.spend_credit(username, category, action, ref_id)


def quota_view(username: str) -> dict:
    """로그인 응답과 /api/auth/me 에 함께 담는 한도 상태.

    화면이 한도가 걸린 계정인지 알아야 잔여 건수를 보여 줄 수 있다. 조회에 실패하면
    무제한으로 본다. 여기서 예외를 올리면 한도와 무관한 로그인이 막힌다.
    """
    try:
        return graph_store.quota_status(username)
    except graph_store.GraphError:
        logger.warning("한도 상태를 읽지 못했습니다 user=%s", username)
        return {"limited": False, "categories": {}}


def user_view() -> dict:
    return {
        "username": g.username,
        "displayName": g.display_name,
        "role": g.role,
        "isAdmin": g.role == ROLE_ADMIN,
        "canManageModels": g.role == ROLE_ADMIN,
        "canManageUsers": g.role == ROLE_ADMIN,
        "quotas": quota_view(g.username),
    }


def safe_user_prefix(username: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_-]", "-", username).strip("-")
    return normalized or "user"


def normalized_filename(original_name: str, fallback_extension: str) -> str:
    name = secure_filename(original_name or "")
    stem = Path(name).stem[:80] or "asset"
    suffix = Path(name).suffix.lower() or fallback_extension
    return f"{stem}{suffix}"


def extension_for_content_type(content_type: str) -> str:
    return {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "video/mp4": ".mp4",
        "video/webm": ".webm",
        "video/quicktime": ".mov",
    }.get(content_type, ".bin")


def s3_client(region: str):
    if region not in _s3_clients:
        _s3_clients[region] = boto3.client("s3", region_name=region)
    return _s3_clients[region]


# Bedrock 클라이언트는 gateway가 단독으로 만든다. 여기서 따로 만들면 타임아웃 설정이 갈리고
# 사용량 기록을 우회하는 호출 지점이 생긴다.


# 버킷 리전 조회는 model_registry가 캐시와 함께 갖고 있다. 두 곳에서 캐시하면 값이 갈린다.
bucket_region = model_registry.bucket_region


def presign(bucket: str, key: str, region: str | None = None, expires: int = 3600,
            content_type: str = "") -> str:
    """만료되는 GET 서명. `content_type` 을 주면 응답 형식을 지정한다.

    provider 가 올린 객체에는 `ContentType` 이 없어 S3 기본값이 나간다. 그대로 두면
    브라우저가 영상 재생을 거부할 수 있다 (실측: 샷 클립이 octet-stream).
    """
    client = s3_client(region or bucket_region(bucket) or AWS_REGION)
    params: dict = {"Bucket": bucket, "Key": key}
    if content_type:
        params["ResponseContentType"] = content_type
    return client.generate_presigned_url(
        "get_object", Params=params, ExpiresIn=expires
    )


def store_bytes(bucket: str, key: str, body: bytes, content_type: str, region: str | None = None) -> None:
    client = s3_client(region or AWS_REGION)
    client.put_object(
        Bucket=bucket,
        Key=key,
        Body=body,
        ContentType=content_type,
        ServerSideEncryption="AES256",
    )


def s3_original_name(key: str) -> str:
    """업로드 당시의 원본 파일명을 S3 메타데이터에서 읽는다.

    **S3 키에 든 이름으로는 알 수 없다.** `normalized_filename`이 `secure_filename`을 쓰고
    그 함수가 ASCII 밖의 문자를 버리기 때문이다. 실측하면 이렇게 된다.

        주인공_김남주_정면_캐릭터시트.png ->  png
        원본장면_비오는골목_밤.jpg        ->  jpg
        캐릭터 구성도 (수정본).webp       ->  webp

    파일명에 적은 설명을 근거로 이미지를 분석하는 기능(image_brief)이 이 값에 의존하므로,
    업로드 시점에 원본 이름을 따로 저장해 두고 여기서 되읽는다.
    클라이언트가 보낸 이름을 믿지 않는 이유는, 목록·이력 화면에서도 같은 값이 필요하고
    요청마다 다시 보내게 하면 값이 갈리기 때문이다.
    """
    try:
        head = s3.head_object(Bucket=S3_BUCKET, Key=key)
    except (BotoCoreError, ClientError):
        logger.warning("원본 파일명을 읽지 못했습니다 key=%s", key)
        return ""
    raw = (head.get("Metadata") or {}).get("original-name") or ""
    if not raw:
        return ""
    try:
        return urllib.parse.unquote(raw)
    except (ValueError, UnicodeDecodeError):
        return ""


def upload_file(file_storage, category: str, allowed_types: set[str], max_bytes: int):
    content_type = (file_storage.mimetype or "").lower()
    if content_type not in allowed_types:
        raise ValueError("지원하지 않는 파일 형식입니다.")

    file_storage.stream.seek(0, os.SEEK_END)
    size = file_storage.stream.tell()
    file_storage.stream.seek(0)
    if size <= 0:
        raise ValueError("빈 파일은 업로드할 수 없습니다.")
    if size > max_bytes:
        raise ValueError(f"파일 크기는 {max_bytes // (1024 * 1024)}MB 이하여야 합니다.")

    extension = extension_for_content_type(content_type)
    original_name = (file_storage.filename or "").strip()[:200]
    filename = normalized_filename(file_storage.filename, extension)
    now = datetime.now(timezone.utc)
    user_prefix = safe_user_prefix(g.username)
    key = f"users/{user_prefix}/{category}/{now:%Y/%m/%d}/{uuid.uuid4().hex}_{filename}"
    s3.upload_fileobj(
        file_storage.stream,
        S3_BUCKET,
        key,
        ExtraArgs={
            "ContentType": content_type,
            "ServerSideEncryption": "AES256",
            "Metadata": {
                "uploaded-by": user_prefix,
                "service": SERVICE_NAME,
                # 원본 파일명을 보존한다. S3 메타데이터는 HTTP 헤더로 실려 ASCII만 허용하므로
                # 퍼센트 인코딩해서 넣는다. 한글 파일명이 키에서는 사라지기 때문에 필요하다.
                "original-name": urllib.parse.quote(original_name, safe=""),
            },
        },
    )
    return {
        "key": key,
        "filename": filename,
        # 화면에는 사용자가 올린 이름 그대로 보여준다. 갤러리에서 어느 파일인지 알아야 한다.
        "originalName": original_name,
        "contentType": content_type,
        "size": size,
        "uploadedAt": now.isoformat(),
    }


def video_buckets() -> list[tuple[str, str]]:
    """영상이 있을 수 있는 (버킷, 리전) 목록.

    업로드한 영상은 기본 버킷에, 생성된 영상은 모델과 같은 리전의 버킷에 떨어진다.
    한쪽만 보면 생성한 영상이 라이브러리에 나타나지 않는다.
    """
    buckets = [(S3_BUCKET, AWS_REGION)]
    if S3_VIDEO_BUCKET and S3_VIDEO_BUCKET != S3_BUCKET:
        buckets.append((S3_VIDEO_BUCKET, bucket_region(S3_VIDEO_BUCKET) or VIDEO_REGION))
    return buckets


def list_objects(
    category: str,
    extensions: set[str] | None = None,
    buckets: list[tuple[str, str]] | None = None,
):
    prefix = f"users/{safe_user_prefix(g.username)}/{category}/"
    items = []
    for bucket, region in buckets or [(S3_BUCKET, AWS_REGION)]:
        client = s3_client(region)
        paginator = client.get_paginator("list_objects_v2")
        try:
            pages = paginator.paginate(
                Bucket=bucket, Prefix=prefix, PaginationConfig={"MaxItems": 200}
            )
            for page in pages:
                for item in page.get("Contents", []):
                    key = item["Key"]
                    if extensions and Path(key).suffix.lower() not in extensions:
                        continue
                    filename = key.rsplit("/", 1)[-1]
                    items.append(
                        {
                            "key": key,
                            "bucket": bucket,
                            "filename": filename.split("_", 1)[-1] if "_" in filename else filename,
                            "size": item["Size"],
                            "lastModified": item["LastModified"].isoformat(),
                            "url": presign(bucket, key, region),
                        }
                    )
        except (BotoCoreError, ClientError):
            # 한 버킷을 못 읽어도 나머지는 보여준다. 목록이 통째로 비는 것보다 낫다.
            logger.warning("목록 조회에 실패한 버킷을 건너뜁니다: %s", bucket)
    items.sort(key=lambda value: value["lastModified"], reverse=True)
    return items


def public_model(model: dict, include_internals: bool) -> dict:
    view = {
        "id": model["id"],
        "label": model["label"],
        "kind": model.get("kind", "video"),
        "tier": model.get("tier", "standard"),
        "enabled": model["enabled"],
        "isDefault": model["isDefault"],
        "capabilities": model.get("capabilities", {}),
        "defaults": model.get("defaults", {}),
        "notes": model.get("notes", ""),
    }
    if include_internals:
        view.update(
            {
                "provider": model.get("provider"),
                "modelId": model.get("modelId"),
                "region": model.get("region"),
                "invocation": model.get("invocation"),
                "outputBucket": model.get("outputBucket"),
                "request": model.get("request"),
                "source": model.get("source"),
                "lifecycle": model.get("lifecycle"),
                "lifecycleCheckedAt": model.get("lifecycleCheckedAt"),
                "lifecycleSource": model.get("lifecycleSource"),
                "eolAt": model.get("eolAt"),
                "apiOperation": model.get("apiOperation"),
                "priceUnit": model.get("priceUnit"),
                "priceUsd": model.get("priceUsd"),
                "priceCheckedAt": model.get("priceCheckedAt"),
                "priceSource": model.get("priceSource"),
                "updatedAt": model.get("updatedAt"),
                "updatedBy": model.get("updatedBy"),
            }
        )
    return view


def project_payload(project: dict) -> dict:
    view = dict(project)
    cover_key = project.get("coverKey")
    view["coverUrl"] = presign(S3_BUCKET, cover_key, AWS_REGION) if cover_key else None
    view["coverHistory"] = [
        {
            "key": entry.get("key"),
            "source": entry.get("source"),
            "replacedAt": entry.get("replacedAt"),
            "url": presign(S3_BUCKET, entry["key"], AWS_REGION),
        }
        for entry in project.get("coverHistory", [])
        if entry.get("key")
    ]
    view["coverHistoryLimit"] = graph_store.COVER_HISTORY_LIMIT
    return view


def remove_s3_objects(keys: list[str]) -> None:
    """정리용 삭제. 실패해도 요청 흐름을 막지 않고 경고만 남긴다."""
    for key in keys:
        if not key:
            continue
        try:
            s3.delete_object(Bucket=S3_BUCKET, Key=key)
            logger.info("S3 object removed key=%s", key)
        except (BotoCoreError, ClientError):
            logger.warning("S3 cleanup skipped key=%s", key)


def apply_new_cover(project_id: str, cover_key: str, cover_source: str) -> dict:
    """표지를 교체하고 보관 한도를 넘긴 이전 이미지를 S3에서 지운다."""
    updated, evicted = graph_store.set_project_cover(project_id, cover_key, cover_source)
    remove_s3_objects(evicted)
    return updated


def build_source_key(project_id: str, name: str) -> str:
    """원본 자료 키. IRSA 정책이 users/* 로 제한되어 있어 사용자 prefix 아래에 둔다."""
    stem = Path(secure_filename(name or "source")).stem[:60] or "source"
    return f"users/{safe_user_prefix(g.username)}/sources/{project_id}/{uuid.uuid4().hex}_{stem}.txt"


def extract_source_text() -> tuple[str, str]:
    """붙여넣은 텍스트 또는 업로드한 텍스트 파일에서 본문을 얻는다.

    (본문, 표시용 이름)을 돌려주고, 검증 실패는 ValueError로 알린다.
    """
    upload = request.files.get("file")
    if upload is not None:
        extension = Path(upload.filename or "").suffix.lower()
        if extension not in ALLOWED_SOURCE_EXTENSIONS:
            raise ValueError("텍스트 파일(.txt, .md)만 업로드할 수 있습니다.")
        raw = upload.read()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = raw.decode("cp949")
            except UnicodeDecodeError:
                raise ValueError("UTF-8 또는 CP949로 인코딩된 텍스트 파일만 지원합니다.")
        name = Path(upload.filename or "source.txt").name
    else:
        payload = request.get_json(silent=True) or {}
        text = str(payload.get("text") or "")
        name = str(payload.get("name") or "붙여넣은 자료").strip()[:120] or "붙여넣은 자료"

    text = text.replace("\r\n", "\n").strip()
    if len(text) < 20:
        raise ValueError("원본 자료는 20자 이상이어야 합니다.")
    if len(text) > MAX_SOURCE_CHARS:
        raise ValueError(f"원본 자료는 {MAX_SOURCE_CHARS:,}자 이하여야 합니다.")
    return text, name


def read_source_text(key: str) -> str:
    obj = s3.get_object(Bucket=S3_BUCKET, Key=key)
    return obj["Body"].read().decode("utf-8", errors="replace")


def read_source_bytes(key: str) -> bytes:
    """S3에서 이진 파일을 읽는다. PDF에 넣을 콘티 컷 그림이 이 경로로 온다.

    presigned URL을 쓰지 않는 이유: 문서를 만드는 것은 서버이고, 서버는 이미 이 버킷에
    접근할 수 있다. 서명 URL을 만들어 자기 자신에게 HTTP로 다시 요청하면 왕복이 하나
    늘고 URL 만료라는 실패 경로가 새로 생긴다.
    """
    obj = s3.get_object(Bucket=S3_BUCKET, Key=key)
    return obj["Body"].read()


def pdf_filename(*parts: str) -> str:
    """다운로드 파일명. 프로젝트·종류·날짜가 들어가야 다운로드 폴더에서 구분된다.

    화면의 projectFilePrefix() 와 같은 구성이다(제목_날짜). 금지 문자를 지우는 규칙도
    safeFileName() 과 같다 — Windows 는 \\ / : * ? " < > | 를 거부한다.
    """
    cleaned = [re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", str(part or "")).strip() for part in parts]
    stem = "_".join(part for part in cleaned if part)[:120].strip(". ") or "결과물"
    return f"{stem}_{datetime.now(timezone.utc):%Y%m%d}.pdf"


def pdf_response(data: bytes, filename: str):
    """PDF 다운로드 응답.

    `attachment` 로 내린다. inline 이면 브라우저가 탭에서 열어 버리고 사용자는 다시
    저장을 눌러야 한다. 이 버튼의 목적은 파일을 받는 것이다.

    파일명은 RFC 5987 로 두 번 적는다. 한국어 제목이 들어가는데, `filename=` 만 쓰면
    비ASCII 를 다루지 못하는 클라이언트가 이름을 깨뜨리거나 통째로 버린다. 그래서 ASCII
    대체 이름을 먼저 주고 `filename*` 에 UTF-8 을 담는다. 최신 브라우저는 뒤를 택한다.
    """
    quoted = urllib.parse.quote(filename)
    return Response(
        data,
        mimetype="application/pdf",
        headers={
            "Content-Disposition": (
                f'attachment; filename="document.pdf"; filename*=UTF-8\'\'{quoted}'
            ),
            "Content-Length": str(len(data)),
            # 결과물은 다시 만들어질 수 있다(회차 재생성·컷 다시 그리기). 캐시하지 않는다.
            "Cache-Control": "no-store",
        },
    )


def can_read_project(project: dict) -> bool:
    if project.get("visibility") == graph_store.VISIBILITY_SHARED:
        return True
    return project.get("owner") == g.username or g.role == ROLE_ADMIN


def can_write_project(project: dict) -> bool:
    return project.get("owner") == g.username or g.role == ROLE_ADMIN


@app.before_request
def enforce_account_guard():
    supplied = str(request.headers.get("X-Request-ID") or "").strip()
    g.request_id = supplied if _REQUEST_ID.match(supplied) else uuid.uuid4().hex
    if request.path in {"/health", "/api/health"}:
        return None
    guard = account_guard.state()
    if not guard["ok"]:
        return response_error(
            f"허용된 AWS 계정({account_guard.EXPECTED_ACCOUNT_ID})이 아니어서 요청을 처리하지 않습니다.",
            503,
            "ACCOUNT_NOT_ALLOWED",
        )
    return None


@app.after_request
def add_api_headers(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    if current_request_id():
        response.headers["X-Request-ID"] = current_request_id()
    return response


@app.errorhandler(RequestEntityTooLarge)
def handle_file_too_large(_error):
    return response_error("파일 크기 제한을 초과했습니다.", 413, "FILE_TOO_LARGE")


@app.errorhandler(404)
def handle_not_found(_error):
    return response_error("요청한 API를 찾을 수 없습니다.", 404, "NOT_FOUND")


@app.errorhandler(500)
def handle_internal_error(error):
    logger.exception("Unhandled API error", exc_info=error)
    return response_error("서버에서 요청을 처리하지 못했습니다.", 500, "INTERNAL_ERROR")


@app.errorhandler(graph_store.GraphUnavailable)
def handle_graph_unavailable(error):
    logger.error("Graph unavailable: %s", error)
    return response_error(str(error), 503, "GRAPH_UNAVAILABLE")


@app.get("/health")
@app.get("/api/health")
def health():
    guard = account_guard.state()
    return jsonify(
        {
            "ok": True,
            "service": SERVICE_NAME,
            "region": AWS_REGION,
            "account": {
                "expected": account_guard.EXPECTED_ACCOUNT_ID,
                "allowed": guard["ok"],
                "detail": guard["detail"],
            },
            "graph": graph_store.health(),
            # 어떤 빌드가 돌고 있는지. 결과가 달라졌을 때 가장 먼저 볼 곳이다 (F-24).
            "build": {
                "imageTag": APP_IMAGE_TAG,
                "sourceRevision": APP_SOURCE_REVISION,
                "sourceArchive": APP_SOURCE_ARCHIVE,
                "gitContext": APP_SOURCE_GIT,
            },
            # manifest 저장 실패 누적. **0이 아니면 기록이 비어 있는 결과가 쌓이고 있다.**
            # 배선 검사(check-manifest.py)는 런타임 실패를 잡지 못하므로 여기가 유일한 신호다.
            "manifestFailures": dict(_manifest_failures),
        }
    )


# --------------------------------------------------------------------------- #
# auth
# --------------------------------------------------------------------------- #
@app.post("/api/auth/login")
def login():
    payload = request.get_json(silent=True) or {}
    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", ""))
    if not username or not password:
        return response_error("아이디와 비밀번호를 입력해 주세요.", 400, "MISSING_CREDENTIALS")

    graph_store.bootstrap_if_needed(ADMIN_USERNAME, ADMIN_PASSWORD, seed_user_list(), DEFAULT_COMPANY_CODE)
    user = graph_store.authenticate(username, password)
    if not user:
        time.sleep(0.25)
        return response_error("아이디 또는 비밀번호가 올바르지 않습니다.", 401, "INVALID_CREDENTIALS")

    token, expires_at = issue_token(user["username"], user["role"], remember=payload.get("remember") is True)
    logger.info("Login ok user=%s role=%s", user["username"], user["role"])
    return jsonify(
        {
            "ok": True,
            "token": token,
            "expiresAt": expires_at,
            "user": {
                "username": user["username"],
                "displayName": user["displayName"],
                "role": user["role"],
                "isAdmin": user["role"] == ROLE_ADMIN,
                "canManageModels": user["role"] == ROLE_ADMIN,
                "canManageUsers": user["role"] == ROLE_ADMIN,
                # 로그인 직후에도 잔여 건수를 보여 줄 수 있게 함께 준다. 이것이 없으면
                # 화면이 /api/auth/me 를 한 번 더 불러야 패널을 그릴 수 있다.
                "quotas": quota_view(user["username"]),
            },
        }
    )


@app.post("/api/auth/signup")
def signup():
    """Self-service registration gated by the company code the admin sets."""
    payload = request.get_json(silent=True) or {}
    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", ""))
    company_code = str(payload.get("companyCode", "")).strip()

    if not USERNAME_PATTERN.match(username):
        return response_error(
            "아이디는 영문/숫자로 시작하는 3~32자여야 합니다. (영문, 숫자, . _ - 사용 가능)",
            400,
            "INVALID_USERNAME",
        )
    if len(password) < MIN_PASSWORD_LENGTH:
        return response_error(
            f"비밀번호는 {MIN_PASSWORD_LENGTH}자 이상이어야 합니다.", 400, "WEAK_PASSWORD"
        )

    graph_store.bootstrap_if_needed(ADMIN_USERNAME, ADMIN_PASSWORD, seed_user_list(), DEFAULT_COMPANY_CODE)

    expected_code = graph_store.get_company_code()
    if not expected_code:
        return response_error("회사 코드가 설정되지 않았습니다. 관리자에게 문의해 주세요.", 503, "COMPANY_CODE_UNSET")
    if not hmac.compare_digest(company_code, expected_code):
        time.sleep(0.25)
        return response_error("회사 고유 코드가 올바르지 않습니다.", 403, "INVALID_COMPANY_CODE")

    try:
        user = graph_store.create_user(username, password, role=ROLE_MEMBER)
    except graph_store.ConflictError as error:
        return response_error(str(error), 409, "USERNAME_TAKEN")

    logger.info("Signup ok user=%s", username)
    return jsonify({"ok": True, "user": user}), 201


@app.get("/api/auth/me")
@require_auth
def current_user():
    return jsonify(
        {
            "ok": True,
            "user": user_view(),
            "storagePrefix": f"users/{safe_user_prefix(g.username)}/",
        }
    )


@app.post("/api/auth/renew")
@require_auth
def renew_token():
    """유효한 토큰을 새 만료 시각의 토큰으로 바꿔 준다(로그인 유지, 설계자 2026-10-13).

    화면이 열릴 때 부른다. 쓰는 동안은 만료가 계속 뒤로 밀리고, 오래 안 쓰면 그대로 만료된다.
    require_auth 가 서명 · 만료 · 계정(역할)을 이미 확인했다.
    """
    payload = request.get_json(silent=True) or {}
    token, expires_at = issue_token(g.username, g.role, remember=payload.get("remember") is True)
    return jsonify({"ok": True, "token": token, "expiresAt": expires_at})


@app.post("/api/auth/password")
@require_auth
def change_own_password():
    """로그인한 사용자가 **자기** 비밀번호를 바꾼다. admin, 일반 사용자 모두 같은 경로다.

    대상은 항상 토큰의 주인(`g.username`)이다. 본문으로 대상을 받지 않는다. 받으면
    관리자가 아닌 사용자가 남의 아이디를 넣어 볼 여지가 생기고, 그 검사를 여기서
    다시 해야 한다. 애초에 받지 않는 편이 안전하다.

    관리자가 남의 비밀번호를 초기화하는 기능은 여기에 없다. 필요하면 별도 관리자
    라우트로 만들어야 하고, 그때는 감사 로그와 대상 계정 확인이 함께 필요하다.

    ## 남는 한계

    세션 저장소가 없어서 **다른 기기에 남아 있는 세션은 무효화되지 않는다.** 토큰은
    비밀번호가 아니라 `TOKEN_SECRET` 으로 서명되고 만료(기본 8시간)까지 유효하다.
    전부 끊으려면 `TOKEN_SECRET` 을 회전해야 하는데 그러면 모든 사용자가 로그아웃된다.
    """
    payload = request.get_json(silent=True) or {}
    current_password = str(payload.get("currentPassword", ""))
    new_password = str(payload.get("newPassword", ""))

    if not current_password or not new_password:
        return response_error(
            "현재 비밀번호와 새 비밀번호를 모두 입력해 주세요.", 400, "MISSING_CREDENTIALS"
        )
    if len(new_password) < MIN_PASSWORD_LENGTH:
        return response_error(
            f"새 비밀번호는 {MIN_PASSWORD_LENGTH}자 이상이어야 합니다.", 400, "WEAK_PASSWORD"
        )
    if new_password == current_password:
        return response_error("현재 비밀번호와 다른 값을 입력해 주세요.", 400, "PASSWORD_UNCHANGED")

    try:
        result = graph_store.change_password(g.username, current_password, new_password)
    except graph_store.AuthError as error:
        # 로그인 실패와 같은 지연을 준다. 이 경로도 비밀번호를 맞춰 보는 데 쓸 수 있고,
        # 여기서는 아이디가 이미 정해져 있어 오히려 표적이 분명하다.
        time.sleep(0.25)
        logger.warning("Password change refused user=%s reason=bad-current", g.username)
        # 401 이 아니라 403 이다. 프론트의 api() 래퍼가 401 을 "세션 만료"로 보고
        # 즉시 로그아웃시킨다. 현재 비밀번호를 한 번 잘못 적은 사용자를 튕겨내면
        # 다시 로그인해서 처음부터 해야 한다. 토큰은 유효하고 입력만 틀린 상황이므로
        # 403(인증은 됐지만 이 요청은 거부)이 실제 상태에 맞다.
        return response_error(str(error), 403, "INVALID_CREDENTIALS")
    except graph_store.ConflictError as error:
        return response_error(str(error), 400, "PASSWORD_UNCHANGED")
    except graph_store.NotFoundError as error:
        return response_error(str(error), 404, "USER_NOT_FOUND")

    # GraphUnavailable 은 일부러 잡지 않는다. 전역 핸들러가 503 으로 알려 준다.
    # 400 으로 뭉개면 "비밀번호가 문제"인지 "DB가 문제"인지 화면에서 구분할 수 없다.
    return jsonify({"ok": True, "passwordUpdatedAt": result["passwordUpdatedAt"]})


# --------------------------------------------------------------------------- #
# admin: company code + users
# --------------------------------------------------------------------------- #
@app.route("/api/admin/company-code", methods=["GET", "PUT"])
@require_admin
def company_code():
    if request.method == "GET":
        return jsonify({"ok": True, "companyCode": graph_store.get_company_code()})

    payload = request.get_json(silent=True) or {}
    code = str(payload.get("companyCode", "")).strip()
    if not re.match(r"^[A-Za-z0-9._-]{4,64}$", code):
        return response_error("회사 코드는 영문/숫자/._- 조합 4~64자여야 합니다.", 400, "INVALID_COMPANY_CODE")
    graph_store.set_company_code(code, actor=g.username)
    return jsonify({"ok": True, "companyCode": code})


@app.get("/api/admin/usage")
@require_admin
def admin_usage():
    """사용자별·모델별 모델 사용량. 기본은 최근 30일."""
    try:
        days = int(request.args.get("days", "30"))
    except ValueError:
        return response_error("days는 숫자여야 합니다.", 400, "INVALID_DAYS")
    if not 1 <= days <= 365:
        return response_error("days는 1~365 사이여야 합니다.", 400, "INVALID_DAYS")

    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    summary = gateway.summarize_usage(since)
    summary["days"] = days
    summary["ok"] = True
    return jsonify(summary)


@app.get("/api/admin/video-costs")
@require_admin
def admin_video_costs():
    """영상 기능(vpoc1 · vpoc2)이 지금까지 쓴 비용과 API 토큰. 관리자 모델 관리에서만 본다.

    설계자 2026-10-07: 영상 만들기 화면의 비용 표시를 모두 걷고 여기 한 곳에서만 보인다.
    원장 두 개(users/_vpoc1/ledger.json · users/_vpoc2/ledger.json)를 그대로 합친다.
    토큰은 2026-10-07 부터 기록한다 — 그 전 항목은 금액만 있다.
    """
    from .vpoc import ledger as vpoc_ledger  # noqa: PLC0415
    from .vpoc2 import ledger as vpoc2_ledger  # noqa: PLC0415

    sources = []
    try:
        sources.append(("vpoc1", vpoc_ledger.read()[0].get("entries") or []))
    except Exception as exc:  # noqa: BLE001
        app.logger.warning("video-costs: vpoc1 ledger unreadable: %s", exc)
    try:
        sources.append(("vpoc2", vpoc2_ledger.read().get("entries") or []))
    except Exception as exc:  # noqa: BLE001
        app.logger.warning("video-costs: vpoc2 ledger unreadable: %s", exc)

    def blank() -> dict:
        return {"calls": 0, "usd": 0.0, "units": 0.0, "inputTokens": 0, "outputTokens": 0}

    totals = blank()
    by_kind: dict[str, dict] = {}
    by_model: dict[tuple[str, str], dict] = {}
    for _source, entries in sources:
        for entry in entries:
            raw_kind = str(entry.get("kind") or "")
            if raw_kind == "hq-slot":  # 720p 월 한도 자리. 비용이 아니다.
                continue
            # Seedance 완료 때 남긴 토큰 항목은 같은 모델의 영상 행에 합친다(호출 수에는 안 센다).
            token_only = raw_kind == "video-tokens"
            kind = "video" if token_only else raw_kind
            model = str(entry.get("model") or "")
            for bucket in (totals, by_kind.setdefault(kind, blank()),
                           by_model.setdefault((kind, model), blank())):
                if not token_only:
                    bucket["calls"] += 1
                    bucket["units"] += float(entry.get("units") or 0)
                bucket["usd"] += float(entry.get("usd") or 0)
                bucket["inputTokens"] += int(entry.get("inputTokens") or 0)
                bucket["outputTokens"] += int(entry.get("outputTokens") or 0)

    def rounded(row: dict) -> dict:
        return {**row, "usd": round(row["usd"], 2), "units": round(row["units"], 2)}

    return jsonify({
        "ok": True,
        "totals": rounded(totals),
        "byKind": [{"kind": k, **rounded(v)} for k, v in sorted(by_kind.items(), key=lambda kv: -kv[1]["usd"])],
        "byModel": [
            {"kind": k, "model": m, **rounded(v)}
            for (k, m), v in sorted(by_model.items(), key=lambda kv: -kv[1]["usd"])
        ],
        "sources": [name for name, _ in sources],
        "tokensSince": "2026-10-07",
    })


@app.get("/api/usage/me")
@require_auth
def my_usage():
    """**자기 사용량만** 본다. 관리자 화면과 같은 내용을 본인 범위로 좁힌 것이다.

    /api/admin/usage 를 일반 사용자에게 열지 않고 라우트를 따로 둔 이유가 있다.
    그 라우트는 응답에 byUser(전체 사용자 목록)와 byModel, recent 를 담는다. 권한만 풀면
    한도가 걸린 사용자가 다른 사람의 사용량과 모델 구성을 그대로 보게 된다.
    여기서는 사용자 이름을 요청에서 받지 않고 **토큰의 주체로 고정**하므로,
    ?username= 을 붙여 남의 것을 보는 것이 불가능하다.
    """
    try:
        days = int(request.args.get("days", "30"))
    except ValueError:
        return response_error("days는 숫자여야 합니다.", 400, "INVALID_DAYS")
    if not 1 <= days <= 365:
        return response_error("days는 1~365 사이여야 합니다.", 400, "INVALID_DAYS")

    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    summary = gateway.summarize_usage(since, username=g.username)
    # byUser 는 자기 자신 한 줄뿐이라 화면에서 쓸 일이 없다. 응답을 가볍게 하려고 뺀다.
    summary.pop("byUser", None)
    summary["username"] = g.username
    summary["days"] = days
    # 한도와 무엇에 썼는지. 사용량(모델 호출)과 한도(결과물 건수)는 세는 단위가 달라서
    # 두 숫자가 다르게 보인다. 화면이 그것을 나란히 보여 줘야 오해가 없다.
    summary["quotas"] = quota_view(g.username)
    try:
        summary["actions"] = graph_store.credit_actions(g.username, limit=100)
    except graph_store.GraphError:
        summary["actions"] = []
    summary["ok"] = True
    return jsonify(summary)


# --------------------------------------------------------------------------- #
# admin: 프롬프트 두 계층 (지시서 작업 5~8)
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# 클라이언트 오류 리포트
# --------------------------------------------------------------------------- #
# 화면에서 터진 예외를 서버 로그로 모은다. 사용자가 브라우저 콘솔을 열지 않아도 원인을
# 볼 수 있게 하기 위한 것이다.
#
# 이것이 없어서 실제로 오래 헤맸다. 화면은 토스트에 `error.message` 만 보여 주고 스택을
# 버렸다. `Cannot read properties of undefined (reading 'length')` 라는 메시지만 남아,
# 어느 함수에서 터졌는지도 사용자가 어느 버전을 실행 중인지도 알 수 없었다.
#
# **인증을 걸지 않는다.** 로그인 화면에서도 오류가 날 수 있고, 그때가 오히려 더 중요하다.
# 대신 아래로 남용을 막는다.
CLIENT_ERROR_MAX_BYTES = int(os.getenv("CLIENT_ERROR_MAX_BYTES", "8192"))
CLIENT_ERROR_PER_MINUTE = int(os.getenv("CLIENT_ERROR_PER_MINUTE", "30"))
# {ip: [timestamp, ...]}. 프로세스마다 따로 세므로 실제 상한은 프로세스 수만큼 곱해진다.
# 정확한 제한이 목적이 아니라 로그 폭주를 막는 것이 목적이라 이 정도로 충분하다.
_client_error_hits: dict[str, list[float]] = {}
_client_error_lock = threading.Lock()


def _client_error_allowed(source_ip: str) -> bool:
    now = time.time()
    with _client_error_lock:
        hits = [stamp for stamp in _client_error_hits.get(source_ip, []) if now - stamp < 60]
        if len(hits) >= CLIENT_ERROR_PER_MINUTE:
            _client_error_hits[source_ip] = hits
            return False
        hits.append(now)
        _client_error_hits[source_ip] = hits
        # 오래된 IP 항목을 정리한다. 놔두면 메모리가 계속 자란다.
        if len(_client_error_hits) > 500:
            for key in [k for k, v in _client_error_hits.items() if not v or now - v[-1] > 300]:
                _client_error_hits.pop(key, None)
    return True


@app.post("/api/client-errors")
def client_error_report():
    """화면에서 보낸 오류 리포트를 로그에 남긴다.

    응답 본문을 거의 두지 않는다. 이 라우트가 무엇을 받았는지 알려 주면 탐색에 쓰인다.
    """
    raw = request.get_data(cache=False, as_text=False) or b""
    if len(raw) > CLIENT_ERROR_MAX_BYTES:
        # 조용히 버리지 않고 상태 코드로 알린다. 리포트가 잘려 오는 것을 알아야 한다.
        return ("", 413)

    source_ip = (request.headers.get("X-Forwarded-For", "") or request.remote_addr or "?").split(",")[0].strip()
    if not _client_error_allowed(source_ip):
        return ("", 429)

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ("", 400)
    if not isinstance(payload, dict):
        return ("", 400)

    def field(name: str, limit: int) -> str:
        return str(payload.get(name) or "")[:limit]

    # 화면이 보내는 것만 골라 남긴다. 임의 키를 그대로 로그에 넣으면 그 자체가
    # 로그 오염·주입 경로가 된다.
    trail = payload.get("breadcrumbs")
    trail_text = ""
    if isinstance(trail, list):
        trail_text = " | ".join(
            f"{item.get('kind')}:{str(item.get('detail'))[:80]}"
            for item in trail[-20:]
            if isinstance(item, dict)
        )[:1200]

    extra = payload.get("extra")
    extra_text = json.dumps(extra, ensure_ascii=False)[:600] if isinstance(extra, dict) else ""

    logger.warning(
        "CLIENT ERROR id=%s appVersion=%s where=%s view=%s route=%s name=%s message=%s\n"
        "  stack: %s\n  extra: %s\n  trail: %s\n  ua=%s ip=%s",
        field("id", 12),
        field("appVersion", 40),
        field("where", 80),
        field("view", 40),
        field("route", 120),
        field("name", 40),
        field("message", 500),
        field("stack", 1200).replace("\n", "\n         "),
        extra_text,
        trail_text,
        field("ua", 200),
        source_ip,
    )
    return ("", 204)


@app.get("/api/admin/prompts")
@require_admin
def admin_prompts():
    """역할별 백엔드/프론트엔드 프롬프트 현황.

    백엔드 프롬프트는 **조회만** 된다. 이 라우트에도, 다른 어떤 라우트에도 백엔드를 바꾸는
    경로가 없다(지시서 6-2). 원문은 파이썬 소스 상수이고 배포로만 바뀐다.

    조회를 아예 막지 않는 이유: 무엇이 고정되어 있는지 모르면 관리자가 프론트엔드 칸에
    같은 내용을 다시 적는다. 그러면 지시가 두 번 들어가 서로 상충한다.
    """
    try:
        # force=True: 캐시는 프로세스마다 따로 있어 저장 직후 다른 워커가 옛 값을 준다.
        # 관리자 화면이 방금 저장한 값을 못 보면 저장이 실패한 줄 알고 다시 저장하게 된다.
        rows = prompt_layers.inventory(include_text=True, force=True)
    except (BotoCoreError, ClientError):
        logger.exception("프롬프트 현황 조회 실패")
        return response_error("프롬프트 설정을 불러오지 못했습니다.", 503, "PROMPT_READ_ERROR")
    return jsonify(
        {
            "ok": True,
            "items": rows,
            "maxFrontendChars": prompt_layers.MAX_FRONTEND_CHARS,
            # 지시서 8-2 가 요구하는 누락 목록. 비어 있어야 정상이다.
            "missingBackend": prompt_layers.missing_backend(),
        }
    )


@app.get("/api/admin/prompts/mapping")
@require_admin
def admin_prompt_mapping():
    """지시서 8-2 의 매핑표: 모델명 | 용도 | 백엔드 유무 | 프론트엔드 기본값."""
    try:
        table = prompt_layers.mapping_table()
    except (BotoCoreError, ClientError):
        logger.exception("프롬프트 매핑표 생성 실패")
        return response_error("매핑표를 만들지 못했습니다.", 503, "PROMPT_READ_ERROR")
    missing = prompt_layers.missing_backend()
    return jsonify(
        {
            "ok": True,
            "items": table,
            "missingBackend": missing,
            "complete": not missing,
        }
    )


@app.route("/api/admin/prompts/<path:role_id>", methods=["GET", "PUT"])
@require_admin
def admin_prompt_detail(role_id: str):
    """한 역할의 프론트엔드 프롬프트 조회·저장.

    role_id 에 콜론이 들어간다(`document:analysis`). Flask 의 기본 문자열 변환기는
    슬래시만 막고 콜론은 통과시키지만, `image:{purpose}` 처럼 값이 늘어날 여지가 있어
    `path` 변환기를 쓴다. 여기서 받은 값은 prompt_layers 가 화이트리스트로 검증한다.
    """
    role = prompt_layers.get_role(role_id)
    if not role:
        return response_error(f"'{role_id}'는 없는 용도입니다.", 404, "ROLE_NOT_FOUND")

    if request.method == "GET":
        try:
            # force=True 이유는 admin_prompts 와 같다. 저장 직후 조회가 빈 값을 주면 안 된다.
            stored = prompt_layers.load(force=True).get(role_id) or {}
            history = prompt_layers.history(role_id)
        except (BotoCoreError, ClientError):
            logger.exception("프롬프트 조회 실패 role=%s", role_id)
            return response_error("프롬프트를 불러오지 못했습니다.", 503, "PROMPT_READ_ERROR")
        return jsonify(
            {
                "ok": True,
                "role": role_id,
                "label": role["label"],
                "frontend": str(stored.get("text") or ""),
                "frontendEditable": prompt_layers.is_editable(role_id),
                "updatedAt": stored.get("updatedAt") or "",
                "updatedBy": stored.get("updatedBy") or "",
                "history": history,
            }
        )

    payload = request.get_json(silent=True) or {}
    if "text" not in payload:
        return response_error("text 항목이 필요합니다.", 400, "TEXT_REQUIRED")
    try:
        saved = prompt_layers.save(role_id, str(payload.get("text") or ""), actor=g.username)
    except prompt_layers.PromptError as error:
        # 수정 불가 역할이거나 길이 초과. 사용자에게 그대로 보여줄 수 있는 문구다.
        return response_error(str(error), 400, "PROMPT_REJECTED")
    except (BotoCoreError, ClientError):
        logger.exception("프롬프트 저장 실패 role=%s", role_id)
        return response_error("프롬프트를 저장하지 못했습니다.", 503, "PROMPT_WRITE_ERROR")

    logger.info("프론트엔드 프롬프트 변경 role=%s by=%s", role_id, g.username)
    return jsonify(
        {
            "ok": True,
            "role": role_id,
            "frontend": str(saved.get("text") or ""),
            "updatedAt": saved.get("updatedAt") or "",
            "updatedBy": saved.get("updatedBy") or "",
        }
    )


@app.get("/api/admin/users")
@require_admin
def admin_users():
    return jsonify({"ok": True, "items": graph_store.list_users()})


@app.get("/api/admin/users/<username>/quota")
@require_admin
def admin_get_user_quota(username: str):
    """한 사용자의 한도와 사용량. 조정 화면이 현재 값을 읽는다."""
    if not graph_store.get_user(username):
        return response_error("대상 사용자를 찾을 수 없습니다.", 404, "USER_NOT_FOUND")
    return jsonify(
        {
            "ok": True,
            "username": username,
            "quotas": graph_store.quota_status(username),
            # 무엇에 썼는지 함께 준다. 숫자만 보여 주면 관리자가 초기화 여부를 판단할 근거가 없다.
            "actions": graph_store.credit_actions(username),
        }
    )


@app.patch("/api/admin/users/<username>/quota")
@require_admin
def admin_set_user_quota(username: str):
    """한도를 바꾸거나 사용량을 초기화한다.

    본문 예:
        {"video": 10, "asset": 50}   두 한도를 지정
        {"video": null}              영상만 무제한으로
        {"reset": true}              쓴 건수를 0으로 (한도 값은 그대로)

    **한도 값과 사용량은 별개다.** 한도를 20으로 올려도 이미 쓴 12건은 남아 있어 8건이
    남는다. 다시 처음부터 쓰게 하려면 reset 을 함께 보낸다. 둘을 한 동작으로 묶지 않은
    이유는, 한도를 조금 올리려던 관리자가 사용 기록까지 날리는 일이 생기기 때문이다.
    """
    payload = request.get_json(silent=True) or {}

    quotas: dict = {}
    for category in graph_store.QUOTA_CATEGORIES:
        if category not in payload:
            continue
        raw = payload.get(category)
        if raw is None or raw == "":
            # 명시적으로 비운 것. 그 카테고리를 무제한으로 만든다.
            quotas[category] = None
            continue
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return response_error(
                f"{category} 한도는 숫자여야 합니다.", 400, "INVALID_QUOTA"
            )
        if value < 0:
            return response_error("한도는 0 이상이어야 합니다.", 400, "INVALID_QUOTA")
        if value > 100000:
            return response_error("한도가 너무 큽니다. 100000 이하로 지정해 주세요.", 400, "INVALID_QUOTA")
        quotas[category] = value

    reset = bool(payload.get("reset"))
    if not quotas and not reset:
        return response_error(
            "바꿀 내용이 없습니다. video, asset 또는 reset 을 보내 주세요.", 400, "NO_CHANGES"
        )

    removed = 0
    try:
        if reset:
            removed = graph_store.reset_credits(username)
        if quotas:
            graph_store.set_user_quotas(username, quotas)
        status = graph_store.quota_status(username)
    except graph_store.NotFoundError as error:
        return response_error(str(error), 404, "USER_NOT_FOUND")
    except graph_store.GraphError as error:
        return response_error(str(error), 400, "QUOTA_UPDATE_FAILED")

    logger.info(
        "Admin quota update user=%s by=%s quotas=%s reset=%s removed=%s",
        username,
        g.username,
        quotas,
        reset,
        removed,
    )
    return jsonify({"ok": True, "username": username, "quotas": status, "resetCount": removed})


@app.post("/api/admin/users/deduplicate")
@require_admin
def admin_deduplicate_users():
    """One-off repair for duplicate vertices created before bootstrap was idempotent."""
    removed = graph_store.deduplicate_users()
    logger.info("Deduplicate requested by=%s result=%s", g.username, removed)
    return jsonify({"ok": True, "removed": removed, "items": graph_store.list_users()})


@app.delete("/api/admin/users/<username>")
@require_admin
def admin_delete_user(username: str):
    """Delete a user. `deleteProjects=true` also removes the projects they own.

    Irreversible: the client must show a warning before calling this.
    """
    delete_projects = str(request.args.get("deleteProjects", "false")).lower() == "true"
    if username == g.username:
        return response_error("자기 자신은 삭제할 수 없습니다.", 400, "SELF_DELETE")

    try:
        result = graph_store.delete_user(username, delete_projects)
    except graph_store.NotFoundError as error:
        return response_error(str(error), 404, "USER_NOT_FOUND")
    except graph_store.ConflictError as error:
        return response_error(str(error), 409, "DELETE_REFUSED")

    if delete_projects:
        # 현재 표지와 보관된 이력 이미지를 모두 정리한다.
        for project in result.get("projects", []):
            remove_s3_objects(project.get("coverKeys") or [])

    logger.info(
        "Admin deleted user=%s by=%s deleteProjects=%s", username, g.username, delete_projects
    )
    return jsonify({"ok": True, "result": {k: v for k, v in result.items() if k != "projects"}})


# --------------------------------------------------------------------------- #
# projects
# --------------------------------------------------------------------------- #
@app.route("/api/projects", methods=["GET", "POST"])
@require_auth
def projects():
    if request.method == "GET":
        buckets = graph_store.list_visible_projects(g.username)
        return jsonify(
            {
                "ok": True,
                "shared": [project_payload(p) for p in buckets["shared"]],
                "private": [project_payload(p) for p in buckets["private"]],
            }
        )

    payload = request.get_json(silent=True) or {}
    title = str(payload.get("title", "")).strip()
    story = str(payload.get("story", "")).strip()
    visibility = str(payload.get("visibility", graph_store.VISIBILITY_PRIVATE)).strip()
    input_type = str(payload.get("inputType", graph_store.INPUT_WEBTOON)).strip()
    # 제작 유형. 새 프로젝트는 화면이 반드시 보내지만, 옛 클라이언트가 보내지 않아도
    # 프로젝트 생성이 깨지지 않게 빈 값을 허용한다(하위 호환). 빈 값은 "미선택"이고
    # 화면이 프로젝트에 들어갈 때 물어본다.
    production_type = str(payload.get("productionType", "")).strip()
    if production_type and production_type not in graph_store.PRODUCTION_TYPES:
        return response_error(
            "제작 유형은 영상 만들기 또는 일반 콘티 만들기여야 합니다.",
            400, "INVALID_PRODUCTION_TYPE",
        )
    if not 1 <= len(title) <= 120:
        return response_error("프로젝트 제목은 1~120자여야 합니다.", 400, "INVALID_TITLE")
    if len(story) > 8000:
        return response_error("스토리는 8000자 이하여야 합니다.", 400, "STORY_TOO_LONG")

    try:
        project = graph_store.create_project(
            g.username,
            title,
            story,
            visibility,
            input_type=input_type,
            direction=payload.get("direction") or {},
            production_type=production_type,
        )
    except graph_store.GraphError as error:
        return response_error(str(error), 400, "PROJECT_CREATE_FAILED")
    return jsonify({"ok": True, "item": project_payload(project)}), 201


@app.route("/api/projects/<project_id>", methods=["GET", "PATCH", "DELETE"])
@require_auth
def project_detail(project_id: str):
    project = graph_store.get_project(project_id)
    if not project:
        return response_error("프로젝트를 찾을 수 없습니다.", 404, "PROJECT_NOT_FOUND")
    if not can_read_project(project):
        return response_error("이 프로젝트에 접근할 권한이 없습니다.", 403, "PROJECT_FORBIDDEN")

    if request.method == "GET":
        return jsonify({"ok": True, "item": project_payload(project)})

    if not can_write_project(project):
        return response_error("본인 프로젝트만 수정할 수 있습니다.", 403, "PROJECT_FORBIDDEN")

    if request.method == "DELETE":
        storage_keys = graph_store.delete_project(project_id)
        remove_s3_objects(storage_keys)
        return jsonify({"ok": True, "deleted": project_id})

    payload = request.get_json(silent=True) or {}
    try:
        updated = graph_store.update_project(
            project_id,
            {
                "title": payload.get("title"),
                "story": payload.get("story"),
                "visibility": payload.get("visibility"),
                "inputType": payload.get("inputType"),
                # 옛 프로젝트는 제작 유형이 비어 있다. 화면이 물어본 값을 여기서 채운다.
                # `update_project` 가 허용값을 검증한다.
                "productionType": payload.get("productionType"),
            },
        )
    except graph_store.GraphError as error:
        return response_error(str(error), 400, "PROJECT_UPDATE_FAILED")
    return jsonify({"ok": True, "item": project_payload(updated)})


@app.put("/api/projects/<project_id>/direction")
@require_auth
def project_direction(project_id: str):
    """기획자가 지시하는 방향(타겟, 장르, 화수, 톤앤매너)을 저장한다."""
    project = graph_store.get_project(project_id)
    if not project:
        return response_error("프로젝트를 찾을 수 없습니다.", 404, "PROJECT_NOT_FOUND")
    if not can_write_project(project):
        return response_error("본인 프로젝트만 수정할 수 있습니다.", 403, "PROJECT_FORBIDDEN")

    payload = request.get_json(silent=True) or {}
    try:
        updated = graph_store.set_project_direction(project_id, payload)
    except graph_store.GraphError as error:
        return response_error(str(error), 400, "INVALID_DIRECTION")
    return jsonify({"ok": True, "item": project_payload(updated)})


@app.route("/api/projects/<project_id>/source", methods=["GET", "POST"])
@require_auth
def project_source(project_id: str):
    """진입점 원본 자료. 본문은 S3에 두고 그래프에는 키와 분량만 남긴다."""
    project = graph_store.get_project(project_id)
    if not project:
        return response_error("프로젝트를 찾을 수 없습니다.", 404, "PROJECT_NOT_FOUND")

    if request.method == "GET":
        if not can_read_project(project):
            return response_error("이 프로젝트에 접근할 권한이 없습니다.", 403, "PROJECT_FORBIDDEN")
        source = project.get("source") or {}
        if not source.get("key"):
            return jsonify({"ok": True, "source": None, "text": ""})
        try:
            text = read_source_text(source["key"])
        except (BotoCoreError, ClientError):
            logger.exception("Source fetch failed project=%s", project_id)
            return response_error("원본 자료를 불러오지 못했습니다.", 503, "STORAGE_ERROR")
        return jsonify({"ok": True, "source": source, "text": text})

    if not can_write_project(project):
        return response_error("본인 프로젝트만 수정할 수 있습니다.", 403, "PROJECT_FORBIDDEN")

    try:
        text, name = extract_source_text()
    except ValueError as error:
        return response_error(str(error), 400, "INVALID_SOURCE")

    key = build_source_key(project_id, name)
    try:
        store_bytes(S3_BUCKET, key, text.encode("utf-8"), "text/plain; charset=utf-8")
    except (BotoCoreError, ClientError):
        logger.exception("Source upload failed project=%s", project_id)
        return response_error("원본 자료를 저장하지 못했습니다.", 503, "STORAGE_ERROR")

    updated, stale = graph_store.set_project_source(project_id, key, name, len(text))
    # 이전 자료는 교체되면 쓰이지 않으므로 남겨두지 않는다.
    remove_s3_objects(stale)
    logger.info("Source stored project=%s chars=%s name=%s", project_id, len(text), name)
    return jsonify({"ok": True, "item": project_payload(updated), "charCount": len(text)}), 201


# --------------------------------------------------------------------------- #
# documents (AI 산출물)
# --------------------------------------------------------------------------- #
def build_document_key(owner: str, project_id: str, kind: str) -> str:
    """산출물 본문 키. 소유자 prefix 아래에 둬서 소유자 삭제 시 함께 정리된다."""
    return f"users/{safe_user_prefix(owner)}/documents/{project_id}/{kind}_{uuid.uuid4().hex}.json"


def reap_stale_document(project_id: str, document: dict) -> dict:
    """생성 중 상태로 방치된 산출물을 실패로 정리한다.

    파드가 롤아웃되면 생성 스레드가 사라지지만 그래프에는 generating이 남는다.
    사용자가 영원히 기다리지 않도록 조회 시점에 정리한다.
    """
    if document.get("status") != graph_store.DOC_GENERATING:
        return document
    updated_at = document.get("updatedAt")
    if not updated_at:
        return document
    try:
        started = datetime.fromisoformat(str(updated_at))
    except ValueError:
        return document
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) - started < timedelta(minutes=DOC_STALE_MINUTES):
        return document
    failed = graph_store.fail_document(
        project_id,
        document["kind"],
        f"생성이 {DOC_STALE_MINUTES}분 안에 끝나지 않았습니다. 다시 시도해 주세요.",
    )
    return failed or document


def load_document_context(project_id: str, kind: str) -> dict:
    """이 산출물이 참고해야 하는 앞 단계 결과를 모은다.

    아직 만들지 않은 단계는 건너뛴다. 앞 단계가 없어도 생성은 가능하고, 있으면 품질이 올라간다.
    """
    spec = documents.spec_for(kind)
    context: dict = {}
    for source_kind in spec.get("contextKinds", ()):
        document = graph_store.get_document(project_id, source_kind)
        if not document or document.get("status") != graph_store.DOC_READY or not document.get("s3Key"):
            continue
        try:
            context[source_kind] = json.loads(read_source_text(document["s3Key"]))
        except (BotoCoreError, ClientError, json.JSONDecodeError):
            # 맥락은 있으면 좋은 것이지 필수가 아니다. 실패해도 생성은 진행한다.
            logger.warning("Context document unreadable project=%s kind=%s", project_id, source_kind)
    if context:
        logger.info("Document context project=%s kind=%s uses=%s", project_id, kind, list(context))
    return context


def run_document_generation(
    project_id: str, kind: str, owner: str, actor: str, options: dict | None = None
) -> None:
    """백그라운드 생성. 요청 컨텍스트가 없으므로 필요한 값은 인자로 받는다."""
    try:
        project = graph_store.get_project(project_id)
        if not project:
            graph_store.fail_document(project_id, kind, "프로젝트를 찾을 수 없습니다.")
            return

        source_key = (project.get("source") or {}).get("key")
        source_text = read_source_text(source_key) if source_key else ""

        # 기획자가 적은 세부사항을 자세한 지시로 다시 쓴다. 요청 안에서 하지 않고 여기서 하는
        # 이유는 생성 요청이 202로 즉시 끝나야 하기 때문이다(보강은 몇 초가 걸린다).
        if options and (options.get("details") or "").strip():
            detail_boost = boost_text(
                options["details"],
                prompt_boost.KIND_TREATMENT,
                actor,
                context=f"작품 제목: {project.get('title') or '(제목 미정)'}",
            )
            if detail_boost.get("changed"):
                options = {**options, "details": detail_boost["text"]}
                logger.info(
                    "Treatment details boosted project=%s %s자 -> %s자",
                    project_id,
                    len(detail_boost["original"]),
                    len(detail_boost["text"]),
                )

        # 모든 모델 호출은 게이트웨이를 지나가고, 거기서 사용량이 기록된다.
        caller = gateway.text_caller(documents.TEXT_MODEL, AWS_REGION, actor, f"document:{kind}")
        payload = documents.generate(
            caller,
            kind,
            project,
            source_text,
            context=load_document_context(project_id, kind),
            options=options,
            # 단계마다 진행 신호를 남긴다. 없으면 여러 번 호출하는 산출물이
            # 아직 쓰고 있는데도 reap_stale_document 에 걸려 실패로 바뀐다.
            on_progress=lambda: graph_store.touch_document(project_id, kind),
        )
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")

        key = build_document_key(owner, project_id, kind)
        store_bytes(S3_BUCKET, key, body, "application/json; charset=utf-8")
        graph_store.complete_document(
            project_id, kind, key, len(body), documents.document_title(kind, payload)
        )
        # 본문이 저장되고 상태가 ready 가 된 뒤에 차감한다. 아래 except 로 떨어지는 경로는
        # 결과물이 없으므로 한도를 먹지 않는다. refId 를 S3 키로 두면 재생성마다 새 키가
        # 생겨 각각 1건으로 세어지고, 같은 생성이 두 번 세어지지는 않는다.
        #
        # 차감 대상은 요청한 사람(actor)이다. owner 는 프로젝트 주인이라 공유 프로젝트에서
        # 남의 한도가 줄어들 수 있다.
        spend_quota(actor, QUOTA_ASSET, f"document:{kind}", key)
        logger.info("Document generated project=%s kind=%s actor=%s", project_id, kind, actor)
    except documents.DocumentError as error:
        graph_store.fail_document(project_id, kind, str(error))
    except ClientError as error:
        code = error.response.get("Error", {}).get("Code", "ClientError")
        message = (
            f"생성 모델을 사용할 수 없습니다({code}). 관리자에게 문의해 주세요."
            if code in {"AccessDeniedException", "ValidationException"}
            else f"생성 중 오류가 발생했습니다({code})."
        )
        logger.warning("Document generation refused project=%s kind=%s code=%s", project_id, kind, code)
        graph_store.fail_document(project_id, kind, message)
    except (BotoCoreError, graph_store.GraphError, ValueError) as error:
        logger.exception("Document generation failed project=%s kind=%s", project_id, kind)
        graph_store.fail_document(project_id, kind, f"생성에 실패했습니다: {error}")
    except Exception as error:  # noqa: BLE001 - 스레드에서 예외가 새면 상태가 generating으로 굳는다
        logger.exception("Document generation crashed project=%s kind=%s", project_id, kind)
        graph_store.fail_document(project_id, kind, f"예상치 못한 오류: {error}")


@app.get("/api/document-kinds")
@require_auth
def document_kinds():
    """진입점별로 만들 수 있는 산출물 목록. 프론트가 단계 UI를 그릴 때 쓴다."""
    return jsonify({"ok": True, "items": documents.describe_kinds()})


@app.get("/api/projects/<project_id>/documents")
@require_auth
def project_documents(project_id: str):
    project = graph_store.get_project(project_id)
    if not project:
        return response_error("프로젝트를 찾을 수 없습니다.", 404, "PROJECT_NOT_FOUND")
    if not can_read_project(project):
        return response_error("이 프로젝트에 접근할 권한이 없습니다.", 403, "PROJECT_FORBIDDEN")

    items = [reap_stale_document(project_id, item) for item in graph_store.list_documents(project_id)]
    return jsonify(
        {
            "ok": True,
            "items": items,
            "allowedKinds": documents.allowed_kinds(project.get("inputType") or ""),
        }
    )


@app.post("/api/projects/<project_id>/documents/<kind>/generate")
@require_auth
@require_quota(QUOTA_ASSET)
def generate_document(project_id: str, kind: str):
    """생성을 시작하고 즉시 202로 응답한다. 완료는 폴링으로 확인한다."""
    project = graph_store.get_project(project_id)
    if not project:
        return response_error("프로젝트를 찾을 수 없습니다.", 404, "PROJECT_NOT_FOUND")
    if not can_write_project(project):
        return response_error("본인 프로젝트만 수정할 수 있습니다.", 403, "PROJECT_FORBIDDEN")

    try:
        spec = documents.spec_for(kind)
    except documents.DocumentError as error:
        return response_error(str(error), 400, "INVALID_DOCUMENT_KIND")

    input_type = project.get("inputType") or ""
    if input_type not in spec["inputTypes"]:
        labels = ", ".join(spec["inputTypes"])
        return response_error(
            f"'{spec['label']}'은(는) 진입점 {labels}에서만 만들 수 있습니다.", 400, "KIND_NOT_ALLOWED"
        )

    if spec["requiresSource"] and not (project.get("source") or {}).get("key"):
        return response_error(
            "먼저 원본 자료를 저장해 주세요. AI가 읽을 내용이 없습니다.", 400, "SOURCE_REQUIRED"
        )

    # 생성 옵션. 지금은 트리트먼트만 받는다(분량·상세도·세부사항).
    # 검증 규칙은 documents 쪽 normalize 함수 한 곳에 두어 화면과 서버가 어긋나지 않게 한다.
    options: dict = {}
    if spec.get("options"):
        body = request.get_json(silent=True) or {}
        try:
            options = documents.normalize_treatment_options(body.get("options") or body)
        except documents.DocumentError as error:
            return response_error(str(error), 400, "INVALID_DOCUMENT_OPTIONS")

    existing = graph_store.get_document(project_id, kind)
    if existing:
        existing = reap_stale_document(project_id, existing)
        if existing.get("status") == graph_store.DOC_GENERATING:
            return response_error("이미 생성 중입니다. 잠시만 기다려 주세요.", 409, "ALREADY_GENERATING")

    document, stale_key = graph_store.begin_document(
        project_id, kind, documents.TEXT_MODEL, g.username, options=options
    )
    # 이전 본문은 새 결과로 대체되므로 남겨두지 않는다.
    if stale_key:
        remove_s3_objects([stale_key])

    owner = project.get("owner") or g.username
    thread = threading.Thread(
        target=run_document_generation,
        args=(project_id, kind, owner, g.username, options),
        name=f"doc-{kind}-{project_id[:8]}",
        daemon=True,
    )
    thread.start()
    return jsonify({"ok": True, "item": document}), 202


@app.route("/api/projects/<project_id>/documents/<kind>", methods=["GET", "PUT", "DELETE"])
@require_auth
def project_document(project_id: str, kind: str):
    project = graph_store.get_project(project_id)
    if not project:
        return response_error("프로젝트를 찾을 수 없습니다.", 404, "PROJECT_NOT_FOUND")

    if request.method == "GET":
        if not can_read_project(project):
            return response_error("이 프로젝트에 접근할 권한이 없습니다.", 403, "PROJECT_FORBIDDEN")
        document = graph_store.get_document(project_id, kind)
        if not document:
            return response_error("산출물이 아직 없습니다.", 404, "DOCUMENT_NOT_FOUND")
        document = reap_stale_document(project_id, document)

        content = None
        if document.get("s3Key") and document.get("status") == graph_store.DOC_READY:
            try:
                content = json.loads(read_source_text(document["s3Key"]))
            except (BotoCoreError, ClientError, json.JSONDecodeError):
                logger.exception("Document fetch failed project=%s kind=%s", project_id, kind)
                return response_error("산출물을 불러오지 못했습니다.", 503, "STORAGE_ERROR")
        return jsonify({"ok": True, "item": document, "content": content})

    if not can_write_project(project):
        return response_error("본인 프로젝트만 수정할 수 있습니다.", 403, "PROJECT_FORBIDDEN")

    if request.method == "DELETE":
        try:
            stale_key = graph_store.delete_document(project_id, kind)
        except graph_store.NotFoundError as error:
            return response_error(str(error), 404, "DOCUMENT_NOT_FOUND")
        remove_s3_objects([stale_key] if stale_key else [])
        return jsonify({"ok": True, "deleted": kind})

    # PUT: 기획자가 AI 결과를 고쳐서 저장한다. 인물 설정처럼 사람이 확정해야 하는 항목에 쓴다.
    payload = request.get_json(silent=True) or {}
    content = payload.get("content")
    if not isinstance(content, dict) or not content:
        return response_error("content는 비어 있지 않은 객체여야 합니다.", 400, "INVALID_CONTENT")
    try:
        documents.spec_for(kind)
    except documents.DocumentError as error:
        return response_error(str(error), 400, "INVALID_DOCUMENT_KIND")

    body = json.dumps(content, ensure_ascii=False, indent=2).encode("utf-8")
    if len(body) > 400_000:
        return response_error("산출물이 너무 큽니다.", 400, "CONTENT_TOO_LARGE")

    existing = graph_store.get_document(project_id, kind)
    owner = project.get("owner") or g.username
    key = build_document_key(owner, project_id, kind)
    try:
        store_bytes(S3_BUCKET, key, body, "application/json; charset=utf-8")
    except (BotoCoreError, ClientError):
        logger.exception("Document save failed project=%s kind=%s", project_id, kind)
        return response_error("산출물을 저장하지 못했습니다.", 503, "STORAGE_ERROR")

    saved = graph_store.save_document(
        project_id, kind, key, len(body), documents.document_title(kind, content), g.username
    )
    if existing and existing.get("s3Key") and existing["s3Key"] != key:
        remove_s3_objects([existing["s3Key"]])
    return jsonify({"ok": True, "item": saved, "content": content})


@app.post("/api/projects/<project_id>/cover")
@require_auth
def upload_project_cover(project_id: str):
    """Manual cover: the user supplies the artwork."""
    project = graph_store.get_project(project_id)
    if not project:
        return response_error("프로젝트를 찾을 수 없습니다.", 404, "PROJECT_NOT_FOUND")
    if not can_write_project(project):
        return response_error("본인 프로젝트만 수정할 수 있습니다.", 403, "PROJECT_FORBIDDEN")
    if "file" not in request.files:
        return response_error("표지 이미지가 필요합니다.", 400, "FILE_REQUIRED")

    try:
        item = upload_file(request.files["file"], "covers", ALLOWED_IMAGE_TYPES, MAX_IMAGE_BYTES)
    except ValueError as error:
        return response_error(str(error), 400, "INVALID_FILE")
    except (BotoCoreError, ClientError):
        logger.exception("Cover upload failed project=%s", project_id)
        return response_error("표지를 저장하지 못했습니다.", 503, "STORAGE_ERROR")

    updated = apply_new_cover(project_id, item["key"], "upload")
    return jsonify({"ok": True, "item": project_payload(updated)}), 201


@app.post("/api/projects/<project_id>/cover/restore")
@require_auth
def restore_project_cover(project_id: str):
    """보관된 이전 표지를 다시 현재 표지로 지정한다."""
    project = graph_store.get_project(project_id)
    if not project:
        return response_error("프로젝트를 찾을 수 없습니다.", 404, "PROJECT_NOT_FOUND")
    if not can_write_project(project):
        return response_error("본인 프로젝트만 수정할 수 있습니다.", 403, "PROJECT_FORBIDDEN")

    payload = request.get_json(silent=True) or {}
    cover_key = str(payload.get("coverKey", ""))
    if not cover_key:
        return response_error("되돌릴 표지를 지정해 주세요.", 400, "COVER_KEY_REQUIRED")

    try:
        updated = graph_store.restore_project_cover(project_id, cover_key)
    except graph_store.NotFoundError as error:
        return response_error(str(error), 404, "COVER_NOT_IN_HISTORY")

    logger.info("Cover restored project=%s by=%s key=%s", project_id, g.username, cover_key)
    return jsonify({"ok": True, "item": project_payload(updated)})


@app.post("/api/projects/<project_id>/cover/generate")
@require_auth
@require_quota(QUOTA_ASSET)
def generate_project_cover(project_id: str):
    """AI cover: Claude turns the story into an image prompt, Stability paints it."""
    project = graph_store.get_project(project_id)
    if not project:
        return response_error("프로젝트를 찾을 수 없습니다.", 404, "PROJECT_NOT_FOUND")
    if not can_write_project(project):
        return response_error("본인 프로젝트만 수정할 수 있습니다.", 403, "PROJECT_FORBIDDEN")

    story = (project.get("story") or "").strip()
    if not story:
        return response_error(
            "표지를 생성할 스토리가 없습니다. 먼저 프로젝트 스토리를 입력해 주세요.", 400, "STORY_REQUIRED"
        )

    aspect = "9:16" if project.get("visibility") == graph_store.VISIBILITY_PRIVATE else "2:3"
    # 표지는 Bedrock 을 두 번 부른다. 텍스트 모델과 이미지 모델은 **다른 모델, 다른
    # 리전, 다른 접근 권한**이다. 두 호출을 한 try 로 묶으면 어느 쪽이 거부됐는지
    # 응답에도 로그에도 남지 않는다. 실제로 그 때문에 원인 파악이 오래 걸렸다.
    try:
        prompt = build_cover_prompt(project.get("title") or "", story, g.username)
    except ClientError as error:
        return cover_call_failed(error, project_id, "표지 문구", COVER_TEXT_MODEL, AWS_REGION)
    except (BotoCoreError, ValueError) as error:
        logger.exception("Cover prompt failed project=%s", project_id)
        return response_error(f"표지 생성에 실패했습니다: {error}", 502, "COVER_FAILED")

    try:
        model = image_model()
    except model_registry.CatalogError as error:
        logger.error("Cover image model unavailable project=%s: %s", project_id, error)
        return response_error(str(error), 503, "COVER_MODEL_DENIED")

    try:
        image_bytes = render_cover_image(model, prompt, aspect, g.username)
    except ClientError as error:
        return cover_call_failed(error, project_id, "표지 그림", model["modelId"], model["region"])
    except (BotoCoreError, ValueError, providers.ProviderError) as error:
        logger.exception("Cover image failed project=%s model=%s", project_id, model["modelId"])
        return response_error(f"표지 생성에 실패했습니다: {error}", 502, "COVER_FAILED")

    now = datetime.now(timezone.utc)
    key = (
        f"users/{safe_user_prefix(g.username)}/covers/{now:%Y/%m/%d}/"
        f"{uuid.uuid4().hex}_ai-cover.png"
    )
    try:
        store_bytes(S3_BUCKET, key, image_bytes, "image/png", AWS_REGION)
    except (BotoCoreError, ClientError):
        logger.exception("Cover store failed project=%s", project_id)
        return response_error("생성한 표지를 저장하지 못했습니다.", 503, "STORAGE_ERROR")

    updated = apply_new_cover(project_id, key, "ai")
    spend_quota(g.username, QUOTA_ASSET, "cover", key)
    logger.info("AI cover generated project=%s by=%s", project_id, g.username)
    return jsonify({"ok": True, "item": project_payload(updated), "prompt": prompt}), 201


def cover_call_failed(error: ClientError, project_id: str, stage: str, model_id: str, region: str):
    """표지 생성 중 Bedrock 이 거부했을 때의 응답.

    **모델 ID 와 리전을 문구에 넣는다.** 토스트에는 이미 배포 버전이 붙으므로,
    이것까지 있으면 스크린샷 한 장으로 "어느 코드가 어느 모델을 어느 리전에서 불렀다가
    거부됐다"까지 확정된다. 관리자에게 문의하라고만 적혀 있으면 문의를 받은 쪽도
    클러스터 로그를 뒤져야 한다.
    """
    code = error.response.get("Error", {}).get("Code", "ClientError")
    message = error.response.get("Error", {}).get("Message", "")[:300]
    logger.warning(
        "Cover generation refused project=%s stage=%s model=%s region=%s code=%s message=%s",
        project_id,
        stage,
        model_id,
        region,
        code,
        message,
    )
    if code in {"AccessDeniedException", "ValidationException", "UnrecognizedClientException"}:
        return response_error(
            f"{stage} 모델 '{model_id}'을(를) {region} 리전에서 호출할 수 없습니다({code}). "
            "관리자에게 문의해 주세요.",
            403,
            "COVER_MODEL_DENIED",
        )
    return response_error("표지 생성에 실패했습니다.", 502, "COVER_FAILED")


def build_cover_prompt(title: str, story: str, actor: str) -> str:
    """Ask Claude for a short English image prompt describing the poster."""
    instruction = (
        "You write image prompts for webtoon-to-drama poster art. "
        "Read the story and reply with ONE English prompt under 60 words. "
        "Describe subject, mood, lighting, colour palette and composition. "
        "No text, no letters, no watermarks in the image. Reply with the prompt only."
    )
    prompt, _ = gateway.converse(
        COVER_TEXT_MODEL,
        AWS_REGION,
        instruction,
        [{"text": f"Title: {title}\n\nStory:\n{story[:4000]}"}],
        max_tokens=200,
        temperature=0.7,
        actor=actor,
        purpose="cover:prompt",
        long_running=False,
    )
    if not prompt:
        raise ValueError("표지 프롬프트를 생성하지 못했습니다.")
    return prompt[:900]


def render_cover_image(model: dict, prompt: str, aspect_ratio: str, actor: str) -> bytes:
    """표지 이미지를 생성한다. 모델은 호출부가 고른다(오류 문구에 모델을 적기 위해서다)."""
    body = providers.resolve(model).build_request(
        model,
        {
            "prompt": f"{prompt}. Poster illustration, cinematic, highly detailed, no text.",
            "aspectRatio": aspect_ratio,
        },
    )
    images = gateway.invoke_image(model, body, actor=actor, purpose="cover:image")
    return images[0]


def image_model(tier: str | None = None, style_reference: bool = False) -> dict:
    """이미지 생성에 쓸 모델. 카탈로그가 단일 원본이다.

    그림체 참조 이미지가 있으면 Style Guide 계열로 보낸다. 프롬프트로는 특정 IP의
    그림체를 재현할 수 없기 때문이다. 그 외에는 요청한 품질 등급으로 고른다.

    **여기에 폴백 모델을 두지 않는다.** 예전에는 카탈로그가 비면 환경변수로 만든
    모델 정의를 돌려주었다. 그것이 이 버그의 절반이었다. 카탈로그가 통째로 거부되고
    있는데도 표지 생성은 계속 시도됐고, 관리자가 지정한 적 없는 모델을 불러 권한
    오류를 냈다. 화면에는 모델 접근 문제로만 보였고 카탈로그가 비었다는 사실은
    어디에도 드러나지 않았다.

    지금은 `model_registry.BUILTIN_MODELS` 가 kind 마다 모델을 하나씩 보장한다.
    그래도 비어 있다면 그것은 설정이 깨진 것이고, 조용히 다른 모델로 바꿔치기하지 말고
    거부 사유를 그대로 올려야 한다.
    """
    if style_reference:
        return model_registry.resolve_for_generation(
            None, kind="image", provider="stability.style-guide"
        )
    return model_registry.resolve_for_generation(None, kind="image", tier=tier)


# --------------------------------------------------------------------------- #
# images (콘티 · 장면 이미지)
# --------------------------------------------------------------------------- #
def style_router_caller(actor: str):
    """그림체 판단용 호출자. 판단만 하는 짧은 작업이라 값싼 모델을 쓴다."""
    return gateway.text_caller(STYLE_ROUTER_MODEL, AWS_REGION, actor, "style:route")


def prompt_boost_caller(actor: str, kind: str):
    """사용자 입력 보강용 호출자. 짧은 작업이라 long_running을 쓰지 않는다."""
    return gateway.text_caller(PROMPT_BOOST_MODEL, AWS_REGION, actor, f"boost:{kind}")


def boost_text(text: str, kind: str, actor: str, context: str = "") -> dict:
    """사용자가 적은 설명을 자세하게 다시 쓴다. 실패하면 원문을 그대로 돌려준다."""
    return prompt_boost.boost(
        prompt_boost_caller(actor, kind), text, kind, context=context
    )


def boost_view(results: dict[str, dict]) -> dict:
    """보강 결과를 응답에 담을 형태로 줄인다. 바뀐 항목만 남긴다."""
    view = {}
    for name, result in (results or {}).items():
        if not result or not result.get("changed"):
            continue
        view[name] = {
            "original": result.get("original", ""),
            "text": result.get("text", ""),
            "degraded": bool(result.get("degraded")),
        }
    return view


def boost_many(items: list[tuple[str, str, str]], actor: str) -> dict[str, dict]:
    """여러 항목을 동시에 보강한다. `items`는 (이름, 원문, 종류) 목록이다.

    순차로 부르면 항목마다 몇 초가 쌓여 사용자가 기다리는 시간이 그대로 늘어난다.
    영상 생성 요청은 이 단계 뒤에 그림체 판단과 영상 프롬프트 작성이 또 이어진다.
    서로 독립된 호출이라 함께 보내는 것이 맞다.
    """
    from concurrent.futures import ThreadPoolExecutor

    results: dict[str, dict] = {}
    pending = [(name, value, kind) for name, value, kind in items if (value or "").strip()]
    if not pending:
        return results
    with ThreadPoolExecutor(max_workers=len(pending)) as pool:
        futures = {
            name: pool.submit(boost_text, value, kind, actor) for name, value, kind in pending
        }
        for name, future in futures.items():
            try:
                results[name] = future.result()
            except Exception as error:  # noqa: BLE001 - 보강 실패가 생성을 막아서는 안 된다
                logger.warning("보강 작업이 실패했습니다 (%s): %s", name, error)
    return results


_ENGLISH_ONLY = re.compile(r"^[\x00-\x7F]+$")


def to_visual_english(scene_text: str, actor: str) -> str:
    """장면 설명을 영어 시각 묘사로 옮긴다.

    Stability 모델은 영어로 학습되어 한국어 프롬프트를 넣으면 묘사가 뭉개진다.
    실제로 한국어 장면을 그대로 넣었을 때 안전 필터에 걸리기도 했다.
    이미 영어면 호출하지 않는다. 불필요한 비용을 쓰지 않기 위한 것이다.
    """
    text = (scene_text or "").strip()
    if not text or _ENGLISH_ONLY.match(text):
        return text

    instruction = (
        "You convert Korean scene notes into one English visual description for an image model.\n"
        "Rules:\n"
        "1. Reply with ONE English sentence or two, 15-60 words, nothing else.\n"
        "2. Describe only what is visible: subject, clothing, action, setting, time of day, lighting.\n"
        "3. Keep it concrete and neutral. Do not add story, dialogue or emotion words.\n"
        "4. Describe characters in their twenties as 'young woman' or 'young man' unless the note "
        "clearly says otherwise. Never mention ages, and never describe bodies in detail.\n"
        "5. Do not add an art style; that is handled separately."
    )
    try:
        english, _ = gateway.converse(
            STYLE_ROUTER_MODEL,
            AWS_REGION,
            instruction,
            [{"text": text[:2000]}],
            max_tokens=300,
            temperature=0.2,
            actor=actor,
            purpose="image:translate",
            long_running=False,
        )
    except (BotoCoreError, ClientError):
        # 번역이 실패해도 생성은 진행한다. 품질이 떨어질 뿐 막을 이유는 없다.
        logger.warning("장면 설명 영어 변환에 실패해 원문을 사용합니다.")
        return text
    english = " ".join(english.split())
    return english or text


# 사용자가 참조 이미지로 쓸 수 있는 키의 갈래. 직접 올린 것, AI로 만든 것, 표지 모두 본인 것이다.
#
# `video` 를 넣은 이유 (실측). 원본 업로드는 올린 이미지를 컷으로 나눠
# `users/<나>/video/<프로젝트>/cuts/*.png` 에 다시 쓴다. 이해 분석과 영상 생성은 그 **컷**
# 키를 참조하는데, 목록에 `video` 가 없어서 본인 컷을 쓰려는 요청이 ASSET_FORBIDDEN 으로
# 막혔다. 앱이 사용자 접두어 아래에 쓴 자산이므로 본인 것이다.
OWNED_IMAGE_CATEGORIES = ("uploads", "images", "covers", "video")


def owned_image_prefixes(actor: str) -> tuple[str, ...]:
    base = f"users/{safe_user_prefix(actor)}/"
    return tuple(f"{base}{category}/" for category in OWNED_IMAGE_CATEGORIES)


def is_owned_image_key(key: str, actor: str) -> bool:
    """이 키가 본인이 올리거나 만든 이미지인지 확인한다.

    같은 판단을 세 곳에 따로 적어 두었더니 허용 범위가 갈렸다. 영상 생성만 `uploads/`로 좁혀서
    AI로 만든 이미지(`images/`)와 콘티 이미지를 첫 프레임으로 쓸 수 없었다. 한 곳에서 판단한다.
    """
    return bool(key) and key.startswith(owned_image_prefixes(actor))


# 자산 검증 결과. 호출부가 상태코드·문구를 정한다.
ASSET_OK = "ok"
ASSET_FORBIDDEN = "forbidden"
ASSET_MISSING = "missing"
ASSET_UNAVAILABLE = "unavailable"


def check_owned_asset(key: str, actor: str) -> str:
    """이미지 참조가 **쓸 수 있는 것인지** 확인한다. 키 모양만 보지 않는다.

    ## 왜 존재 확인이 필요한가

    `is_owned_image_key` 는 접두어만 본다. 그래서 `users/<나>/uploads/없는파일.png` 가
    통과했고, 장면이 만들어진 뒤 컷 등록·렌더 단계에서야 실패했다(실측: `POST
    /api/scenes` 가 201 을 줬다). 유효하지 않은 입력으로 장면이 생기면 그 뒤 어느
    단계에서 깨지는지 사용자가 알기 어렵다.

    ## 순서가 중요하다

    **소유권을 먼저 본다.** 존재 확인을 먼저 하면 남의 키를 넣어 보고 응답 차이로
    **다른 사용자의 자산 존재 여부를 알아낼 수 있다.** 소유가 아니면 존재를 확인하지
    않고 그대로 `forbidden` 이다 — 있는지 없는지 구분해서 알려 주지 않는다.

    내부적으로는 `missing` 과 `forbidden` 을 구분한다(로그·문구가 달라야 한다).
    """
    if not is_owned_image_key(key, actor):
        return ASSET_FORBIDDEN
    try:
        s3.head_object(Bucket=S3_BUCKET, Key=key)
    except ClientError as error:
        code = error.response.get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchKey", "NotFound"):
            return ASSET_MISSING
        logger.warning("자산 확인 실패 key=%s code=%s", key, code)
        return ASSET_UNAVAILABLE
    except BotoCoreError:
        logger.warning("자산 확인 연결 실패 key=%s", key)
        return ASSET_UNAVAILABLE
    return ASSET_OK


def asset_error_response(key: str, verdict: str):
    """`check_owned_asset` 결과를 사용자 응답으로 바꾼다."""
    if verdict == ASSET_FORBIDDEN:
        return response_error(
            "본인이 올리거나 만든 이미지만 사용할 수 있습니다.", 403, "ASSET_FORBIDDEN"
        )
    if verdict == ASSET_UNAVAILABLE:
        return response_error(
            "이미지 저장소에서 자산을 확인하지 못했습니다.",
            503,
            "ASSET_STORAGE_UNAVAILABLE",
            retryable=True,
            action="잠시 뒤 다시 시도해 주세요.",
        )
    return response_error(
        f"이미지를 찾을 수 없습니다: {key.rsplit('/', 1)[-1]}. "
        "삭제되었거나 업로드가 끝나지 않았을 수 있습니다. 다시 올려 주세요.",
        404,
        "ASSET_NOT_FOUND",
    )


def load_style_reference(reference_key: str, actor: str) -> str:
    """그림체 참조 이미지를 base64로 읽는다. 본인 것만 허용한다."""
    if not is_owned_image_key(reference_key, actor):
        raise PermissionError("본인이 업로드하거나 생성한 이미지만 참조할 수 있습니다.")
    obj = s3.get_object(Bucket=S3_BUCKET, Key=reference_key)
    prepared, _, _, _ = prepare_reference_image(obj["Body"].read())
    return base64.b64encode(prepared).decode("ascii")


@app.get("/api/styles")
@require_auth
def list_styles():
    """그림체 프리셋과 품질 등급. 프론트가 선택 UI를 그릴 때 쓴다."""
    return jsonify({"ok": True, **styles.describe()})


@app.route("/api/images", methods=["GET", "DELETE"])
@require_auth
def images():
    if request.method == "GET":
        return jsonify({"ok": True, "items": list_objects("images", {".png", ".jpg", ".jpeg"})})

    key = str((request.get_json(silent=True) or {}).get("key") or request.args.get("key") or "").strip()
    if not key:
        return response_error("삭제할 이미지 키가 필요합니다.", 400, "KEY_REQUIRED")
    if not key.startswith(f"users/{safe_user_prefix(g.username)}/images/"):
        return response_error("본인이 생성한 이미지만 삭제할 수 있습니다.", 403, "IMAGE_FORBIDDEN")
    try:
        s3.delete_object(Bucket=S3_BUCKET, Key=key)
    except (BotoCoreError, ClientError):
        logger.exception("Image delete failed key=%s", key)
        return response_error("이미지를 삭제하지 못했습니다.", 503, "STORAGE_ERROR")
    logger.info("Image deleted user=%s key=%s", g.username, key)
    return jsonify({"ok": True, "deleted": key})


def image_client_error_response(error: ClientError, model: dict):
    info = error.response.get("Error", {})
    code = str(info.get("Code") or "ClientError")
    message = str(info.get("Message") or "")
    logger.warning(
        "Image provider error request=%s alias=%s model=%s region=%s code=%s detail=%s",
        current_request_id(), model.get("id"), model.get("modelId"), model.get("region"),
        code, " ".join(message.split())[:300],
    )
    if code in {"AccessDeniedException", "UnrecognizedClientException"}:
        return response_error(
            "현재 이미지 모델을 사용할 권한이 준비되지 않았습니다.", 503,
            "MODEL_ACCESS_DENIED", action="관리자가 모델 접근 권한과 앱 역할을 확인해야 합니다.",
        )
    if code in {"ResourceNotFoundException", "ModelNotFoundException"}:
        return response_error(
            "설정된 이미지 모델을 해당 리전에서 찾을 수 없습니다.", 503,
            "MODEL_ID_INVALID", action="관리자가 모델 ID와 리전 설정을 확인해야 합니다.",
        )
    if code == "ValidationException":
        lowered = message.lower()
        if any(token in lowered for token in (
            "model identifier", "not found", "not supported in this region", "invalid model",
        )):
            return response_error(
                "설정된 이미지 모델과 리전 조합이 올바르지 않습니다.", 503,
                "MODEL_ID_INVALID", action="관리자가 modelId와 region을 확인해야 합니다.",
            )
        if "on-demand" in lowered and "not supported" in lowered:
            return response_error(
                "이 이미지 모델은 현재 호출 방식으로 사용할 수 없습니다.", 503,
                "MODEL_INVOCATION_UNSUPPORTED", action="관리자가 추론 프로파일을 확인해야 합니다.",
            )
        return response_error(
            "이미지 모델이 요청 형식을 거부했습니다.", 400, "MODEL_REQUEST_INVALID",
            action="화면 비율이나 참조 이미지를 바꿔 다시 시도해 주세요.",
        )
    if code in {"ThrottlingException", "TooManyRequestsException", "ServiceQuotaExceededException"}:
        return response_error(
            "이미지 생성 요청이 몰려 잠시 대기해야 합니다.", 429, "MODEL_BUSY",
            retryable=True, action="잠시 뒤 같은 요청으로 다시 시도해 주세요.",
        )
    if code in {"ModelTimeoutException", "InternalServerException", "ServiceUnavailableException"}:
        return response_error(
            "이미지 모델이 일시적으로 응답하지 않습니다.", 503, "MODEL_TEMPORARY_FAILURE",
            retryable=True, action="잠시 뒤 다시 시도해 주세요.",
        )
    return response_error(
        "이미지 생성 요청을 처리하지 못했습니다.", 502, "IMAGE_PROVIDER_FAILED",
        action="오류 ID를 관리자에게 전달해 주세요.",
    )


@app.post("/api/images/generate")
@require_auth
@require_quota(QUOTA_ASSET)
def generate_image():
    """장면 설명 + 그림체 지시로 이미지를 만든다(콘티, 장면, 키비주얼).

    그림체는 사용자가 한국어로 적고, 라우터가 프리셋과 모델 등급을 판단한다.
    판단 결과를 응답에 담아 사용자가 무엇이 적용됐는지 볼 수 있게 한다.
    """
    payload = request.get_json(silent=True) or {}
    scene_prompt = str(payload.get("prompt") or "").strip()
    if not 5 <= len(scene_prompt) <= 4000:
        return response_error("장면 설명은 5~4000자여야 합니다.", 400, "INVALID_PROMPT")

    purpose = str(payload.get("purpose") or "scene")
    if purpose not in styles.PURPOSE_TIERS:
        return response_error("purpose 값이 올바르지 않습니다.", 400, "INVALID_PURPOSE")

    style_text = str(payload.get("stylePrompt") or "")
    reference_key = str(payload.get("referenceKey") or "").strip()

    # 자산 소유권·존재·완전 디코딩을 모든 유료 prompt 보강보다 먼저 한다.
    reference_base64 = None
    if reference_key:
        verdict = check_owned_asset(reference_key, g.username)
        if verdict != ASSET_OK:
            return asset_error_response(reference_key, verdict)
        try:
            reference_base64 = load_style_reference(reference_key, g.username)
        except PermissionError as error:
            return response_error(str(error), 403, "REFERENCE_FORBIDDEN")
        except (BotoCoreError, ClientError):
            return response_error(
                "참조 이미지를 읽지 못했습니다.", 400, "REFERENCE_UNREADABLE",
                action="참조 이미지를 다시 업로드해 주세요.",
            )
        except Exception:  # noqa: BLE001 - Pillow 예외 종류가 다양하다
            return response_error(
                "참조 이미지가 손상되어 해석하지 못했습니다.", 400, "REFERENCE_UNREADABLE",
                action="PNG, JPG 또는 WEBP 파일을 다시 업로드해 주세요.",
            )

    # 자산이 유효한 뒤에만 유료 텍스트 보강을 수행한다.
    boosted = boost_many(
        [("scene", scene_prompt, prompt_boost.KIND_SCENE), ("style", style_text, prompt_boost.KIND_STYLE)],
        g.username,
    )
    scene_boost = boosted.get("scene") or {}
    style_boost = boosted.get("style") or {}
    scene_prompt = scene_boost.get("text") or scene_prompt
    style_text = style_boost.get("text") or style_text

    # 프리셋을 직접 고른 경우에는 AI 판단을 건너뛴다. 사용자의 명시적 선택이 우선이다.
    forced_preset = str(payload.get("presetId") or "").strip()
    if forced_preset:
        decision = styles.route("", purpose=purpose, has_reference=bool(reference_base64))
        preset = styles.get_preset(forced_preset)
        decision.update(
            {
                "presetId": preset["id"],
                "presetLabel": preset["label"],
                "lead": preset["lead"],
                "stylePrompt": preset["prompt"],
                "negativePrompt": f"{preset['negative']}, {styles.BASE_NEGATIVE}",
                "source": "manual",
                "reason": "기획자가 프리셋을 직접 선택했습니다.",
            }
        )
        # 직접 선택한 경우에도 프리셋의 최소 등급은 지킨다.
        decision.update(
            styles.apply_min_tier(preset, styles.PURPOSE_TIERS.get(purpose, "standard"), decision["reason"])
        )
        if style_text.strip():
            decision["stylePrompt"] += f", {style_text.strip()[:300]}"
    else:
        decision = styles.route(
            style_text,
            purpose=purpose,
            has_reference=bool(reference_base64),
            caller=style_router_caller(g.username),
        )

    try:
        model = image_model(tier=decision["tier"], style_reference=bool(reference_base64))
    except model_registry.CatalogError as error:
        return response_error(str(error), 400, "MODEL_UNAVAILABLE")

    # 한국어 장면 설명은 영어 시각 묘사로 옮긴 뒤 그림체와 합친다.
    english_scene = to_visual_english(scene_prompt, g.username)
    generation_request = {
        "prompt": styles.compose_prompt(english_scene, decision),
        "negativePrompt": decision["negativePrompt"],
        "aspectRatio": payload.get("aspectRatio") or "16:9",
        "referenceBase64": reference_base64,
        "styleFidelity": payload.get("styleFidelity"),
        "seed": payload.get("seed"),
    }

    try:
        adapter = providers.resolve(model)
        body = adapter.build_request(model, generation_request)
        logger.info(
            "Image dispatch request=%s alias=%s provider=%s model=%s region=%s operation=%s source=%s",
            current_request_id(), model.get("id"), model.get("provider"), model.get("modelId"),
            model.get("region"), model.get("apiOperation") or "InvokeModel", model.get("source") or "unknown",
        )
        raw_image = gateway.invoke_image(
            model, body, actor=g.username, purpose=f"image:{purpose}"
        )[0]
        generated = image_validation.validate_generated_image(raw_image)
        image_bytes = generated.data
    except providers.ProviderError as error:
        return response_error(str(error), 400, "INVALID_REQUEST", action="입력 옵션을 확인해 주세요.")
    except image_validation.GeneratedImageError as error:
        logger.error("Invalid image response request=%s alias=%s error=%s", current_request_id(), model.get("id"), error)
        return response_error(
            "이미지 모델이 저장할 수 없는 결과를 반환했습니다.", 502, "IMAGE_RESPONSE_INVALID",
            retryable=True, action="잠시 뒤 다시 시도하고 반복되면 오류 ID를 관리자에게 전달해 주세요.",
        )
    except ValueError as error:
        return response_error(str(error), 422, "CONTENT_BLOCKED", action="참조 이미지 또는 장면 설명을 확인해 주세요.")
    except ClientError as error:
        return image_client_error_response(error, model)
    except BotoCoreError:
        logger.exception("Image generation failed request=%s", current_request_id())
        return response_error(
            "이미지 생성 서비스에 연결하지 못했습니다.", 503, "IMAGE_UNAVAILABLE",
            retryable=True, action="잠시 뒤 다시 시도해 주세요.",
        )

    now = datetime.now(timezone.utc)
    key = (
        f"users/{safe_user_prefix(g.username)}/images/{now:%Y/%m/%d}/"
        f"{uuid.uuid4().hex}_{decision['presetId']}.png"
    )
    try:
        store_bytes(S3_BUCKET, key, image_bytes, "image/png", AWS_REGION)
    except (BotoCoreError, ClientError):
        logger.exception("Image store failed key=%s", key)
        return response_error("생성한 이미지를 저장하지 못했습니다.", 503, "STORAGE_ERROR")

    # 저장까지 끝난 뒤에 차감한다. S3 저장이 실패하면 사용자는 결과물을 받지 못하므로
    # 한도를 먹여서는 안 된다. refId 로 S3 키를 쓰면 결과물 하나에 정확히 1건이 붙는다.
    spend_quota(g.username, QUOTA_ASSET, f"image:{purpose}", key)

    logger.info(
        "Image generated request=%s user=%s alias=%s model=%s region=%s preset=%s tier=%s "
        "reference=%s bytes=%s dimensions=%sx%s sha256=%s",
        current_request_id(), g.username, model["id"], model.get("modelId"), model.get("region"),
        decision["presetId"], decision["tier"], bool(reference_base64), len(image_bytes),
        generated.width, generated.height, generated.sha256[:16],
    )
    return jsonify(
        {
            "ok": True,
            "requestId": current_request_id(),
            "item": {
                "key": key,
                "filename": key.rsplit("/", 1)[-1],
                "size": len(image_bytes),
                "width": generated.width,
                "height": generated.height,
                "format": "png",
                "sha256": generated.sha256,
                "lastModified": now.isoformat(),
                "url": presign(S3_BUCKET, key, AWS_REGION),
            },
            "style": {
                "presetId": decision["presetId"],
                "presetLabel": decision["presetLabel"],
                "tier": decision["tier"],
                "source": decision["source"],
                "reason": decision["reason"],
                "usedStyleReference": bool(reference_base64),
            },
            "model": public_model(model, False),
            "prompt": generation_request["prompt"],
            "scene": english_scene,
            # 보강 결과를 그대로 보여준다. 사용자가 적은 문장과 실제로 쓰인 문장이 다르므로
            # 감추면 결과가 기대와 다를 때 원인을 찾을 수 없다.
            "boost": boost_view({"scene": scene_boost, "style": style_boost}),
        }
    ), 201


# --------------------------------------------------------------------------- #
# models
# --------------------------------------------------------------------------- #
@app.get("/api/models")
@require_auth
def get_models():
    try:
        is_admin = g.role == ROLE_ADMIN
        models = model_registry.load() if is_admin else model_registry.enabled_models()
        return jsonify(
            {
                "ok": True,
                "canManage": is_admin,
                "providers": providers.known_providers() if is_admin else [],
                "items": [public_model(model, is_admin) for model in models],
                # 거부된 카탈로그 정의. 관리자에게만 준다. 비어 있는 것이 정상이다.
                # 이것이 없으면 카탈로그가 통째로 버려져도 화면에는 "모델이 몇 개 있다"로만
                # 보이고, 사라진 모델을 쓰는 기능이 왜 실패하는지 알 방법이 없다.
                "rejected": model_registry.rejected() if is_admin else [],
            }
        )
    except (BotoCoreError, ClientError):
        logger.exception("모델 카탈로그 조회 실패")
        return response_error("모델 목록을 불러오지 못했습니다.", 503, "CATALOG_ERROR")


@app.post("/api/admin/models")
@require_admin
def create_or_update_model():
    payload = request.get_json(silent=True) or {}
    try:
        model = model_registry.upsert(payload, actor=g.username)
        return jsonify({"ok": True, "item": public_model(model, True)}), 201
    except (model_registry.CatalogError, providers.ProviderError) as error:
        return response_error(str(error), 400, "INVALID_MODEL")
    except (BotoCoreError, ClientError):
        logger.exception("모델 저장 실패")
        return response_error("모델 설정을 저장하지 못했습니다.", 503, "CATALOG_WRITE_ERROR")


@app.patch("/api/admin/models/<model_id>")
@require_admin
def patch_model(model_id: str):
    payload = request.get_json(silent=True) or {}
    try:
        model = None
        if "enabled" in payload:
            model = model_registry.set_enabled(model_id, bool(payload["enabled"]), actor=g.username)
        if payload.get("isDefault"):
            model = model_registry.set_default(model_id, actor=g.username)
        if model is None:
            return response_error("변경할 항목이 없습니다.", 400, "NO_CHANGES")
        return jsonify({"ok": True, "item": public_model(model, True)})
    except model_registry.CatalogError as error:
        return response_error(str(error), 400, "INVALID_MODEL")
    except (BotoCoreError, ClientError):
        logger.exception("모델 변경 실패")
        return response_error("모델 설정을 변경하지 못했습니다.", 503, "CATALOG_WRITE_ERROR")


# --------------------------------------------------------------------------- #
# assets / videos
# --------------------------------------------------------------------------- #
@app.route("/api/assets", methods=["GET", "POST"])
@require_auth
def assets():
    try:
        if request.method == "GET":
            return jsonify({"ok": True, "items": list_objects("uploads")})
        if "file" not in request.files:
            return response_error("업로드할 이미지가 필요합니다.", 400, "FILE_REQUIRED")
        item = upload_file(request.files["file"], "uploads", ALLOWED_IMAGE_TYPES, MAX_IMAGE_BYTES)
        return jsonify({"ok": True, "item": item}), 201
    except ValueError as error:
        return response_error(str(error), 400, "INVALID_FILE")
    except (BotoCoreError, ClientError):
        logger.exception("S3 asset operation failed user=%s", g.username)
        return response_error("이미지 저장소에 연결하지 못했습니다.", 503, "STORAGE_ERROR")


_SCENE_ARTIFACT_KEY = re.compile(r"/scenes/(?P<scene>[^/]+)/(?:artifacts/v\d+/|final/)")


def annotate_video_projects(items: list[dict]) -> list[dict]:
    """라이브러리 항목에 소속 프로젝트를 붙인다.

    `/api/videos` 는 S3 목록이라 프로젝트 정보가 없다. 단계형 제작의 결과 키에는
    `.../scenes/<sceneId>/artifacts/vN/scene.mp4` 처럼 **sceneId 가 들어 있고**,
    scene 은 projectId 를 갖는다. 그 경로로 연결한다.

    장면 목록을 **한 번만** 읽어 map 을 만든다. 항목마다 조회하면 목록 크기만큼
    그래프 질의가 늘어난다. 연결할 수 없는 항목(옛 studio·GPU 결과)은 그대로 둔다 —
    임의 프로젝트에 섞지 않는다.
    """
    keys = {
        match.group("scene")
        for item in items
        if (match := _SCENE_ARTIFACT_KEY.search(str(item.get("key") or "")))
    }
    if not keys:
        return items
    try:
        scenes = graph_store.list_video_scenes(g.username, limit=200)
    except graph_store.GraphError:
        return items  # 연결 정보가 없어도 목록은 그대로 보여준다.
    by_scene = {
        str(scene.get("id")): scene for scene in scenes if str(scene.get("id") or "") in keys
    }
    if not by_scene:
        return items
    titles: dict[str, str] = {}
    for item in items:
        match = _SCENE_ARTIFACT_KEY.search(str(item.get("key") or ""))
        scene = by_scene.get(match.group("scene")) if match else None
        if not scene:
            continue
        project_id = str(scene.get("projectId") or "")
        item["sceneId"] = scene.get("id")
        item["projectId"] = project_id
        if project_id and project_id not in titles:
            project = graph_store.get_project(project_id)
            # 읽기 권한이 없으면 제목을 노출하지 않는다. 이동 링크만 남는다.
            titles[project_id] = (
                str(project.get("title") or "") if project and can_read_project(project) else ""
            )
        if project_id:
            item["projectTitle"] = titles.get(project_id, "")
    return items


def prepare_reference_image(
    raw: bytes, max_pixels: int | None = None, *, upscale: bool = True
) -> tuple[bytes, str, tuple[int, int], tuple[int, int]]:
    """영상 모델에 넣을 수 있는 형태로 이미지를 손질한다.

    Luma Ray2 키프레임 제약(실측):
      - PNG/JPEG만 받는다. 업로드는 WEBP도 허용하므로 여기서 변환한다.
      - 한 변이 1552px를 넘으면 거부한다. Bedrock 문서에는 4096px까지 된다고 적혀 있지만
        실제로는 "height of frame0 image is too large, max is 1552px" 오류가 난다.
        문서보다 모델 응답을 믿는다. 여유를 두고 1536px로 맞춘다.

    ## 첫 프레임 전처리 (품질·속도 계획서 Q1)

    I2V 는 첫 프레임의 픽셀을 그대로 물려받는다. 그래서 여기서 하는 일이 결과 품질에
    직접 남는다. 계획서가 요구한 네 가지 중 이 함수가 하는 것과 하지 않는 것을 구분해 둔다.

    하는 것:
      ② 짧은 변을 MIN_FIRST_FRAME_PIXELS(720)까지 올린다. 웹툰 컷은 366x457 처럼
         작은 것이 많고, 그대로 넣으면 결과도 저해상도 질감이 된다.
      ④ RGBA/P/CMYK -> RGB, **ICC 프로파일이 있으면 sRGB 로 변환**, PNG 무손실 유지.
         프로파일을 무시하고 픽셀만 쓰면 AdobeRGB 로 저장된 컷의 색이 눌린다.

    하지 않는 것과 그 이유:
      ③ **목표 비율로 패딩하지 않는다.** 두 엔진이 이미 입력 비율을 지킨다.
         Wan 은 `--size` 를 면적으로만 쓰고 출력 크기를 이미지 비율에서 계산한다
         (wan/image2video.py:364-372, `MAX_AREA_CONFIGS['832*480'] == ['480*832']`).
         Luma 는 `snap_aspect_ratio` 로 이미지에 가장 가까운 비율을 골라 보낸다.
         여기서 여백을 넣으면 그 여백이 영상에 그대로 들어가고 픽셀 예산까지 잡아먹는다.
         계획서 Q1-③ 은 비율을 고정으로 받는 엔진을 가정한 항목이라 이 조합에는 해롭다.
      ① 말풍선·텍스트 제거는 이 함수에서 하지 않는다. 판별에 모델이 필요하고(앱 파드에
         torch 가 없다) 잘못 지우면 원화를 훼손한다. 별도 과제로 둔다.
         참고: 모더레이션 실험에서 말풍선 제거는 통과율에 영향이 없었다(D 변형 0/1).
         즉 이 항목의 근거는 모더레이션이 아니라 품질뿐이다.

    Args:
        max_pixels: 긴 변의 상한. 기본은 첫 프레임용 MAX_REFERENCE_PIXELS.
                    분석용 축소본은 BRIEF_IMAGE_PIXELS 를 넘긴다.
        upscale: 짧은 변을 최소 크기까지 올릴지. 분석용에는 올릴 이유가 없다
                 (토큰만 늘고 판단은 나아지지 않는다).

    Returns:
        (바이트, 포맷, 처리 후 크기, **원본 크기**).
        원본 크기를 함께 주는 이유는 갤러리가 원본 해상도를 표시해야 하기 때문이다.
        축소본 크기를 보여 주면 사용자가 올린 이미지가 작아진 것으로 오해한다.
    """
    from io import BytesIO

    from PIL import Image, ImageCms

    limit = max_pixels or MAX_REFERENCE_PIXELS
    # 첫 프레임인지 분석용 축소본인지로 하한이 갈린다. 축소본에 720 하한을 걸면
    # 768px 로 줄이는 목적(비전 토큰 절감)이 무의미해진다.
    is_first_frame = not (max_pixels and max_pixels < MAX_REFERENCE_PIXELS)
    floor = MIN_FIRST_FRAME_PIXELS if is_first_frame else MIN_REFERENCE_PIXELS
    with Image.open(BytesIO(raw)) as image:
        original = image.size
        # ICC 프로파일이 sRGB 가 아니면 변환한다. 프로파일이 없거나 깨졌으면 sRGB 로 가정한다
        # (웹툰 컷 대부분이 그렇다). 실패해도 생성을 막지 않는다 — 색이 조금 어긋나는 것이
        # 영상을 못 만드는 것보다 낫다.
        profile = image.info.get("icc_profile")
        if profile:
            try:
                source = ImageCms.ImageCmsProfile(BytesIO(profile))
                image = ImageCms.profileToProfile(
                    image, source, ImageCms.createProfile("sRGB"), outputMode="RGB"
                )
            except Exception:  # noqa: BLE001 - ImageCms 는 다양한 예외를 던진다
                logger.info("ICC 프로파일 변환을 건너뜁니다(프로파일이 깨졌거나 지원되지 않음).")
        image = image.convert("RGB")
        width, height = image.size

        shortest = min(width, height)
        if upscale and shortest < floor:
            scale = floor / shortest
            image = image.resize((round(width * scale), round(height * scale)), Image.LANCZOS)
            width, height = image.size

        longest = max(width, height)
        if longest > limit:
            scale = limit / longest
            image = image.resize(
                (max(1, round(width * scale)), max(1, round(height * scale))), Image.LANCZOS
            )

        buffer = BytesIO()
        # 첫 프레임은 무손실이어야 한다(모델이 그 픽셀을 그대로 이어받는다).
        # 분석용 축소본은 JPEG 로 둔다. 같은 크기에서 10분의 1 이하라 메모리와 전송이 가볍고
        # 역할 판단에는 압축 손실이 영향을 주지 않는다.
        # 하한과 같은 판정을 쓴다. 두 곳에서 따로 판단하면 한쪽만 고쳤을 때
        # "첫 프레임인데 JPEG" 같은 조합이 조용히 생긴다.
        if is_first_frame:
            image.save(buffer, format="PNG", optimize=True)
            image_format = "png"
        else:
            image.save(buffer, format="JPEG", quality=82, optimize=True)
            image_format = "jpeg"
        logger.info(
            "Reference image prepared %sx%s -> %sx%s (%s, %s bytes, floor=%s, icc=%s)",
            *original,
            *image.size,
            image_format,
            len(buffer.getvalue()),
            floor,
            "yes" if profile else "no",
        )
        return buffer.getvalue(), image_format, image.size, original


def snap_aspect_ratio(size: tuple[int, int]) -> str:
    """이미지 비율에 가장 가까운 지원 비율을 고른다.

    첫 프레임이 곧 업로드한 이미지이므로, 요청 비율이 이미지와 다르면 잘리거나 여백이 생긴다.
    사용자가 고른 값보다 이미지 비율을 우선하고, 무엇으로 생성했는지 응답에 담아 알린다.
    """
    width, height = size
    if not width or not height:
        return "16:9"
    target = width / height
    candidates = {"1:1": 1.0, "16:9": 16 / 9, "9:16": 9 / 16, "4:3": 4 / 3, "3:4": 3 / 4, "21:9": 21 / 9, "9:21": 9 / 21}
    return min(candidates, key=lambda name: abs(candidates[name] - target))


def video_prompt_caller(actor: str):
    """video_prompt 모듈이 쓸 호출자. 게이트웨이를 거쳐 사용량이 기록된다."""

    def call(system: str, content: list[dict], max_tokens: int) -> str:
        text, _ = gateway.converse(
            VIDEO_PROMPT_MODEL,
            AWS_REGION,
            system,
            content,
            max_tokens=max_tokens,
            # 프롬프트는 매번 다른 문장이 나오기보다 지시를 정확히 따르는 것이 중요하다.
            temperature=0.2,
            actor=actor,
            purpose="video:prompt",
            long_running=False,
        )
        return text

    return call


# --------------------------------------------------------------------------- #
# 참고 이미지 여러 장 (두 영상 경로가 함께 쓴다)
# --------------------------------------------------------------------------- #
# 영상 모델은 첫 프레임으로 이미지 **한 장**만 받는다(Luma의 keyframes.frame0,
# Wan의 imageBase64). 그런데 실제 작업에서는 캐릭터 시트와 컨셉 아트를 함께 올린다.
# 그래서 여러 장을 받아 한 장은 첫 프레임으로 쓰고, 나머지는 분석해 프롬프트의 근거로만 쓴다.
# Bedrock Converse 는 요청당 이미지 20장이 상한이다(AWS 문서). 그래서 분석 한 번에 담을 수
# 있는 최대가 20장이고, 이 값을 그것에 맞춘다. 더 늘리려면 분석을 여러 번 호출해 결과를
# 합쳐야 하는데, 실제 작업에서 20장을 넘기는 경우가 드물어 지금은 단일 호출로 둔다.
MAX_REFERENCE_IMAGES = int(os.getenv("VIDEO_MAX_REFERENCE_IMAGES", "20"))


def load_reference_image(key: str, actor: str, max_pixels: int | None = None) -> tuple[dict | None, str | None]:
    """한 장만 읽는다. 첫 프레임처럼 실제로 바이트가 필요한 경우에 쓴다."""
    if not is_owned_image_key(key, actor):
        return None, "본인이 올리거나 만든 이미지만 사용할 수 있습니다."
    try:
        obj = s3.get_object(Bucket=S3_BUCKET, Key=key)
        prepared, image_format, size, original = prepare_reference_image(
            obj["Body"].read(), max_pixels
        )
    except (BotoCoreError, ClientError):
        logger.exception("참고 이미지를 읽지 못했습니다 key=%s", key)
        return None, "이미지를 불러오지 못했습니다."
    except Exception as error:  # noqa: BLE001 - Pillow 예외 종류가 다양하다
        logger.warning("참고 이미지를 해석하지 못했습니다 key=%s: %s", key, error)
        return None, "이미지를 읽지 못했습니다. PNG, JPG, WEBP 파일인지 확인해 주세요."
    return (
        {
            "key": key,
            # 파일 이름이 분석의 핵심 근거다. S3 키에서는 한글이 사라지므로 메타데이터에서 읽는다.
            "name": s3_original_name(key),
            "bytes": prepared,
            "format": image_format,
            "size": size,
            "originalSize": original,
        },
        None,
    )


def collect_reference_images(
    keys: list[str],
    actor: str,
    *,
    load_bytes: bool = True,
    max_pixels: int | None = None,
) -> tuple[list[dict], str | None]:
    """S3에서 이미지들을 읽어 원본 파일명과 함께 돌려준다.

    (이미지 목록, 오류 메시지)를 돌려준다. 오류가 있으면 목록은 신뢰하지 않는다.
    소유권은 키마다 확인한다. 한 장만 검사하고 나머지를 통과시키면 우회 통로가 된다.

    Args:
        load_bytes: False여도 모든 자산을 한 장씩 완전 디코딩한다. 다만 반환 목록에서는
                    변환 바이트만 버려 메모리를 줄인다. 이름/HEAD만 보던 옛 경로는 secondary
                    reference의 없음·손상을 유료 제출 전 잡지 못했다.
        max_pixels: 긴 변 상한. 분석 경로는 BRIEF_IMAGE_PIXELS 를 넘겨 축소본을 받는다.
    """
    images: list[dict] = []
    for key in keys[:MAX_REFERENCE_IMAGES]:
        if not is_owned_image_key(key, actor):
            return [], "본인이 올리거나 만든 이미지만 사용할 수 있습니다."
        validate_pixels = max_pixels
        if not load_bytes and validate_pixels is None:
            validate_pixels = BRIEF_IMAGE_PIXELS
        item, error = load_reference_image(key, actor, validate_pixels)
        if error:
            return [], error
        if not item:
            return [], "이미지를 읽지 못했습니다."
        if not load_bytes:
            item = {**item, "bytes": None}
        images.append(item)
    return images, None


def parse_asset_keys(payload: dict) -> list[str]:
    """요청에서 이미지 키 목록을 뽑는다.

    예전 요청은 `assetKey` 하나만 보냈다. 그 형식도 계속 받는다.
    프론트가 null을 보내면 str(None) == "None" 이 되므로 `or ""` 로 걸러낸다.
    """
    keys: list[str] = []
    raw_list = payload.get("assetKeys")
    if isinstance(raw_list, list):
        keys = [str(item or "").strip() for item in raw_list]
    single = str(payload.get("assetKey") or "").strip()
    if single:
        keys.append(single)
    # 순서를 유지하면서 중복을 제거한다. 같은 이미지를 두 번 분석할 이유가 없다.
    seen: set[str] = set()
    unique = []
    for key in keys:
        if key and key not in seen:
            seen.add(key)
            unique.append(key)
    return unique


def pick_primary(images: list[dict], payload: dict, brief: dict | None) -> int:
    """첫 프레임으로 쓸 이미지의 위치(0부터)를 정한다.

    우선순위는 기획자의 명시적 선택 > 분석의 추천 > 첫 번째다.
    """
    requested = str(payload.get("primaryKey") or "").strip()
    if requested:
        for index, item in enumerate(images):
            if item["key"] == requested:
                return index
    suggested = (brief or {}).get("suggestedPrimary")
    try:
        number = int(suggested)
    except (TypeError, ValueError):
        number = 0
    if 1 <= number <= len(images):
        return number - 1
    return 0


def parse_answers(payload: dict) -> list[dict]:
    """확인 질문에 대한 답변을 정리한다.

    **비어 있는 답은 버린다.** 건너뛴 질문을 "미정"처럼 채워 보내면 모델이 그 단어를
    반영하려 하고, 결과에 실제로 나타난다. 답하지 않은 것은 없는 것으로 둔다.
    """
    raw = payload.get("answers")
    answers: list[dict] = []
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            answer = str(item.get("answer") or "").strip()
            if not answer:
                continue
            answers.append(
                {
                    "id": str(item.get("id") or "")[:32],
                    "question": str(item.get("question") or "")[:300],
                    "answer": answer[:500],
                }
            )
    elif isinstance(raw, dict):
        # {questionId: answer} 형태도 받는다.
        for key, value in raw.items():
            answer = str(value or "").strip()
            if answer:
                answers.append({"id": str(key)[:32], "question": "", "answer": answer[:500]})
    return answers[:10]


def run_image_analysis(target: str):
    """이미지 분석 요청을 처리한다. 두 영상 경로가 각자의 라우트에서 이 함수를 부른다.

    **라우트는 분리하고 구현은 공유한다.** 지시서 1-2는 경로가 섞이지 않게 하라고 했고,
    그 요구는 엔드포인트와 상태·모델 선택에 대한 것이다. 이미지를 읽어 역할을 판단하는
    절차 자체는 같으므로 복사하지 않는다. 두 벌로 두면 한쪽만 고쳐져 조용히 갈라진다.
    무엇을 물을지는 `target`이 갈라 준다.

    생성과 분리한 이유: 기획자가 분석을 보고 첫 프레임을 바꾸거나 질문에 답한 뒤에
    생성을 눌러야 한다. 생성 안에서 함께 하면 그 사이에 개입할 지점이 없다.
    """
    payload = request.get_json(silent=True) or {}
    keys = parse_asset_keys(payload)
    if not keys:
        return response_error("분석할 이미지를 올려 주세요.", 400, "IMAGE_REQUIRED")

    # 분석에는 축소본을 쓴다. 역할 판단에 768px 이면 충분하고, 20장 × 1536px PNG 를
    # 그대로 보내면 비전 토큰이 6만 개가 되고 파드 메모리도 위험하다.
    images, error = collect_reference_images(
        keys, g.username, max_pixels=BRIEF_IMAGE_PIXELS
    )
    if error:
        code = "ASSET_FORBIDDEN" if "본인" in error else "INVALID_IMAGE"
        return response_error(error, 403 if code == "ASSET_FORBIDDEN" else 400, code)
    if not images:
        return response_error("이미지를 읽지 못했습니다.", 400, "INVALID_IMAGE")

    user_text = str(payload.get("text") or "")[:20000]
    brief = image_brief.analyze(
        image_brief_caller(g.username), images, user_text, target=target
    )

    # 화면이 갤러리를 그릴 수 있게 키와 미리보기 URL을 붙여 돌려준다.
    for index, item in enumerate(brief["images"], start=1):
        if index > len(images):
            continue
        source = images[index - 1]
        item["key"] = source["key"]
        try:
            item["url"] = presign(S3_BUCKET, source["key"], AWS_REGION)
        except (BotoCoreError, ClientError):
            item["url"] = None
        # 갤러리에는 원본 해상도를 보여준다. 분석용 축소본 크기를 보여 주면
        # 올린 이미지가 작아진 것으로 오해한다.
        item["width"], item["height"] = source["originalSize"]

    primary_index = brief["suggestedPrimary"]
    primary_key = (
        images[primary_index - 1]["key"]
        if 1 <= primary_index <= len(images)
        else images[0]["key"]
    )
    logger.info(
        "Image brief user=%s target=%s images=%s questions=%s primary=%s degraded=%s",
        g.username,
        target,
        len(images),
        len(brief["questions"]),
        primary_index,
        brief["degraded"],
    )
    return jsonify(
        {
            "ok": True,
            "brief": brief,
            "primaryKey": primary_key,
            "roles": [{"id": key, "label": label} for key, label in image_brief.ROLES.items()],
        }
    )


def image_brief_caller(actor: str):
    """이미지 분석용 호출자. 여러 장을 함께 읽어야 해서 비전 모델을 쓴다."""

    def call(system: str, content: list[dict], max_tokens: int) -> str:
        text, _ = gateway.converse(
            VIDEO_PROMPT_MODEL,
            AWS_REGION,
            system,
            content,
            max_tokens=max_tokens,
            temperature=0.2,
            actor=actor,
            purpose="image:brief",
            long_running=False,
        )
        return text

    return call


def build_video_prompt(
    image_bytes: bytes | None,
    image_format: str,
    user_prompt: str,
    actor: str,
    style_decision: dict | None = None,
    extra_context: str = "",
) -> dict:
    """사용자 지시를 영상 모델용 영어 프롬프트로 다시 쓴다.

    규칙과 조립은 `video_prompt` 모듈이 갖는다. 여기서는 게이트웨이 연결만 한다.
    이미지가 있을 때와 없을 때 프롬프트가 담아야 하는 내용이 반대라서 모듈이 갈라서 처리한다.
    """
    return video_prompt.build(
        video_prompt_caller(actor),
        image_bytes=image_bytes,
        image_format=image_format,
        user_prompt=user_prompt,
        style_prompt=(style_decision or {}).get("stylePrompt", ""),
        extra_context=extra_context,
    )


def sequence_payload(sequence: dict) -> dict:
    """사용자에게 보낼 형태. 완성된 영상은 presigned URL로 준다."""
    view = dict(sequence)
    if sequence.get("outputKey") and sequence.get("outputBucket"):
        bucket = sequence["outputBucket"]
        region = bucket_region(bucket) or VIDEO_REGION
        view["videoUrl"] = presign(bucket, sequence["outputKey"], region)
    else:
        view["videoUrl"] = None
    # 컷 프롬프트 전문은 목록에서 필요하지 않다. 요약만 남겨 응답을 가볍게 한다.
    view["shots"] = [
        {
            "number": shot.get("number"),
            "summary": shot.get("summary"),
            "status": shot.get("status") or "pending",
            "error": shot.get("error") or "",
        }
        for shot in sequence.get("shots", [])
    ]
    return view


def start_clip_with_retry(
    model: dict, model_input: dict, output_uri: str, owner: str, seconds: float
) -> str:
    """컷 생성을 시작한다. 스로틀에 걸리면 기다렸다가 다시 시도한다.

    이 계정의 Luma Ray V2 동시 요청 한도는 1건이다(Service Quotas 확인).
    한도를 넘으면 ThrottlingException이 오는데, 그때 컷을 버리면 긴 작업이 통째로 망가진다.
    잠시 기다렸다 다시 부르는 것이 맞다.
    """
    delay = CLIP_RETRY_BASE_SECONDS
    last_error: Exception | None = None
    for attempt in range(1, CLIP_RETRY_ATTEMPTS + 1):
        try:
            return gateway.start_video(
                model, model_input, output_uri, actor=owner, purpose="video:sequence", seconds=seconds
            )
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code", "")
            # ResourceNotFoundException도 동시 호출이 몰릴 때 관측됐다. 재시도 대상에 포함한다.
            if code not in {"ThrottlingException", "TooManyRequestsException", "ResourceNotFoundException"}:
                raise
            last_error = error
            logger.info(
                "Clip start throttled (%s), retry %s/%s after %ss", code, attempt, CLIP_RETRY_ATTEMPTS, delay
            )
            time.sleep(delay)
            delay = min(delay * 2, CLIP_RETRY_MAX_SECONDS)
    raise last_error or RuntimeError("컷 생성을 시작하지 못했습니다.")


def wait_for_clip(invocation_arn: str, region: str, deadline: float) -> str | None:
    """비동기 영상 작업이 끝날 때까지 기다리고 결과 S3 키를 돌려준다."""
    client = gateway.runtime(region)
    while time.time() < deadline:
        time.sleep(SEQUENCE_POLL_SECONDS)
        try:
            result = client.get_async_invoke(invocationArn=invocation_arn)
        except (BotoCoreError, ClientError):
            logger.exception("Clip status check failed arn=%s", invocation_arn)
            return None
        status = result.get("status")
        if status == "InProgress":
            continue
        if status != "Completed":
            # failureMessage 를 버리면 컷이 왜 빠졌는지 어디에도 남지 않는다.
            # 롱테이크는 한 시간 넘게 도는 작업이라 나중에 원인을 찾을 방법이 없었다.
            # 콘텐츠 필터 거부가 실제로 여기서 일어난다(참조 이미지를 첫 컷에 넣는다).
            failure = result.get("failureMessage") or ""
            logger.warning(
                "Clip failed arn=%s status=%s message=%s detail=%s",
                invocation_arn,
                status,
                " ".join(failure.split())[:300],
                explain_video_failure(failure) if failure else "",
            )
            return None
        output_uri = (result.get("outputDataConfig") or {}).get("s3OutputDataConfig", {}).get("s3Uri", "")
        return find_video_key(output_uri)
    return None


def find_video_key(output_uri: str) -> str | None:
    """비동기 출력 폴더에서 mp4 키를 찾는다. Bedrock이 하위 폴더를 만들기 때문이다."""
    if not output_uri.startswith("s3://"):
        return None
    without_scheme = output_uri[5:]
    bucket, _, prefix = without_scheme.partition("/")
    region = bucket_region(bucket) or VIDEO_REGION
    client = s3_client(region)
    try:
        response = client.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=50)
    except (BotoCoreError, ClientError):
        logger.exception("Output listing failed uri=%s", output_uri)
        return None
    for item in response.get("Contents", []):
        if item["Key"].lower().endswith(".mp4"):
            return item["Key"]
    return None


def run_sequence(sequence_id: str, owner: str, reference_key: str, model_id: str) -> None:
    """롱테이크 생성 전체 흐름. 백그라운드 스레드에서 돈다.

    계획 -> 컷 생성(제한된 동시 실행) -> 다운로드 -> 이어 붙이기 -> 업로드.
    중간에 일부 컷이 실패해도 성공한 컷만으로 이어 붙인다. 두 시간 걸린 작업을
    한 컷 때문에 전부 버리는 것이 더 나쁘다.
    """
    import tempfile

    work_dir = None
    try:
        sequence = graph_store.get_sequence(sequence_id)
        if not sequence:
            return
        model = model_registry.get(model_id) or model_registry.resolve_for_generation(None, kind="video")
        region = model["region"]
        output_bucket = model_registry.effective_output_bucket(model)

        # 1) 컷 나누기
        caller = gateway.text_caller(VIDEO_PROMPT_MODEL, AWS_REGION, owner, "sequence:plan")
        plan = documents.plan_shots(
            caller,
            sequence["prompt"],
            sequence["shotCount"],
            sequence["shotSeconds"],
            sequence["stylePrompt"],
        )
        shots = [dict(shot, status="pending", error="") for shot in plan["shots"]]
        graph_store.update_sequence(
            sequence_id,
            {
                "status": graph_store.SEQ_RENDERING,
                "title": plan["title"] or sequence["title"],
                "shotsJson": json.dumps(shots, ensure_ascii=False),
                "shotCount": len(shots),
            },
        )

        # 2) 컷 생성. 동시 실행 수를 제한해 서비스 쿼터를 넘지 않게 한다.
        reference_base64 = None
        image_format = "png"
        # --- 비율 결정 ---
        #
        # 예전에는 아래 요청 조립에서 `"aspectRatio": "16:9"` 를 고정했다. 그것이 버그였다.
        # 첫 컷은 업로드 이미지를 frame0 으로 쓰는데, 요청 비율이 이미지 비율과 다르면 **잘린다**
        # (이 프로젝트가 단일 클립 경로에서 snap_aspect_ratio 를 쓰는 이유가 정확히 그것이다).
        # 세로 웹툰 컷으로 롱테이크를 만들면 첫 컷의 위아래가 날아갔다.
        #
        # **모든 컷이 같은 비율이어야 한다.** stitcher 가 재인코딩 없이 스트림 복사로 붙이므로
        # 컷마다 비율이 다르면 복사가 실패하고 재인코딩으로 떨어져 화질이 깎인다.
        # 그래서 첫 프레임에서 한 번 정해 전 컷에 같은 값을 쓴다.
        sequence_aspect = "16:9"
        if reference_key and (model.get("capabilities") or {}).get("imageInput"):
            try:
                obj = s3.get_object(Bucket=S3_BUCKET, Key=reference_key)
                prepared, image_format, prepared_size, _ = prepare_reference_image(obj["Body"].read())
                reference_base64 = base64.b64encode(prepared).decode("ascii")
                sequence_aspect = snap_aspect_ratio(prepared_size)
            except Exception:  # noqa: BLE001
                logger.warning("Sequence reference image unusable key=%s", reference_key)
        # 해상도도 코드에 박지 않고 카탈로그에서 가장 높은 값을 고른다.
        # 모델을 바꿨을 때 이 값이 따라오지 않으면 조용히 옛 해상도로 만든다.
        supported_resolutions = (model.get("capabilities") or {}).get("resolutions") or ["720p"]
        sequence_resolution = "720p" if "720p" in supported_resolutions else supported_resolutions[-1]
        logger.info(
            "Sequence %s aspect=%s resolution=%s (참조 이미지 %s)",
            sequence_id,
            sequence_aspect,
            sequence_resolution,
            "있음" if reference_base64 else "없음",
        )

        adapter = providers.resolve(model)
        prefix = f"users/{safe_user_prefix(owner)}/videos/{datetime.now(timezone.utc):%Y/%m/%d}"
        # 컷당 예산을 두고 전체 기한을 잡는다. 컷이 많을수록 오래 걸리는 것이 정상이다.
        deadline = time.time() + min(
            SEQUENCE_TIMEOUT_SECONDS, CLIP_BUDGET_SECONDS * max(1, len(shots))
        )

        # 이 계정은 Luma 동시 요청 한도가 1이라 컷을 순차로 만든다.
        # 병렬로 돌리면 대부분 ThrottlingException으로 버려진다(실측).
        results: list[dict] = []
        for shot in shots:
            request = {
                "prompt": shot["prompt"],
                "durationSeconds": sequence["shotSeconds"],
                # 해상도는 이 모델이 주는 최대치를 쓴다. 롱테이크는 한 번 만들면 오래 걸려
                # 낮은 해상도로 뽑을 이유가 없다. 선택지로 열려 있지 않은 값이라 카탈로그를
                # 기준으로 삼는다(예전에는 "720p" 가 코드에 박혀 있어 모델을 바꿔도 따라오지 않았다).
                "resolution": sequence_resolution,
                "aspectRatio": sequence_aspect,
                "outputBucket": output_bucket,
                "outputPrefix": f"{prefix}/seq-{sequence_id[:8]}/shot-{shot['number']:03d}",
            }
            # 첫 컷만 참조 이미지를 첫 프레임으로 쓴다. 모든 컷에 넣으면 매 컷이 같은 그림에서
            # 다시 시작해 이어지지 않는다.
            if reference_base64 and shot["number"] == 1:
                request["imageBase64"] = reference_base64
                request["imageFormat"] = image_format

            if time.time() >= deadline:
                results.append({**shot, "status": "failed", "error": "전체 제한 시간을 초과했습니다."})
            else:
                try:
                    model_input = adapter.build_request(model, request)
                    arn = start_clip_with_retry(
                        model,
                        model_input,
                        adapter.output_location(model, request),
                        owner,
                        float(sequence["shotSeconds"]),
                    )
                    key = wait_for_clip(arn, region, deadline)
                    if key:
                        results.append({**shot, "status": "done", "error": "", "key": key})
                    else:
                        results.append({**shot, "status": "failed", "error": "컷 생성이 완료되지 않았습니다."})
                except (providers.ProviderError, BotoCoreError, ClientError, RuntimeError) as error:
                    results.append({**shot, "status": "failed", "error": str(error)[:200]})

            done = sum(1 for item in results if item["status"] == "done")
            failed = sum(1 for item in results if item["status"] == "failed")
            finished_numbers = {item["number"] for item in results}
            remaining = [item for item in shots if item["number"] not in finished_numbers]
            graph_store.update_sequence(
                sequence_id,
                {
                    "shotsJson": json.dumps(results + remaining, ensure_ascii=False),
                    "doneCount": done,
                    "failedCount": failed,
                },
            )

        results.sort(key=lambda item: item["number"])
        completed = [item for item in results if item.get("key")]
        if not completed:
            graph_store.update_sequence(
                sequence_id,
                {"status": graph_store.SEQ_FAILED, "error": "모든 컷 생성이 실패했습니다."},
            )
            return

        # 3) 내려받아 이어 붙이기
        graph_store.update_sequence(sequence_id, {"status": graph_store.SEQ_STITCHING})
        work_dir = tempfile.mkdtemp(prefix="seq-")
        clip_region = bucket_region(output_bucket) or region
        clip_client = s3_client(clip_region)
        local_paths = []
        for item in completed:
            local = os.path.join(work_dir, f"shot-{item['number']:03d}.mp4")
            clip_client.download_file(output_bucket, item["key"], local)
            local_paths.append(local)

        final_local = os.path.join(work_dir, "sequence.mp4")
        stitcher.concat(local_paths, final_local)

        final_key = f"{prefix}/seq-{sequence_id[:8]}/longtake_{sequence_id[:8]}.mp4"
        with open(final_local, "rb") as handle:
            store_bytes(output_bucket, final_key, handle.read(), "video/mp4", clip_region)

        graph_store.update_sequence(
            sequence_id,
            {
                "status": graph_store.SEQ_READY,
                "outputKey": final_key,
                "outputBucket": output_bucket,
                "error": (
                    f"{len(results) - len(completed)}개 컷이 실패해 제외했습니다."
                    if len(completed) < len(results)
                    else ""
                ),
            },
        )
        # 롱테이크 한 편이 결과물 1건이다. 컷이 4개든 67개든 1건이다.
        # 컷마다 세면 1분짜리(7컷)가 영상 한도 10건 중 7건을 먹어 사실상 쓸 수 없다.
        # 다만 그래서 30초 한 편과 10분 한 편이 같은 1건이 된다는 점은 알고 있어야 한다.
        # 지금은 그것을 받아들인다. 컷 수로 세면 위의 문제가 더 크다.
        spend_quota(owner, QUOTA_VIDEO, "video:longtake", sequence_id)
        logger.info(
            "Sequence ready id=%s clips=%s/%s key=%s",
            sequence_id,
            len(completed),
            len(results),
            final_key,
        )
    except (documents.DocumentError, stitcher.StitchError) as error:
        graph_store.update_sequence(sequence_id, {"status": graph_store.SEQ_FAILED, "error": str(error)})
    except Exception as error:  # noqa: BLE001 - 스레드에서 예외가 새면 상태가 굳는다
        logger.exception("Sequence failed id=%s", sequence_id)
        graph_store.update_sequence(
            sequence_id, {"status": graph_store.SEQ_FAILED, "error": f"예상치 못한 오류: {error}"}
        )
    finally:
        if work_dir:
            import shutil as _shutil

            _shutil.rmtree(work_dir, ignore_errors=True)


STORYBOARD_CUT_CHOICES = (4, 6, 8, 12)
# 콘티 컷 기획은 컷 수만큼 분량이 늘어난다. 긴 출력이 끊기지 않게 넉넉한 타임아웃이 필요하다.
STORYBOARD_TEXT_MODEL = os.getenv("STORYBOARD_TEXT_MODEL", documents.TEXT_MODEL)
# 콘티 그림은 컷 수만큼 호출한다. 전체 기한을 두어 파드가 무한히 붙잡히지 않게 한다.
STORYBOARD_BUDGET_SECONDS = int(os.getenv("STORYBOARD_BUDGET_SECONDS", "60"))
STORYBOARD_STALE_MINUTES = int(os.getenv("STORYBOARD_STALE_MINUTES", "20"))


def storyboard_payload(board: dict) -> dict:
    """사용자에게 보낼 형태. 컷 그림은 presigned URL로 준다."""
    view = dict(board)
    cuts = []
    for cut in board.get("cuts", []):
        item = {
            "number": cut.get("number"),
            "context": cut.get("context") or "",
            "shot": cut.get("shot") or "",
            "dialogue": cut.get("dialogue") or "",
            "audio": cut.get("audio") or "",
            "note": cut.get("note") or "",
            "seconds": cut.get("seconds") or 0,
            "status": cut.get("status") or "pending",
            "error": cut.get("error") or "",
            "imageKey": cut.get("imageKey") or "",
            "imageUrl": None,
        }
        if cut.get("imageKey"):
            try:
                item["imageUrl"] = presign(S3_BUCKET, cut["imageKey"], AWS_REGION)
            except (BotoCoreError, ClientError):
                logger.warning("Storyboard image presign failed key=%s", cut["imageKey"])
        cuts.append(item)
    view["cuts"] = cuts
    view["totalSeconds"] = sum(int(cut.get("seconds") or 0) for cut in cuts)
    return view


def reap_stale_storyboard(board: dict) -> dict:
    """진행 중 상태로 방치된 콘티를 실패로 정리한다.

    파드가 롤아웃되면 생성 스레드가 사라지지만 그래프에는 drawing이 남는다.
    산출물 쪽과 같은 방식으로 조회 시점에 정리한다(롱테이크에는 이 장치가 없어 굳는 경우가 있었다).
    """
    if board.get("status") not in graph_store.SB_ACTIVE_STATUSES:
        return board
    updated_at = board.get("updatedAt")
    if not updated_at:
        return board
    try:
        started = datetime.fromisoformat(str(updated_at))
    except ValueError:
        return board
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) - started < timedelta(minutes=STORYBOARD_STALE_MINUTES):
        return board
    try:
        return graph_store.update_storyboard(
            board["id"],
            {
                "status": graph_store.SB_FAILED,
                "error": f"생성이 {STORYBOARD_STALE_MINUTES}분 안에 끝나지 않았습니다. 다시 시도해 주세요.",
            },
        )
    except (graph_store.GraphError, graph_store.NotFoundError):
        return board


STORYBOARD_MAX_CUTS = int(os.getenv("STORYBOARD_MAX_CUTS", "24"))

# 기획자가 컷을 적을 때 쓸 수 있는 숏 크기. 자유 문자열을 그대로 두면 표기가 갈린다.
STORYBOARD_SHOTS = ("wide", "medium", "close-up", "over-the-shoulder")


def normalize_cut_shot(raw: str) -> str:
    text = " ".join(str(raw or "").split()).lower().replace("_", "-")
    for shot in STORYBOARD_SHOTS:
        if shot in text:
            return shot
    if "close" in text:
        return "close-up"
    if "shoulder" in text:
        return "over-the-shoulder"
    return "medium"


def cut_fields_from(payload: dict, number: int) -> dict:
    """요청 본문에서 컷 한 개의 사람이 읽는 필드를 뽑는다."""
    try:
        seconds = int(payload.get("seconds") or 4)
    except (TypeError, ValueError):
        seconds = 4
    return {
        "number": number,
        "context": str(payload.get("context") or "").strip()[:600],
        "dialogue": str(payload.get("dialogue") or "").strip()[:400],
        "audio": str(payload.get("audio") or "").strip()[:300],
        "note": str(payload.get("note") or "").strip()[:300],
        "shot": normalize_cut_shot(payload.get("shot")),
        "seconds": max(1, min(30, seconds)),
    }


def parse_manual_cuts(raw) -> list[dict]:
    """직접 입력한 컷 목록을 정리한다.

    두 형태를 받는다. 객체 배열과 줄바꿈으로 구분한 문자열.
    문자열을 허용하는 이유는 기획자가 컷을 한 줄씩 빠르게 적는 것이 가장 흔한 사용이기 때문이다.
    """
    items: list[dict] = []
    if isinstance(raw, str):
        for line in raw.splitlines():
            text = line.strip()
            # "1. 장면" 처럼 번호를 붙여 적는 경우가 많다. 번호는 서버가 다시 매긴다.
            text = re.sub(r"^\s*\d+\s*[.)\]]\s*", "", text)
            if len(text) >= 2:
                items.append({"context": text})
    elif isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, str):
                if entry.strip():
                    items.append({"context": entry.strip()})
            elif isinstance(entry, dict):
                if str(entry.get("context") or "").strip():
                    items.append(entry)

    return [
        {**cut_fields_from(item, index), "status": "pending", "error": "", "imageKey": "", "scene": ""}
        for index, item in enumerate(items[:STORYBOARD_MAX_CUTS], start=1)
    ]


def draw_storyboard_cut(board: dict, cut: dict, actor: str) -> dict:
    """컷 한 개를 그려 S3에 올린다. 저장된 그림체 판단을 그대로 다시 쓴다.

    다시 그릴 때 그림체를 새로 판단하면 표 안에서 화풍이 섞인다. 그래서 처음 판단을 재사용한다.
    """
    decision = board.get("style") or {}
    if not decision.get("stylePrompt"):
        # 예전에 만든 콘티에는 판단이 저장되어 있지 않다. 그림체 지시로 다시 판단한다.
        decision = styles.route(
            board["stylePrompt"],
            purpose="storyboard",
            caller=style_router_caller(actor),
        )
    shared = board.get("shared") or {}
    model = model_registry.get(board.get("modelId") or "") or image_model(
        tier=decision.get("tier")
    )

    scene = documents.storyboard_scene_prompt(
        cut, shared.get("characters", ""), shared.get("setting", "")
    )
    body = providers.resolve(model).build_request(
        model,
        {
            "prompt": styles.compose_prompt(scene, decision),
            "negativePrompt": decision.get("negativePrompt", ""),
            "aspectRatio": board["aspectRatio"],
        },
    )
    image_bytes = gateway.invoke_image(model, body, actor=actor, purpose="image:storyboard")[0]

    now = datetime.now(timezone.utc)
    key = (
        f"users/{safe_user_prefix(actor)}/images/{now:%Y/%m/%d}/"
        f"{board['id'][:8]}_cut{cut['number']:02d}_{uuid.uuid4().hex[:6]}.png"
    )
    store_bytes(S3_BUCKET, key, image_bytes, "image/png", AWS_REGION)
    return {**cut, "status": "done", "error": "", "imageKey": key}


def save_storyboard_cuts(storyboard_id: str, cuts: list[dict]) -> dict:
    """컷 목록과 진행 카운터를 함께 저장한다. 둘이 어긋나면 진행률이 틀리게 보인다."""
    done = sum(1 for cut in cuts if cut.get("status") == "done")
    failed = sum(1 for cut in cuts if cut.get("status") == "failed")
    return graph_store.update_storyboard(
        storyboard_id,
        {
            "cutsJson": json.dumps(cuts, ensure_ascii=False),
            "cutCount": len(cuts),
            "doneCount": done,
            "failedCount": failed,
        },
    )


def load_storyboard_context(project_id: str) -> dict:
    """콘티 기획에 넣을 앞 단계 산출물. 있으면 인물과 구조를 그대로 쓴다."""
    context: dict = {}
    if not project_id:
        return context
    for kind in ("characters", "treatment"):
        document = graph_store.get_document(project_id, kind)
        if not document or document.get("status") != graph_store.DOC_READY or not document.get("s3Key"):
            continue
        try:
            context[kind] = json.loads(read_source_text(document["s3Key"]))
        except (BotoCoreError, ClientError, json.JSONDecodeError):
            logger.warning("Storyboard context unreadable project=%s kind=%s", project_id, kind)
    if context:
        logger.info("Storyboard context project=%s uses=%s", project_id, list(context))
    return context


def run_storyboard(storyboard_id: str, owner: str, preset_id: str, reference_key: str) -> None:
    """콘티 생성 전체 흐름. 백그라운드 스레드에서 돈다.

    컷 기획(1회) -> 그림체 판단(1회) -> 컷마다 그림 생성.

    그림체 판단을 컷마다 하지 않는 이유는 두 가지다. 컷 수만큼 비용이 붙고, 판단이 컷마다
    흔들리면 표 안에서 그림체가 섞인다. 한 번 정해 모든 컷에 같은 값을 쓴다.
    """
    try:
        board = graph_store.get_storyboard(storyboard_id)
        if not board:
            return

        # 0) 이야기와 그림체 지시를 자세하게 다시 쓴다. 요청이 아니라 여기서 하는 이유는
        # 콘티 생성이 202로 즉시 응답해야 하기 때문이다. 보강한 문장을 정점에 저장해
        # 화면에서도 실제로 쓰인 문장을 볼 수 있게 한다.
        # 직접 입력 모드에서는 이야기를 맥락으로만 쓰므로 그림체만 보강한다.
        boost_items = [("style", board["stylePrompt"], prompt_boost.KIND_STYLE)]
        if board["mode"] != graph_store.SB_MODE_MANUAL:
            boost_items.append(("story", board["prompt"], prompt_boost.KIND_SCENE))
        boosted = boost_many(boost_items, owner)
        updates = {}
        if (boosted.get("story") or {}).get("changed"):
            board["prompt"] = boosted["story"]["text"]
            updates["prompt"] = board["prompt"]
        if (boosted.get("style") or {}).get("changed"):
            board["stylePrompt"] = boosted["style"]["text"]
            updates["stylePrompt"] = board["stylePrompt"]
        if updates:
            graph_store.update_storyboard(storyboard_id, updates)
            logger.info("Storyboard input boosted id=%s fields=%s", storyboard_id, list(updates))

        # 1) 컷 준비. 자동이면 AI가 나누고, 직접 입력이면 적힌 컷을 그대로 쓴다.
        caller = gateway.text_caller(
            STORYBOARD_TEXT_MODEL, AWS_REGION, owner, "storyboard:plan"
        )
        if board["mode"] == graph_store.SB_MODE_MANUAL:
            plan = documents.prepare_manual_cuts(
                caller,
                board["cuts"],
                style_prompt=board["stylePrompt"],
                story=board["prompt"],
            )
        else:
            plan = documents.plan_storyboard(
                caller,
                board["prompt"],
                board["cutCount"],
                detail=board["detail"],
                style_prompt=board["stylePrompt"],
                context=load_storyboard_context(board["projectId"]),
            )
        cuts = plan["cuts"]

        # 2) 그림체 판단 1회. 모든 컷이 같은 화풍이어야 표가 하나로 읽힌다.
        reference_base64 = None
        if reference_key:
            try:
                reference_base64 = load_style_reference(reference_key, owner)
            except Exception:  # noqa: BLE001 - 참조 실패가 콘티 생성을 막을 이유는 없다
                logger.warning("Storyboard style reference unusable key=%s", reference_key)

        if preset_id:
            preset = styles.get_preset(preset_id)
            decision = styles.route(
                "", purpose="storyboard", has_reference=bool(reference_base64)
            )
            decision.update(
                {
                    "presetId": preset["id"],
                    "presetLabel": preset["label"],
                    "lead": preset["lead"],
                    "stylePrompt": preset["prompt"],
                    "negativePrompt": f"{preset['negative']}, {styles.BASE_NEGATIVE}",
                    "source": "manual",
                    "reason": "기획자가 프리셋을 직접 선택했습니다.",
                }
            )
            decision.update(
                styles.apply_min_tier(preset, styles.PURPOSE_TIERS["storyboard"], decision["reason"])
            )
            if board["stylePrompt"].strip():
                decision["stylePrompt"] += f", {board['stylePrompt'].strip()[:300]}"
        else:
            decision = styles.route(
                board["stylePrompt"],
                purpose="storyboard",
                has_reference=bool(reference_base64),
                caller=style_router_caller(owner),
            )

        model = image_model(tier=decision["tier"], style_reference=bool(reference_base64))
        adapter = providers.resolve(model)

        graph_store.update_storyboard(
            storyboard_id,
            {
                "status": graph_store.SB_DRAWING,
                "title": plan["title"] or board["title"],
                "cutsJson": json.dumps(cuts, ensure_ascii=False),
                "cutCount": len(cuts),
                "presetId": decision["presetId"],
                "presetLabel": decision["presetLabel"],
                "styleReason": decision["reason"],
                "modelId": model["id"],
                "modelLabel": model["label"],
                # 컷을 개별로 다시 그릴 때 같은 인물·그림체로 그리려면 이 판단을 남겨야 한다.
                "sharedJson": json.dumps(
                    {"characters": plan["characters"], "setting": plan["setting"]},
                    ensure_ascii=False,
                ),
                "styleJson": json.dumps(decision, ensure_ascii=False),
            },
        )

        # 3) 컷마다 그림. 순차로 만든다. 이미지 모델은 동기 호출이라 한 장이 5~10초다.
        deadline = time.time() + STORYBOARD_BUDGET_SECONDS * max(1, len(cuts))
        now = datetime.now(timezone.utc)
        prefix = f"users/{safe_user_prefix(owner)}/images/{now:%Y/%m/%d}"

        for index, cut in enumerate(cuts):
            if time.time() >= deadline:
                cut.update({"status": "failed", "error": "전체 제한 시간을 초과했습니다."})
                continue
            try:
                scene = documents.storyboard_scene_prompt(cut, plan["characters"], plan["setting"])
                request_body = adapter.build_request(
                    model,
                    {
                        "prompt": styles.compose_prompt(scene, decision),
                        "negativePrompt": decision["negativePrompt"],
                        "aspectRatio": board["aspectRatio"],
                        "referenceBase64": reference_base64,
                    },
                )
                image_bytes = gateway.invoke_image(
                    model, request_body, actor=owner, purpose="image:storyboard"
                )[0]
                key = f"{prefix}/{storyboard_id[:8]}_cut{cut['number']:02d}.png"
                store_bytes(S3_BUCKET, key, image_bytes, "image/png", AWS_REGION)
                cut.update({"status": "done", "error": "", "imageKey": key})
            except (providers.ProviderError, ValueError, BotoCoreError, ClientError) as error:
                # 한 컷이 실패해도 나머지는 계속 그린다. 표가 통째로 비는 것이 더 나쁘다.
                logger.warning("Storyboard cut failed id=%s cut=%s: %s", storyboard_id, cut["number"], error)
                cut.update({"status": "failed", "error": str(error)[:200]})

            done = sum(1 for item in cuts if item["status"] == "done")
            failed = sum(1 for item in cuts if item["status"] == "failed")
            graph_store.update_storyboard(
                storyboard_id,
                {
                    "cutsJson": json.dumps(cuts, ensure_ascii=False),
                    "doneCount": done,
                    "failedCount": failed,
                },
            )
            logger.info(
                "Storyboard progress id=%s %s/%s", storyboard_id, index + 1, len(cuts)
            )

        done = sum(1 for item in cuts if item["status"] == "done")
        failed = len(cuts) - done
        if done == 0:
            graph_store.update_storyboard(
                storyboard_id,
                {"status": graph_store.SB_FAILED, "error": "모든 컷 그림 생성이 실패했습니다."},
            )
            return
        graph_store.update_storyboard(
            storyboard_id,
            {
                "status": graph_store.SB_READY,
                "error": f"{failed}개 컷의 그림을 만들지 못했습니다. 설명은 그대로 남아 있습니다." if failed else "",
            },
        )
        # 콘티 한 장(표 전체)이 결과물 1건이다. 컷이 8개든 24개든 1건이다.
        # 컷마다 세면 컷 수를 고르는 것이 곧 한도 소모량이 되어, 사용자는 한도를 아끼려고
        # 4컷만 만들게 된다. 그건 이 기능을 쓰지 말라는 것과 같다.
        # 그림이 전부 실패한 위쪽 분기는 여기 오지 않으므로 차감되지 않는다.
        spend_quota(owner, QUOTA_ASSET, "storyboard", storyboard_id)
        logger.info("Storyboard ready id=%s cuts=%s/%s", storyboard_id, done, len(cuts))
    except documents.DocumentError as error:
        graph_store.update_storyboard(
            storyboard_id, {"status": graph_store.SB_FAILED, "error": str(error)}
        )
    except Exception as error:  # noqa: BLE001 - 스레드에서 예외가 새면 상태가 굳는다
        logger.exception("Storyboard failed id=%s", storyboard_id)
        try:
            graph_store.update_storyboard(
                storyboard_id,
                {"status": graph_store.SB_FAILED, "error": f"예상치 못한 오류: {error}"},
            )
        except (graph_store.GraphError, graph_store.NotFoundError):
            pass


def owned_storyboard_for_edit(storyboard_id: str):
    """편집용으로 콘티를 가져온다. 실패하면 (None, 오류응답)을 돌려준다."""
    board = graph_store.get_storyboard(storyboard_id)
    if not board:
        return None, response_error("콘티를 찾을 수 없습니다.", 404, "STORYBOARD_NOT_FOUND")
    if board["owner"] != g.username:
        return None, response_error("본인 콘티만 수정할 수 있습니다.", 403, "STORYBOARD_FORBIDDEN")
    if board["status"] in graph_store.SB_ACTIVE_STATUSES:
        return None, response_error(
            "생성이 끝난 뒤에 수정할 수 있습니다.", 409, "STORYBOARD_BUSY"
        )
    return board, None


def explain_video_failure(message: str) -> str:
    """비동기 영상 생성 실패 메시지를 **무엇을 하면 되는지 아는 문장**으로 바꾼다.

    예전에는 Bedrock 이 준 영어 원문을 그대로 화면에 띄웠다. 실제로 사용자가 본 것은 이것이다.

        만들지 못했습니다
        0:49
        image moderation failed

    이 문장으로는 무엇이 문제인지도, 다음에 무엇을 해야 하는지도 알 수 없다. 원문은 진단용으로
    함께 돌려주고(화면의 상세 항목), 사용자에게는 원인과 다음 행동을 보여 준다.
    같은 태도를 이미 두 곳에서 쓰고 있다(video-gen/server.py 의 explain_failure,
    gateway.invoke_image 의 finish_reason 분기).

    ## 콘텐츠 필터가 이 서비스에서 특히 자주 걸리는 이유 (실험으로 확인)

    소재가 웹툰이다. `.build/moderation-test.py` 로 같은 컷의 변형 10종을 Luma 에 실제
    제출해 무엇이 신호인지 좁혔다(결과: `.build/moderation-test-results.csv`).

        통과  사람이 없는 합성 이미지        2/2
        통과  인물 한 명만 남긴 컷           2/2
        통과  같은 장면을 실사로 바꾼 것      2/2  (나이를 명시하지 않아도 통과)
        실패  원본 (2인)                    0/4
        실패  넓은 여백을 둬 피사체를 줄인 것  1/5
        실패  말풍선을 지운 것               0/1
        실패  RGB 변환·업스케일·비율 패딩      0/1 씩

    읽는 방법은 이렇다. **일러스트 화풍 + 인물 2인**이 신호다. 한 명을 지우면 통과하고,
    같은 구도를 실사로 바꿔도 통과한다. 즉 필터는 작화를 보고 나이를 판단하고 있다.
    말풍선·해상도·비율·여백은 원인이 아니다. 넓은 여백을 둔 변형이 5번 중 1번 통과한 것으로
    보아 판정에 무작위성이 있지만, 20%에 기대어 재시도할 만한 수준이 아니다.

    그래서 안내는 두 가지만 말한다. **인물이 한 명인 컷**으로 바꾸는 것과, 외부 필터가 없는
    경로(NEW_영상 제작, 자체 GPU 의 Wan 2.2)로 가는 것이다. 둘 다 실제로 통하는 길이다.
    여백을 두거나 이미지를 키우라는 안내는 하지 않는다. 통하지 않는 것을 시키면 사용자가
    그 시간을 버린다.

    자동 재시도도 넣지 않았다. 실패 판정은 1분쯤 걸리는데 통과 확률이 20%라, 재시도는
    대기 시간만 배로 늘리고 결과는 대개 같다. 쓸 수 있는 대안이 이미 있으므로 빨리 알려
    주는 편이 낫다.
    """
    raw = " ".join((message or "").split())
    lowered = raw.lower()
    if not raw:
        return "영상 생성이 실패했습니다. 잠시 후 다시 시도해 주세요."

    if "moderation" in lowered:
        # 이미지와 프롬프트 중 어느 쪽이 걸렸는지 메시지가 구분해 준다. 조치가 다르다.
        if "image" in lowered or "frame" in lowered:
            return (
                "업로드한 이미지가 모델(Luma)의 콘텐츠 안전 필터에 걸려 거부됐습니다."
                " 프롬프트를 고쳐도, 이미지를 키우거나 여백을 넣어도 통과하지 않습니다."
                " 실제로 확인해 보니 웹툰 작화에 인물이 둘 이상 함께 있는 컷이 거부되고,"
                " 인물이 한 명인 컷은 통과했습니다."
                " 인물 한 명이 담긴 컷으로 바꾸거나,"
                " 외부 필터를 거치지 않는 [NEW_영상 제작](자체 GPU)에서 만들어 주세요."
            )
        return (
            "프롬프트가 모델의 콘텐츠 안전 필터에 걸려 거부됐습니다."
            " 폭력·선정적 표현을 덜어내고 장면 묘사 중심으로 다시 적어 주세요."
        )
    if "too large" in lowered or "1552" in lowered:
        return (
            "이미지가 모델이 받는 크기를 넘었습니다. 서버가 줄여서 보내지만 실패했다면"
            " 배포 담당자 확인이 필요합니다."
        )
    if "unsupported" in lowered or "invalid image" in lowered or "decode" in lowered:
        return (
            "이미지를 모델이 읽지 못했습니다. PNG 또는 JPG 로 다시 올려 주세요."
        )
    if "throttl" in lowered or "too many" in lowered:
        return (
            "모델이 동시에 처리할 수 있는 한도를 넘었습니다."
            " 앞 작업이 끝난 뒤 다시 시도해 주세요."
        )
    if "internal" in lowered or "unavailable" in lowered:
        return "모델 쪽 일시적인 오류입니다. 잠시 후 다시 시도해 주세요."

    # 분류되지 않은 실패. 그래도 원문만 던지지 않고 무엇을 볼지 알려 준다.
    return f"영상 생성이 실패했습니다: {raw[:300]}"


def collect_generated_video(output_uri: str, region: str) -> dict:
    """Copy the finished mp4 into the main bucket so the library stays one place."""
    if not output_uri.startswith("s3://"):
        return {}
    without_scheme = output_uri[len("s3://"):]
    source_bucket, _, source_prefix = without_scheme.partition("/")
    client = s3_client(region)

    listing = client.list_objects_v2(Bucket=source_bucket, Prefix=source_prefix.rstrip("/"))
    videos = [
        item
        for item in listing.get("Contents", [])
        if Path(item["Key"]).suffix.lower() in VIDEO_EXTENSIONS
    ]
    if not videos:
        return {"ready": False}

    newest = max(videos, key=lambda item: item["LastModified"])
    source_key = newest["Key"]

    if source_bucket == S3_BUCKET:
        return {
            "ready": True,
            "videoKey": source_key,
            "videoUrl": presign(S3_BUCKET, source_key, AWS_REGION),
        }

    now = datetime.now(timezone.utc)
    target_key = (
        f"users/{safe_user_prefix(g.username)}/videos/{now:%Y/%m/%d}/"
        f"{uuid.uuid4().hex}_generated.mp4"
    )
    body = client.get_object(Bucket=source_bucket, Key=source_key)["Body"].read()
    store_bytes(S3_BUCKET, target_key, body, "video/mp4", AWS_REGION)
    logger.info("Generated video collected key=%s bytes=%s", target_key, len(body))
    return {
        "ready": True,
        "videoKey": target_key,
        "videoUrl": presign(S3_BUCKET, target_key, AWS_REGION),
        "sourceUri": f"s3://{source_bucket}/{source_key}",
    }


@app.get("/api/dashboard")
@require_auth
def dashboard():
    try:
        assets_list = list_objects("uploads")
        videos_list = list_objects("videos", VIDEO_EXTENSIONS)
    except (BotoCoreError, ClientError):
        logger.exception("S3 dashboard failed user=%s", g.username)
        return response_error("저장소 현황을 불러오지 못했습니다.", 503, "STORAGE_ERROR")

    try:
        models = model_registry.enabled_models()
        active_model = next((model for model in models if model.get("isDefault")), None)
    except (BotoCoreError, ClientError, model_registry.CatalogError):
        models, active_model = [], None

    try:
        buckets = graph_store.list_visible_projects(g.username)
        project_counts = {"shared": len(buckets["shared"]), "private": len(buckets["private"])}
    except graph_store.GraphError:
        project_counts = {"shared": 0, "private": 0}

    return jsonify(
        {
            "ok": True,
            "counts": {
                "assets": len(assets_list),
                "videos": len(videos_list),
                "models": len(models),
                "sharedProjects": project_counts["shared"],
                "privateProjects": project_counts["private"],
            },
            "storage": {
                "bucket": S3_BUCKET,
                "prefix": f"users/{safe_user_prefix(g.username)}/",
                "videoBytes": sum(item["size"] for item in videos_list),
            },
            "activeModel": public_model(active_model, False) if active_model else None,
        }
    )


# --------------------------------------------------------------------------- #
# NEW_영상 제작 (자체 GPU의 Wan 2.2 I2V-A14B 전용)
# --------------------------------------------------------------------------- #
# 지시서 1-2: 기존 Bedrock 기반 기능과 코드·라우팅이 섞이지 않도록 분리된 페이지·엔드포인트로
# 구성한다. 그래서 경로가 /api/v2/video/* 이고 정점도 wanjob 으로 따로 둔다.
#
# 기존 /api/videos/generate 와 무엇이 다른가:
#   기존  = Bedrock 비동기 호출(Luma). Bedrock이 job과 출력 버킷을 관리한다.
#   NEW   = 우리가 띄운 추론 서버. 앱이 진행 상황을 폴링하고 결과 mp4를 받아 S3에 올린다.
#
# 두 경로가 공유하는 것은 프롬프트 보강(prompt_boost)과 그림체 판단(styles.route)뿐이다.
# 지시서 9-3이 정한 대로 전처리용 텍스트 LLM은 분리 규칙과 무관하게 기존대로 쓴다.
WAN_POLL_SECONDS = int(os.getenv("WAN_POLL_SECONDS", "20"))
# 480p 실측 약 15분, 720p는 오프로딩이 겹쳐 훨씬 느리다. 무한정 붙잡지 않도록 상한을 둔다.
WAN_BUDGET_SECONDS = int(os.getenv("WAN_BUDGET_SECONDS", "5400"))
# 워커 스레드가 죽었는지 판단하는 기준. 워커가 살아 있으면 폴링마다 updatedAt을 찍는다.
WAN_STALE_MINUTES = int(os.getenv("WAN_STALE_MINUTES", "15"))
# 폴링이 끊긴 것으로 보고 **다시 맡기** 시작하는 기준. 실패로 적는 기준(위)과 다르다.
#
# 두 값을 나눈 이유가 있다. 재부착은 안전한 동작이고(추론 서버에 물어보기만 한다) 빠를수록
# 좋다. 실패 표시는 되돌릴 수 없어서 늦어야 한다. 예전에는 기준이 하나뿐이라 배포로 폴링이
# 끊긴 작업을 15분 뒤에 곧바로 실패로 적었고, 그 사이 GPU가 완성한 영상이 버려졌다.
# 정상 워커는 WAN_POLL_SECONDS(20초)마다 찍으므로 90초면 죽은 것으로 봐도 안전하다.
WAN_ADOPT_AFTER_SECONDS = int(os.getenv("WAN_ADOPT_AFTER_SECONDS", "90"))


def wan_prompt_caller(actor: str):
    """Wan 프롬프트 변환용 호출자. 첨부 이미지를 읽어야 해서 비전 모델을 쓴다."""

    def call(system: str, content: list[dict], max_tokens: int) -> str:
        text, _ = gateway.converse(
            VIDEO_PROMPT_MODEL,
            AWS_REGION,
            system,
            content,
            max_tokens=max_tokens,
            temperature=0.2,
            actor=actor,
            purpose="video:wan-prompt",
            long_running=False,
        )
        return text

    return call


def wanjob_payload(job: dict) -> dict:
    """사용자에게 보낼 형태. 완성된 영상은 presigned URL로 준다."""
    view = dict(job)
    view["videoUrl"] = (
        presign(S3_BUCKET, job["outputKey"], AWS_REGION) if job.get("outputKey") else None
    )
    view["sourceUrl"] = (
        presign(S3_BUCKET, job["sourceKey"], AWS_REGION) if job.get("sourceKey") else None
    )
    # 참고 이미지 갤러리. 이름을 함께 줘서 어느 파일이었는지 알 수 있게 한다.
    names = job.get("sourceNames") or []
    sources = []
    for index, key in enumerate(job.get("sourceKeys") or []):
        try:
            url = presign(S3_BUCKET, key, AWS_REGION)
        except (BotoCoreError, ClientError):
            url = None
        sources.append(
            {
                "key": key,
                "name": names[index] if index < len(names) else "",
                "url": url,
                "isPrimary": key == job.get("sourceKey"),
            }
        )
    view["sources"] = sources
    return view


_wan_adopted: set[str] = set()
_wan_adopt_lock = threading.Lock()


def wan_profile_json(remote: dict) -> str:
    """추론 서버가 준 스테이지 프로파일을 Neptune 에 담을 JSON 문자열로 만든다.

    ## 왜 앱이 이것을 저장하는가

    추론 서버는 프로파일을 자기 프로세스 메모리에만 갖는다. 파드가 재시작되면 사라지고,
    추론 서버는 클러스터 내부에만 있어 워크스테이션에서 조회할 수도 없다. 기준선은
    며칠에 걸쳐 여러 번 재서 비교하는 값이라(품질·속도 계획서 6장) 남지 않으면 쓸모가 없다.

    ## 왜 통째로 담지 않고 골라 담는가

    Neptune 속성에는 크기 제한이 있고, 추론 서버 응답에는 logTail 처럼 큰 값이 섞여 있다.
    벤치에 필요한 것만 고르고 상한을 둔다. 상한을 넘으면 저장 자체가 실패해
    **작업 기록 갱신이 통째로 실패**한다(영상은 만들어졌는데 ready 로 못 바꾼다).
    그래서 자르는 것이 아니라 넘칠 때 비운다 — 프로파일을 잃는 것이 결과를 잃는 것보다 낫다.
    """
    import json as _json

    profile = remote.get("profile")
    if not isinstance(profile, dict):
        return "{}"
    picked = {
        "queueWaitSeconds": profile.get("queueWaitSeconds"),
        "stageSeconds": profile.get("stageSeconds"),
        "experts": profile.get("experts"),
        "peakVramMiB": profile.get("peakVramMiB"),
        # 무엇으로 만든 값인지 함께 남긴다. 이것이 없으면 다른 구성의 수치와 섞인다.
        "size": remote.get("size"),
        "steps": remote.get("steps"),
        "frames": remote.get("frames"),
        "seed": remote.get("seed"),
        "elapsedSeconds": remote.get("elapsedSeconds"),
    }
    text = _json.dumps(
        {key: value for key, value in picked.items() if value is not None},
        ensure_ascii=False,
    )
    return text if len(text) <= 4000 else "{}"


def _scene_key(owner: str, scene_id: str, *parts: str) -> str:
    return f"users/{safe_user_prefix(owner)}/scenes/{scene_id}/" + "/".join(parts)


def _character_key(owner: str, character_id: str, *parts: str) -> str:
    return f"users/{safe_user_prefix(owner)}/characters/{character_id}/" + "/".join(parts)


def _can_access_video_scene(scene: dict) -> bool:
    """장면 접근 권한. 소유자와 함께 **소속 프로젝트 권한**도 본다.

    장면·계획·생성 작업·결과 영상·리뷰는 모두 장면에 매달려 있다. 그래서 소속을
    중복 필드로 흩뿌리지 않고 `video_scene.projectId` 한 곳에서 해석한다(요구 4).

    프로젝트가 붙어 있으면 그 프로젝트를 읽을 수 없는 사람은 장면도 볼 수 없다.
    프로젝트를 개인으로 되돌렸는데 장면만 열리는 구멍을 막는다.

    projectId 가 빈 장면(옛 데이터)은 소유자 기준으로만 판정한다. 여기서 막으면
    이관 전 기존 작업이 통째로 잠긴다 — 단계적 적용의 핵심이다.
    """
    if scene.get("owner") != g.username and g.role != ROLE_ADMIN:
        return False
    project_id = str(scene.get("projectId") or "")
    if not project_id:
        return True
    try:
        project = graph_store.get_project(project_id)
    except graph_store.GraphError:
        # 프로젝트를 확인할 수 없으면 소유자 판정만 남긴다. 그래프 장애로 본인 작업이
        # 잠기지 않게 한다 — 이 함수는 소유자 검사를 이미 통과한 뒤다.
        return True
    if not project:
        # 프로젝트가 삭제된 장면. 소유자는 계속 접근할 수 있어야 결과를 회수한다.
        return True
    return can_read_project(project)


def _scene_project_or_error(scene: dict, *, write: bool):
    """장면의 소속 프로젝트를 해석하고 권한을 확인한다.

    `(project|None, 오류응답|None)`. 프로젝트가 없는 옛 장면은 `(None, None)` 이다 —
    호출부가 "아직 이관되지 않았다"를 구분할 수 있어야 한다.
    """
    project_id = str(scene.get("projectId") or "")
    if not project_id:
        return None, None
    project = graph_store.get_project(project_id)
    if not project:
        return None, None
    allowed = can_write_project(project) if write else can_read_project(project)
    if not allowed:
        return None, response_error(
            "이 작업이 속한 프로젝트에 접근할 권한이 없습니다.", 403, "PROJECT_FORBIDDEN"
        )
    return project, None


def _can_access_character(character: dict) -> bool:
    return character.get("owner") == g.username or g.role == ROLE_ADMIN


def _load_character_sheet_json(character: dict) -> dict:
    """`sheetKey`가 가리키는 S3 sheet.json을 읽는다. 아직 없으면 빈 sheet."""
    sheet_key = character.get("sheetKey")
    if not sheet_key:
        return {}
    try:
        return json.loads(s3.get_object(Bucket=S3_BUCKET, Key=sheet_key)["Body"].read())
    except (BotoCoreError, ClientError, ValueError, KeyError):
        logger.warning("캐릭터 시트 읽기 실패 character=%s key=%s", character.get("id"), sheet_key)
        return {}


PRODUCTION_ANALYZING = "analyzing"
PRODUCTION_QUESTIONS = "questions"
PRODUCTION_PLAN_AWAITING = "plan_awaiting"
PRODUCTION_REFERENCE_GENERATING = "reference_generating"
PRODUCTION_REFERENCE_REVIEW = "reference_review"
PRODUCTION_I2V_GENERATING = "i2v_generating"
PRODUCTION_DONE = "done"
PRODUCTION_FAILED = "failed"


def _production_key(owner: str, scene_id: str) -> str:
    return _scene_key(owner, scene_id, "production", "state.json")


def _load_production(scene: dict) -> dict | None:
    key = scene.get("productionKey") or _production_key(scene.get("owner") or "", scene["id"])
    try:
        value = json.loads(s3.get_object(Bucket=S3_BUCKET, Key=key)["Body"].read())
    except (BotoCoreError, ClientError, ValueError, KeyError):
        return None
    return value if isinstance(value, dict) else None


def _save_production(scene: dict, production: dict, *, stage: str = "") -> dict:
    """production JSON과 scene 인덱스를 같이 갱신한다.

    JSON이 정본(질문·답·계획·검수 근거), graph 속성은 목록/새로고침/멱등성 조회에
    필요한 작은 인덱스다. 둘을 따로 갱신하면 화면이 다른 상태를 보므로 한 함수로 둔다.
    """
    if stage:
        production["stage"] = stage
    production["updatedAt"] = datetime.now(timezone.utc).isoformat()
    key = scene.get("productionKey") or _production_key(scene["owner"], scene["id"])
    store_bytes(
        S3_BUCKET, key, json.dumps(production, ensure_ascii=False).encode("utf-8"),
        "application/json", AWS_REGION,
    )
    return graph_store.update_video_scene(
        scene["id"],
        {
            "productionKey": key,
            "productionStage": production.get("stage") or "",
            "error": production.get("error") or "",
        },
    )


PRODUCTION_CANCELLED = "cancelled"

# 사용자가 이전 단계로 돌아갈 때 어디로 가는지. 현 단계와 **그 결과에 의존하는 이후
# 단계**만 무효화하고, 입력과 완료된 이전 결과는 건드리지 않는다.
#
# 영상 생성에서 돌아가면 계획 확인·수정으로 간다(사용자 예시). 계획을 다시 승인해야
# 새 제작이 시작되므로, 돌아가는 것만으로는 유료 제출이 일어나지 않는다.
PRODUCTION_STEP_BACK = {
    PRODUCTION_QUESTIONS: PRODUCTION_QUESTIONS,
    PRODUCTION_PLAN_AWAITING: PRODUCTION_QUESTIONS,
    PRODUCTION_REFERENCE_GENERATING: PRODUCTION_PLAN_AWAITING,
    PRODUCTION_REFERENCE_REVIEW: PRODUCTION_PLAN_AWAITING,
    PRODUCTION_I2V_GENERATING: PRODUCTION_PLAN_AWAITING,
    PRODUCTION_FAILED: PRODUCTION_PLAN_AWAITING,
    PRODUCTION_CANCELLED: PRODUCTION_PLAN_AWAITING,
    PRODUCTION_DONE: PRODUCTION_PLAN_AWAITING,
}

# 실행 중으로 볼 단계. 이 단계에서 돌아가거나 취소하면 백엔드 작업을 실제로 접어야 한다.
PRODUCTION_RUNNING_STAGES = (
    PRODUCTION_ANALYZING,
    PRODUCTION_REFERENCE_GENERATING,
    PRODUCTION_I2V_GENERATING,
)


def _new_run_id() -> str:
    return uuid.uuid4().hex


def _cancel_requested(scene: dict, run_id: str) -> bool:
    """이 실행이 취소됐는지. **그래프가 정본이다.**

    실행 ID로 비교한다. 그래야 취소한 실행의 늦은 응답이 그다음 새 실행을 덮어쓰지
    않는다. 파드가 재시작하거나 큐가 같은 메시지를 다시 전달해도 이 값이 남아 있어
    취소된 작업이 되살아나지 않는다.
    """
    if not run_id:
        return False
    return str((scene or {}).get("cancelRequestedRun") or "") == str(run_id)


def _cancel_checkpoint(scene_id: str, run_id: str) -> bool:
    """단계 사이 취소 확인. 최신 상태를 다시 읽는다(캐시된 dict 를 믿지 않는다)."""
    if not run_id:
        return False
    try:
        fresh = graph_store.get_video_scene(scene_id)
    except graph_store.GraphError:
        # 조회 실패를 취소로 해석하지 않는다. 진행 중 작업을 그래프 장애로 버리지 않는다.
        return False
    return _cancel_requested(fresh or {}, run_id)


def _production_question(question_id: str, topic: str, question: str, options: list[str], why: str) -> dict:
    return {"id": question_id, "topic": topic, "question": question, "options": options, "why": why}


def _text_only_questions(description: str) -> list[dict]:
    """이미지가 없을 때 필요한 것만 묻는다. 이미 적은 정보는 다시 묻지 않는다."""
    lower = description.lower()
    questions: list[dict] = []
    # 장면 설명에 동작 단서가 없을 때만 묻는다.
    action_words = ("걷", "돌", "보", "들", "잡", "손", "고개", "앉", "일어나", "웃", "울", "달리")
    if not any(word in description for word in action_words):
        questions.append(_production_question(
            "motion", "motion", "이 장면에서 인물이 어떤 한 가지 행동을 하나요?",
            ["고개를 돌려 바라보기", "손을 뻗어 물건 잡기", "두 걸음 걷기", "조용히 숨 쉬며 시선 바꾸기"],
            "한 샷에는 한 가지 핵심 동작만 넣어야 I2V가 과장되지 않습니다.",
        ))
    mood_words = ("밤", "낮", "따뜻", "차가", "긴장", "설렘", "비", "조용", "밝", "어두")
    if not any(word in description for word in mood_words):
        questions.append(_production_question(
            "mood", "scene", "원하는 분위기와 조명은 어떤 쪽인가요?",
            ["따뜻하고 차분한 실내", "차갑고 긴장된 밤", "밝은 낮의 일상", "부드러운 감정 장면"],
            "조명과 분위기는 기준 이미지와 영상의 색감을 함께 결정합니다.",
        ))
    # 인물이 언급된 경우에만 이름/관계/특징을 묻는다. 없는 정보를 억지로 만들지 않는다.
    person_words = (
        "남자", "여자", "인물", "캐릭터", "그", "그녀", "두 사람", "커플",
        "man", "men", "woman", "women", "person", "people", "character", "lover", "romantic", "couple",
    )
    if any(word in description for word in person_words):
        questions.append(_production_question(
            "characters", "character", "등장인물의 이름과 눈에 띄는 외형이 있으면 적어 주세요. 없으면 건너뛰어도 됩니다.",
            [], "기준 이미지에서 같은 인물을 유지하는 근거로 사용합니다.",
        ))
        if any(word in lower for word in ("두 ", "커플", "연인", "사랑", "관계", "lover", "romantic", "couple")):
            questions.append(_production_question(
                "relationship", "relation", "두 인물의 관계를 어떻게 표현할까요? 이미 적은 관계는 그대로 유지합니다.",
                [], "관계 자체와 화면에서의 행동 수위는 별도로 계획합니다.",
            ))
    return questions


def _filter_production_questions(questions: list[dict], answers: dict) -> list[dict]:
    """답한 질문은 다시 묻지 않는다. 질문 id가 유일한 영속 키다."""
    return [item for item in questions if not str((answers or {}).get(item.get("id")) or "").strip()]


def _run_production_analysis(scene_id: str, owner: str) -> None:
    scene = graph_store.get_video_scene(scene_id)
    if not scene:
        return
    production = _load_production(scene)
    if not production:
        return
    try:
        keys = production.get("assetKeys") or []
        if keys:
            images, error = collect_reference_images(keys, owner, load_bytes=True)
            if error or not images:
                raise ValueError(error or "업로드 이미지를 읽지 못했습니다.")
            brief = image_brief.analyze(
                image_brief_caller(owner), images, production.get("description") or "", target=image_brief.TARGET_I2V
            )
            questions = brief.get("questions") or []
            # character sheet 또는 장면 인물이 있지 않으면 캐릭터/관계 질문을 만들지 않는다.
            has_characters = any(item.get("role") in (image_brief.ROLE_SCENE, image_brief.ROLE_CHARACTER) for item in brief.get("images") or [])
            if not has_characters:
                questions = [q for q in questions if q.get("topic") not in ("character", "relation")]
            production["brief"] = brief
        else:
            questions = _text_only_questions(production.get("description") or "")
            production["brief"] = {"summary": production.get("description") or "", "images": [], "questions": questions, "degraded": False}
        production["questions"] = _filter_production_questions(questions, production.get("answers") or {})
        production["error"] = ""
        _save_production(scene, production, stage=PRODUCTION_QUESTIONS if production["questions"] else PRODUCTION_PLAN_AWAITING)
    except Exception as error:  # noqa: BLE001 - thread must reach a terminal state
        logger.exception("Production analysis failed scene=%s", scene_id)
        production["error"] = f"분석을 완료하지 못했습니다: {error}"
        _save_production(scene, production, stage=PRODUCTION_FAILED)


def _stage_or_error(scene_id: str, *, write: bool = True):
    """장면·제작 상태를 함께 읽고 권한을 확인한다. 취소·이전 단계 API의 공통 진입."""
    scene = graph_store.get_video_scene(scene_id)
    if not scene:
        return None, None, response_error("장면을 찾을 수 없습니다.", 404, "SCENE_NOT_FOUND")
    if scene.get("owner") != g.username:
        return None, None, response_error("본인 작업만 조작할 수 있습니다.", 403, "SCENE_FORBIDDEN")
    _project, project_error = _scene_project_or_error(scene, write=write)
    if project_error:
        return None, None, project_error
    production = _load_production(scene)
    if not production:
        return None, None, response_error(
            "대화형 제작 작업을 찾을 수 없습니다.", 404, "PRODUCTION_NOT_FOUND"
        )
    return scene, production, None


def _owned_revisable_scene(scene_id: str):
    """수정 요청을 받을 수 있는 장면. 완성본과 실패한 제작 모두 허용한다.

    실패한 제작을 제외하면 사용자는 실패 화면에서 아무것도 바꿀 수 없고 처음부터
    다시 입력해야 한다. 취향 반영은 owner 전용이라 공유 열람자는 제외한다.
    """
    scene = graph_store.get_video_scene(scene_id)
    if not scene:
        return None, response_error("장면을 찾을 수 없습니다.", 404, "SCENE_NOT_FOUND")
    if scene.get("owner") != g.username:
        return None, response_error("본인이 만든 영상만 수정 요청할 수 있습니다.", 403, "SCENE_FORBIDDEN")
    finished = bool(scene.get("finalKey")) and scene.get("status") in (
        graph_store.SCENE_DONE, graph_store.SCENE_NEEDS_REVIEW,
    )
    failed = (
        str(scene.get("productionStage") or "") == PRODUCTION_FAILED
        or scene.get("status") == graph_store.SCENE_FAILED
    )
    if not finished and not failed:
        return None, response_error(
            "생성이 끝난 뒤에 수정 요청을 할 수 있습니다.", 409, "SCENE_IN_PROGRESS",
            action="생성이 끝나면 결과 카드에서 수정 요청을 보낼 수 있습니다.",
        )
    return scene, None


def _owned_finished_scene(scene_id: str):
    scene = graph_store.get_video_scene(scene_id)
    if not scene:
        return None, response_error("장면을 찾을 수 없습니다.", 404, "SCENE_NOT_FOUND")
    # 평가는 owner만 쓴다. shared project의 read 권한자가 타인의 결과 취향을 바꾸지 않는다.
    if scene.get("owner") != g.username:
        return None, response_error("본인이 만든 영상만 평가할 수 있습니다.", 403, "SCENE_FORBIDDEN")
    if not scene.get("finalKey") or scene.get("status") not in (graph_store.SCENE_DONE, graph_store.SCENE_NEEDS_REVIEW):
        return None, response_error("완성된 영상만 평가할 수 있습니다.", 409, "SCENE_NOT_FINISHED")
    return scene, None


def _cut_sequence_key(owner: str, scene_id: str) -> str:
    return _scene_key(owner, scene_id, "input", "cut_sequence.json")


def _load_cut_sequence(scene: dict) -> dict | None:
    key = _cut_sequence_key(scene.get("owner") or g.username, scene["id"])
    try:
        return json.loads(s3.get_object(Bucket=S3_BUCKET, Key=key)["Body"].read())
    except (BotoCoreError, ClientError, ValueError, KeyError):
        return None


def _store_cut_sequence(owner: str, scene_id: str, sequence: dict) -> None:
    store_bytes(
        S3_BUCKET,
        _cut_sequence_key(owner, scene_id),
        json.dumps(sequence, ensure_ascii=False).encode("utf-8"),
        "application/json",
    )


def _scene_or_error(scene_id: str):
    """장면을 읽고 소유권을 확인한다. `(scene, None)` 또는 `(None, 오류응답)`."""
    scene = graph_store.get_video_scene(scene_id)
    if not scene:
        return None, response_error("장면을 찾을 수 없습니다.", 404, "SCENE_NOT_FOUND")
    if not _can_access_video_scene(scene):
        return None, response_error("본인 장면만 접근할 수 있습니다.", 403, "SCENE_FORBIDDEN")
    return scene, None


_scene_adopted: set[str] = set()
_scene_adopt_lock = threading.Lock()


def _provider_output_prefix(owner: str, scene_id: str, shot_no: int, attempt: int) -> str:
    """provider 원본이 떨어질 프리픽스.

    `users/` 로 시작해야 한다 — `AI-POC-s3-access` 의 `ManageVideoOutput` 과
    `ListVideoOutputBucket` 이 그 접두만 허용한다. provider 기본값
    `_smoke/<name>/shot-<n>` 은 테넌트 키가 없어 두 고객이 같은 키를 덮어쓴다.

    시도마다 `try-N` 을 붙여 앞선 시도의 결과를 덮지 않는다 — 덮으면 "어느 시도의
    결과인가" 를 되짚을 수 없다.
    """
    return (
        f"users/{safe_user_prefix(owner)}/scenes/{scene_id}"
        f"/raw/shot-{int(shot_no)}/try-{int(attempt)}"
    )


def _submission_bucket(output_uri: str) -> str:
    value = str(output_uri or "")
    return value[5:].split("/", 1)[0] if value.startswith("s3://") else ""


def _fetch_provider_clip(output, clip_key: str, edit_seconds: float = 0.0) -> None:
    """provider 원본을 **모델 리전 버킷에서** 읽어 콘텐츠 버킷으로 옮긴다.

    결과가 앱 리전이 아니라 모델 리전에 떨어진다. 그 버킷을 그대로 화면에 노출하지
    않는다 — 수명주기가 14일이고 테넌트 격리는 콘텐츠 버킷에서 지켜진다.

    `edit_seconds` 가 생성 길이보다 짧으면 **앞 구간을 그만큼 잘라** 저장한다.
    모델은 자기 단위(Reel 6초)로만 만들므로, 2초 샷을 원하면 6초를 만들고 잘라 쓰는
    것이 정상 경로다. 자르기가 실패하면 원본을 그대로 저장한다 — 결과물을 잃지 않는다.
    """
    body = s3_client(output.region).get_object(Bucket=output.bucket, Key=output.key)["Body"].read()

    if edit_seconds and edit_seconds > 0:
        import tempfile

        tmp_dir = tempfile.mkdtemp(prefix="scene-trim-")
        src = os.path.join(tmp_dir, "src.mp4")
        dst = os.path.join(tmp_dir, "cut.mp4")
        try:
            with open(src, "wb") as handle:
                handle.write(body)
            command = [
                stitcher.ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "error",
                "-i", src, "-t", f"{edit_seconds:.2f}",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart", dst,
            ]
            result = subprocess.run(command, capture_output=True, timeout=300, check=False)
            if result.returncode == 0 and os.path.exists(dst) and os.path.getsize(dst) > 0:
                with open(dst, "rb") as handle:
                    body = handle.read()
                logger.info("생성 클립을 편집 길이로 잘랐습니다 %.2fs key=%s", edit_seconds, clip_key)
            else:
                logger.warning(
                    "클립 자르기 실패, 원본을 저장합니다 key=%s stderr=%s",
                    clip_key, result.stderr.decode("utf-8", "replace")[:200],
                )
        except Exception:  # noqa: BLE001 - 자르기 실패가 결과물 손실이 되면 안 된다
            logger.warning("클립 자르기 예외, 원본을 저장합니다 key=%s", clip_key)
        finally:
            for path in (src, dst):
                if os.path.exists(path):
                    os.unlink(path)
            os.rmdir(tmp_dir)

    store_bytes(S3_BUCKET, clip_key, body, output.content_type or "video/mp4")


_scene_queue_lock = threading.Lock()
_scene_queue_waiting: dict[str, float] = {}
# 마지막으로 화면에 알린 대기 상태. 샷마다 S3 를 왕복하지 않기 위한 캐시다.
_scene_queue_published: dict[str, str] = {}

# 대기 중 취소를 확인하는 간격. 생성 한 샷이 수십 초 걸리므로 이 정도면 충분히 빠르다.
SCENE_QUEUE_CHECK_SECONDS = 5.0
# 대기 표시를 믿지 않을 종료 단계. 취소·실패·완료를 대기 표시가 되살리면 안 된다.
_QUEUE_SETTLED_STAGES = (PRODUCTION_CANCELLED, PRODUCTION_FAILED, PRODUCTION_DONE)


def _queue_ahead(scene_id: str) -> int:
    """이 장면보다 먼저 기다리기 시작한 작업 수."""
    with _scene_queue_lock:
        started = _scene_queue_waiting.get(scene_id)
        if started is None:
            return 0
        return sum(
            1 for other, at in _scene_queue_waiting.items()
            if other != scene_id and at < started
        )


def _forget_queue_state(scene_id: str) -> None:
    """실행이 끝나면 대기 캐시를 비운다. 다음 실행이 다시 알릴 수 있게 한다."""
    _scene_queue_published.pop(scene_id, None)
    with _scene_queue_lock:
        _scene_queue_waiting.pop(scene_id, None)


def _save_plan(scene_id: str, owner: str, plan: dict) -> None:
    scene = graph_store.get_video_scene(scene_id)
    if not scene or not scene.get("planKey"):
        return
    try:
        store_bytes(
            S3_BUCKET,
            scene["planKey"],
            json.dumps(plan, ensure_ascii=False).encode("utf-8"),
            "application/json",
        )
    except (BotoCoreError, ClientError):
        logger.warning("Plan 갱신 실패 scene=%s", scene_id)


def _load_scene_plan(scene: dict) -> dict | None:
    """저장된 샷 플랜을 읽는다. 없거나 못 읽으면 None."""
    plan_key = scene.get("planKey")
    if not plan_key:
        return None
    try:
        return json.loads(s3.get_object(Bucket=S3_BUCKET, Key=plan_key)["Body"].read())
    except (BotoCoreError, ClientError, ValueError, KeyError):
        logger.warning("샷 플랜을 읽지 못했습니다 scene=%s key=%s", scene.get("id"), plan_key)
        return None


def _resolve_video_bucket_region(bucket: str) -> tuple[str | None, str]:
    """`(리전 | None, 오류코드)`. readiness 에 넘긴다.

    None 은 **"확인 못 했다"** 이고 "버킷이 없다" 가 아니다. 그 둘을 합치면 권한
    문제(명시적 Deny)가 설정 문제로 오진된다 — 실제로 그렇게 오진됐다.
    """
    try:
        location = s3_client(VIDEO_REGION).get_bucket_location(Bucket=bucket)
    except ClientError as error:
        return None, error.response.get("Error", {}).get("Code", "") or "ClientError"
    except BotoCoreError:
        return None, "BotoCoreError"
    return (location.get("LocationConstraint") or "us-east-1"), ""


STORY_IMAGE_PIXELS = int(os.getenv("STORY_IMAGE_MAX_PIXELS", "1568"))

# 대본 잡이 진척 없이 방치되면 실패로 정리하는 기준.
#
# 이 값은 "이만큼 조용하면 작업 스레드가 사라진 것" 이라는 뜻이다. 전체 실행 시간
# 예산이 아니다. 12화 대본은 정상적으로도 40분을 넘길 수 있고 그것은 실패가 아니다.
# 그 구분이 성립하는 것은 `script_job_heartbeat` 이 주기적으로 updatedAt 을 찍기
# 때문이다. 하트비트가 없으면 이 값은 "느린 작업"과 "죽은 작업"을 구별하지 못한다.
SCRIPT_STALE_MINUTES = int(os.getenv("SCRIPT_STALE_MINUTES", "25"))
# 하트비트 주기. 스테일 기준보다 훨씬 짧아야 의미가 있다.
SCRIPT_HEARTBEAT_SECONDS = int(os.getenv("SCRIPT_HEARTBEAT_SECONDS", "60"))


def story_caller(actor: str, role: str, quality: str = ""):
    """역할 하나의 gateway 호출자.

    역할 모듈은 (system, content, max_tokens, temperature) -> (text, truncated) 만 안다.
    모델 ID·리전·purpose 는 여기서 붙는다. 그래서 역할 코드에 모델 이름이 없다.

    purpose 를 역할 id 로 넘기는 것이 중요하다. gateway 가 그 값으로
      - 사용량을 기록하고 (usage.purpose)
      - prompt_layers 의 프론트엔드 프롬프트를 결합한다
    두 기능이 자동으로 붙는다. 새 purpose 를 prompt_layers.ROLES 에 등록해 두면
    관리자가 화면에서 역할별 지시를 덧붙일 수 있다.
    """
    resolved = story_models.spec(role, quality=quality or story_models.QUALITY_STANDARD)

    def call(system_text: str, content: list[dict], max_tokens: int, temperature: float):
        return gateway.converse(
            resolved["model"],
            AWS_REGION,
            system_text,
            content,
            max_tokens=max_tokens,
            temperature=temperature,
            actor=actor,
            purpose=role,
            # 회차 대본은 1.6만 토큰을 만든다. 짧은 타임아웃으로는 끝나지 않는다.
            long_running=True,
        )

    return call


def story_callers(actor: str, quality: str = "") -> dict:
    """라우터가 쓰는 역할 → 호출자 표."""
    return {role: story_caller(actor, role, quality) for role in story_models.ROLES}


class _AnalysisStore:
    """이미지 분석 캐시. S3 에 둔다.

    Neptune 에 두지 않는 이유: 분석 하나가 2KB 가까이 되고 프로젝트와 무관하게
    (같은 이미지를 다른 프로젝트에 올려도) 재사용되어야 한다. 정점으로 두면
    프로젝트 삭제 시 함께 지워지거나 고아 정점이 된다.

    키에 사용자를 넣지 않는다. 분석은 이미지 내용의 함수이고 sha256 이 같으면 결과가
    같다. 다른 사용자가 같은 이미지를 올렸을 때 다시 분석할 이유가 없다.
    프라이버시 관점에서도 노출되는 것은 없다 — 키가 sha256 해시라 목록을 나열할 수 없고,
    캐시를 읽으려면 같은 이미지를 이미 갖고 있어야 한다.
    """

    PREFIX = "_cache/story-analysis"

    def get_image_analysis(self, cache_key: str) -> dict | None:
        try:
            obj = s3.get_object(Bucket=S3_BUCKET, Key=f"{self.PREFIX}/{cache_key}.json")
            return json.loads(obj["Body"].read())
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code")
            if code in {"NoSuchKey", "404"}:
                return None
            if code == "AccessDenied":
                # 권한이 없는 것을 "캐시 없음"과 같이 다루면 캐시가 조용히 죽는다.
                # 실제로 IRSA 정책에 _cache/ 프리픽스가 빠져 매번 다시 분석한 적이 있다.
                # 요청을 실패시키지는 않되(분석은 계속 가능하다) 반드시 눈에 띄게 남긴다.
                logger.error(
                    "이미지 분석 캐시를 읽을 수 없습니다(AccessDenied). "
                    "앱 역할에 s3:GetObject %s/%s/* 권한이 필요합니다.",
                    S3_BUCKET, self.PREFIX,
                )
                return None
            raise
        except (BotoCoreError, ValueError, json.JSONDecodeError):
            return None

    def put_image_analysis(self, cache_key: str, value: dict) -> None:
        store_bytes(
            S3_BUCKET,
            f"{self.PREFIX}/{cache_key}.json",
            json.dumps(value, ensure_ascii=False).encode("utf-8"),
            "application/json",
            AWS_REGION,
        )


_analysis_store = _AnalysisStore()


def story_owned_project(project_id: str):
    """프로젝트를 가져오고 쓰기 권한을 확인한다. (project, error_response)."""
    project = graph_store.get_project(project_id)
    if not project:
        return None, response_error("프로젝트를 찾을 수 없습니다.", 404, "PROJECT_NOT_FOUND")
    if not can_read_project(project):
        return None, response_error("이 프로젝트에 접근할 권한이 없습니다.", 403, "PROJECT_FORBIDDEN")
    if not can_write_project(project):
        return None, response_error("본인 프로젝트만 수정할 수 있습니다.", 403, "PROJECT_FORBIDDEN")
    return project, None


def story_text_of(project: dict) -> str:
    """실제 스토리 본문.

    S3 키가 있으면 그것을 읽고, 없으면 옛 `story` 인라인 필드로 내려간다.
    지시서 4절 마이그레이션: "기존 스토리 → story_text". 옛 프로젝트를 쿼리로 옮기지 않고
    읽는 시점에 흡수한다 — 마이그레이션 스크립트를 돌리지 않아도 되고, 실패할 지점이 없다.
    """
    key = (project.get("storyText") or {}).get("key") or ""
    if key:
        try:
            return read_source_text(key)
        except (BotoCoreError, ClientError):
            logger.warning("실제 스토리를 읽지 못했습니다 key=%s", key)
            return ""
    return str(project.get("story") or "")


def story_key(owner: str, project_id: str, name: str) -> str:
    now = datetime.now(timezone.utc)
    return (
        f"users/{safe_user_prefix(owner)}/story/{project_id[:8]}/"
        f"{now:%Y%m%d}_{name}_{uuid.uuid4().hex[:6]}.json"
    )


def store_story_json(owner: str, project_id: str, name: str, payload: dict) -> tuple[str, int]:
    """결과물 JSON 을 S3 에 저장하고 (키, 글자수)를 돌려준다.

    지시서 0절: "생성물은 전부 AI 생성 표기(뱃지 + 파일 메타데이터)".
    뱃지는 화면이 붙이고(story_output.aiGenerated), 파일 메타데이터는 여기서 붙인다.
    파일만 따로 내려받아도 AI 생성물이라는 사실이 남는다.
    """
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    key = story_key(owner, project_id, name)
    s3.put_object(
        Bucket=S3_BUCKET,
        Key=key,
        Body=body,
        ContentType="application/json",
        ServerSideEncryption="AES256",
        Metadata={
            "ai-generated": "true",
            "generated-by": SERVICE_NAME,
            "generated-at": datetime.now(timezone.utc).isoformat(),
        },
    )
    return key, len(body.decode("utf-8"))


def story_pack_for(project: dict, output_kind: str, options: dict | None = None) -> dict:
    return story_context.new_pack(
        project["id"],
        project.get("storyType") or "",
        output_kind,
        summary_story=project.get("summaryStory") or "",
        story_text_ref=(project.get("storyText") or {}).get("key") or "",
        options=options or {},
    )


def story_images_for(project: dict, actor: str) -> tuple[list[dict], str | None]:
    """프로젝트에 첨부된 이미지를 분석 가능한 형태로 읽는다.

    캐시가 있는 것도 바이트를 읽어야 하는가: 아니다. 하지만 어느 것이 캐시에 있는지는
    sha256 을 알아야 판단할 수 있고, sha256 은 바이트를 읽어야 나온다. 그래서 저장 시점에
    sha256 을 프로젝트에 함께 기록해 두고(storyAssets[].sha256) 여기서는 그것을 쓴다.
    캐시 히트인 이미지는 바이트를 읽지 않는다 — 10장이면 그 차이가 크다.
    """
    assets = project.get("storyAssets") or []
    if not assets:
        return [], None

    model = story_models.spec(story_models.ROLE_VISION)["model"]
    images: list[dict] = []
    need_bytes: list[str] = []

    for asset in assets:
        key = str(asset.get("s3_key") or "")
        if not key:
            continue
        sha = str(asset.get("sha256") or "")
        entry = {
            "key": key,
            "sha256": sha,
            "name": str(asset.get("name") or "") or s3_original_name(key),
            "analysis": asset.get("analysis") if isinstance(asset.get("analysis"), dict) else None,
        }
        # 사용자가 고친 분석이 있으면 바이트가 필요 없다.
        if entry["analysis"] and entry["analysis"].get("edited_by_user"):
            images.append(entry)
            continue
        # 캐시에 있으면 바이트가 필요 없다.
        if sha and _analysis_store.get_image_analysis(story_context.image_cache_key(sha, model)):
            images.append(entry)
            continue
        need_bytes.append(key)
        images.append(entry)

    if need_bytes:
        loaded, error = collect_reference_images(
            need_bytes, actor, load_bytes=True, max_pixels=STORY_IMAGE_PIXELS
        )
        if error:
            return [], error
        by_key = {item["key"]: item for item in loaded}
        for entry in images:
            source = by_key.get(entry["key"])
            if source:
                entry["bytes"] = source["bytes"]
                entry["format"] = source["format"]
                if not entry["sha256"]:
                    entry["sha256"] = hashlib.sha256(source["bytes"]).hexdigest()
    return images, None


def persist_story_assets(project_id: str, project: dict, analyses: list[dict]) -> None:
    """분석 결과를 프로젝트의 storyAssets 에 되돌려 저장한다.

    캐시에만 두면 사용자가 화면에서 고친 내용을 남길 곳이 없고, 프로젝트를 다시 열었을 때
    분석 카드가 비어 보인다.
    """
    by_key = {str(item.get("asset_id") or ""): item for item in analyses}
    model = story_models.spec(story_models.ROLE_VISION)["model"]
    merged = []
    for asset in project.get("storyAssets") or []:
        key = str(asset.get("s3_key") or "")
        analysis = by_key.get(key)
        if analysis and not asset.get("edited_by_user"):
            asset = {**asset, "analysis": analysis, "analysis_model": model}
        merged.append(asset)
    try:
        graph_store.set_story_assets(project_id, merged)
    except graph_store.GraphError as error:
        logger.warning("첨부 분석 저장 실패 project=%s: %s", project_id, error)


# --------------------------------------------------------------------------- #
# 프로젝트 스토리 필드
# --------------------------------------------------------------------------- #
@app.post("/api/projects/<project_id>/story")
@require_auth
def story_save(project_id: str):
    """요약 스토리 · 실제 스토리 · 스토리 타입을 저장한다.

    한 라우트로 묶는 이유: 화면이 세 값을 한 폼에서 함께 저장한다. 나누면 부분 저장이
    가능해져 "타입은 바뀌었는데 본문은 안 바뀐" 상태가 생기고, 그 조합마다 바이블
    해시가 달라져 불필요한 재생성이 돈다.
    """
    project, error = story_owned_project(project_id)
    if error:
        return error

    payload = request.get_json(silent=True) or {}
    updates: dict = {}
    stale: list[str] = []

    if "storyType" in payload:
        story_type = str(payload.get("storyType") or "").strip()
        if story_type and story_type not in story_context.STORY_TYPES:
            return response_error(
                "스토리 타입은 대본 / 트리트먼트 / 로그라인 중 하나여야 합니다.",
                400, "INVALID_STORY_TYPE",
            )
        updates["storyType"] = story_type

    if "summaryStory" in payload:
        summary = str(payload.get("summaryStory") or "").strip()
        if len(summary) > story_context.SUMMARY_MAX_CHARS:
            return response_error(
                f"요약 스토리는 {story_context.SUMMARY_MAX_CHARS}자 이하여야 합니다.",
                400, "SUMMARY_TOO_LONG",
            )
        updates["summaryStory"] = summary

    if "storyText" in payload:
        # 길이 제한이 없다(지시서 3절). S3 에 둔다.
        text = str(payload.get("storyText") or "")
        if text.strip():
            key = story_key(g.username, project_id, "source")
            store_bytes(S3_BUCKET, key, text.encode("utf-8"), "text/plain; charset=utf-8", AWS_REGION)
            updates["storyTextKey"] = key
            updates["storyTextCharCount"] = len(text)
            previous = (project.get("storyText") or {}).get("key") or ""
            if previous and previous != key:
                stale.append(previous)
        else:
            updates["storyTextKey"] = ""
            updates["storyTextCharCount"] = 0

    if not updates:
        return response_error(
            "저장할 항목이 없습니다. summaryStory / storyText / storyType 중 하나를 보내세요.",
            400, "NOTHING_TO_SAVE",
        )

    try:
        updated = graph_store.update_project(project_id, updates)
    except graph_store.GraphError as error:
        return response_error(str(error), 400, "SAVE_FAILED")

    remove_s3_objects(stale)
    logger.info("Story saved project=%s fields=%s", project_id, list(updates))
    return jsonify({"ok": True, "item": updated})


@app.post("/api/projects/<project_id>/summary")
@require_auth
@require_quota(QUOTA_ASSET)
def story_summarize(project_id: str):
    """실제 스토리에서 요약 스토리를 자동 생성한다(지시서 3절의 버튼).

    저장하지 않고 돌려준다. 사용자가 보고 고친 뒤 저장하는 것이 맞고,
    바로 저장하면 기존 요약이 확인 없이 사라진다.
    """
    project, error = story_owned_project(project_id)
    if error:
        return error

    text = story_text_of(project)
    if not text.strip():
        return response_error("실제 스토리를 먼저 입력해 주세요.", 400, "STORY_REQUIRED")

    spec = story_models.spec(story_models.ROLE_SUMMARY)
    caller = story_caller(g.username, story_models.ROLE_SUMMARY)
    try:
        parsed, _meta = roles_json.call_json(
            caller,
            prompts_story.SUMMARIZE,
            # 요약에 원문 전체를 넣지 않는다. 앞 3만 자면 무엇에 대한 이야기인지 충분하다.
            [{"text": text[:30000]}],
            spec["max_tokens"],
            spec["temperature"],
            "summary",
        )
    except roles_json.RoleError as error:
        return response_error(f"요약을 만들지 못했습니다. {error}", 502, "SUMMARY_FAILED")

    summary = str(parsed.get("summary") or "").strip()[: story_context.SUMMARY_MAX_CHARS]
    if not summary:
        return response_error("요약이 비어 있습니다. 다시 시도해 주세요.", 502, "SUMMARY_EMPTY")

    spend_quota(g.username, QUOTA_ASSET, "story:summary", f"{project_id}:summary")
    return jsonify({"ok": True, "summary": summary})


# --------------------------------------------------------------------------- #
# 첨부 이미지 + 분석
# --------------------------------------------------------------------------- #
@app.post("/api/projects/<project_id>/assets/analyze")
@require_auth
def story_assets_analyze(project_id: str):
    """첨부 이미지를 분석한다. 캐시 히트면 모델을 부르지 않는다.

    화면은 이미지를 올릴 때 /api/assets 로 S3 에 저장하고, 그 키 목록을 여기로 보낸다.
    """
    project, error = story_owned_project(project_id)
    if error:
        return error

    payload = request.get_json(silent=True) or {}
    keys = parse_asset_keys(payload)
    if not keys:
        return response_error("분석할 이미지를 올려 주세요.", 400, "IMAGE_REQUIRED")
    if len(keys) > story_context.MAX_STORY_IMAGES:
        return response_error(
            f"이미지는 최대 {story_context.MAX_STORY_IMAGES}장까지 첨부할 수 있습니다.",
            400, "TOO_MANY_IMAGES",
        )
    for key in keys:
        if not is_owned_image_key(key, g.username):
            return response_error(
                "본인이 올리거나 만든 이미지만 사용할 수 있습니다.", 403, "ASSET_FORBIDDEN"
            )

    # 바이트를 읽어 sha256 을 구한다. 캐시 키의 근거다.
    loaded, load_error = collect_reference_images(
        keys, g.username, load_bytes=True, max_pixels=STORY_IMAGE_PIXELS
    )
    if load_error:
        return response_error(load_error, 400, "INVALID_IMAGE")
    if not loaded:
        return response_error("이미지를 읽지 못했습니다.", 400, "INVALID_IMAGE")

    # 이미 저장된 사용자 편집본은 유지한다.
    edited = {
        str(asset.get("s3_key") or ""): asset
        for asset in (project.get("storyAssets") or [])
        if asset.get("edited_by_user")
    }
    images = []
    for item in loaded:
        entry = {
            "key": item["key"],
            "sha256": hashlib.sha256(item["bytes"]).hexdigest(),
            "name": item["name"],
            "bytes": item["bytes"],
            "format": item["format"],
        }
        previous = edited.get(item["key"])
        if previous:
            entry["analysis"] = previous.get("analysis")
        images.append(entry)

    pack = story_pack_for(project, story_context.OUT_LOGLINE)
    callers = story_callers(g.username)
    try:
        pack = story_router.analyze_images(pack, images, callers, _analysis_store)
    except story_router.StoryError as error:
        return response_error(str(error), 502, "ANALYZE_FAILED")

    analyses = pack.get("image_analyses") or []
    model = story_models.spec(story_models.ROLE_VISION)["model"]

    # 프로젝트에 저장한다. 다시 열었을 때 카드가 비어 있지 않아야 한다.
    assets = []
    for entry, analysis in zip(images, analyses):
        previous = edited.get(entry["key"])
        assets.append(
            {
                "s3_key": entry["key"],
                "sha256": entry["sha256"],
                "name": entry["name"],
                "analysis": (previous or {}).get("analysis") or analysis,
                "analysis_model": model,
                "edited_by_user": bool(previous),
            }
        )
    try:
        graph_store.set_story_assets(project_id, assets)
    except graph_store.GraphError as error:
        logger.warning("첨부 목록 저장 실패 project=%s: %s", project_id, error)

    # 미리보기 URL 을 붙인다. 화면이 카드에 그림을 함께 보여준다.
    cards = []
    for asset in assets:
        card = dict(asset.get("analysis") or {})
        card["s3Key"] = asset["s3_key"]
        card["name"] = asset["name"]
        card["editedByUser"] = asset["edited_by_user"]
        try:
            card["url"] = presign(S3_BUCKET, asset["s3_key"], AWS_REGION)
        except (BotoCoreError, ClientError):
            card["url"] = None
        cards.append(card)

    totals = story_context.trace_totals(pack)
    logger.info(
        "Story analyze project=%s images=%s calls=%s cacheHits=%s",
        project_id, len(cards), totals["calls"], totals["cacheHits"],
    )
    return jsonify(
        {
            "ok": True,
            "items": cards,
            "trace": pack.get("trace") or [],
            "totals": totals,
            "safetyBlocked": roles_vision.safety_blocked(analyses),
        }
    )


@app.patch("/api/projects/<project_id>/assets/<path:asset_key>")
@require_auth
def story_asset_edit(project_id: str, asset_key: str):
    """분석 카드를 사용자가 고친다(지시서 3절: "편집 가능").

    고친 분석은 `edited_by_user=True` 가 되어 다시 분석해도 덮어쓰지 않고,
    바이블 재생성의 근거가 된다(지시서 9절 수용 기준).
    """
    project, error = story_owned_project(project_id)
    if error:
        return error

    payload = request.get_json(silent=True) or {}
    assets = list(project.get("storyAssets") or [])
    target = None
    for asset in assets:
        if str(asset.get("s3_key") or "") == asset_key:
            target = asset
            break
    if not target:
        return response_error("첨부 이미지를 찾을 수 없습니다.", 404, "ASSET_NOT_FOUND")

    analysis = dict(target.get("analysis") or {})
    # 편집 가능한 필드만 받는다. image_kind 와 usable_as 는 목록이 정해져 있어
    # 자유 입력을 받으면 화면이 라벨을 못 찾는다.
    for field in ("setting", "mood_lighting", "art_style", "text_in_image"):
        if field in payload:
            analysis[field] = str(payload.get(field) or "").strip()[:2000]
    if "characters" in payload and isinstance(payload["characters"], list):
        characters = []
        for item in payload["characters"][:8]:
            if not isinstance(item, dict):
                continue
            descriptor = str(item.get("descriptor") or "").strip()
            if not descriptor:
                continue
            characters.append(
                {
                    "descriptor": descriptor[:400],
                    "name_if_visible": str(item.get("name_if_visible") or "").strip()[:80],
                }
            )
        analysis["characters"] = characters
    analysis["edited_by_user"] = True

    target["analysis"] = analysis
    target["edited_by_user"] = True
    try:
        updated = graph_store.set_story_assets(project_id, assets)
    except graph_store.GraphError as error:
        return response_error(str(error), 400, "SAVE_FAILED")

    logger.info("Story asset analysis edited project=%s key=%s", project_id, asset_key[:60])
    return jsonify({"ok": True, "item": analysis, "project": updated})


# --------------------------------------------------------------------------- #
# 바이블
# --------------------------------------------------------------------------- #
@app.post("/api/projects/<project_id>/bible")
@require_auth
def story_bible(project_id: str):
    """바이블을 확보한다. source_hash 가 같으면 재생성하지 않는다.

    한도를 차감하지 않는다. 바이블은 사용자가 받는 결과물이 아니라 중간 표현이고,
    결과물을 만들 때마다 필요하다. 여기서 차감하면 결과물 1건에 2건이 빠진다.
    """
    project, error = story_owned_project(project_id)
    if error:
        return error
    if not (project.get("storyType") or ""):
        return response_error(
            "스토리 타입을 먼저 선택해 주세요. 대본 / 트리트먼트 / 로그라인 중 하나입니다.",
            400, "STORY_TYPE_REQUIRED",
        )

    text = story_text_of(project)
    if not text.strip():
        return response_error("실제 스토리를 먼저 입력해 주세요.", 400, "STORY_REQUIRED")

    force = bool((request.get_json(silent=True) or {}).get("force"))
    pack = story_pack_for(project, story_context.OUT_LOGLINE)
    callers = story_callers(g.username)

    images, image_error = story_images_for(project, g.username)
    if image_error:
        return response_error(image_error, 400, "INVALID_IMAGE")
    pack = story_router.analyze_images(pack, images, callers, _analysis_store)
    persist_story_assets(project_id, project, pack.get("image_analyses") or [])

    existing = None if force else (graph_store.get_story_bible(project_id) or {}).get("bible")
    try:
        pack = story_router.ensure_bible(pack, text, callers, existing)
    except story_router.StoryError as error:
        return response_error(str(error), 502, "BIBLE_FAILED")

    bible = pack.get("bible") or {}
    reused = any(
        row.get("role") == story_models.ROLE_BIBLE and row.get("cache_hit")
        for row in (pack.get("trace") or [])
    )
    if not reused:
        try:
            graph_store.save_story_bible(
                project_id, bible, story_models.spec(story_models.ROLE_BIBLE)["model"]
            )
        except graph_store.GraphError as error:
            logger.warning("바이블 저장 실패 project=%s: %s", project_id, error)

    return jsonify(
        {
            "ok": True,
            "bible": bible,
            "reused": reused,
            "trace": pack.get("trace") or [],
            "totals": story_context.trace_totals(pack),
        }
    )


@app.get("/api/projects/<project_id>/bible")
@require_auth
def story_bible_get(project_id: str):
    project = graph_store.get_project(project_id)
    if not project:
        return response_error("프로젝트를 찾을 수 없습니다.", 404, "PROJECT_NOT_FOUND")
    if not can_read_project(project):
        return response_error("이 프로젝트에 접근할 권한이 없습니다.", 403, "PROJECT_FORBIDDEN")
    stored = graph_store.get_story_bible(project_id)
    return jsonify({"ok": True, "item": stored})


def _seed_video_from_story(project: dict, bible: dict) -> tuple[str, list[dict]]:
    """스토리 → 영상 ① 입력의 글과 캐릭터(설계자 2026-10-08 "결과물 만들기 > 영상").

    글: 제목·로그라인·요약·톤/화풍·배경·주요 전개, 그리고 스토리 본문 앞부분.
    캐릭터: 작품 바이블의 인물(이름 + 역할·외형·성격). 스토리 트랙에는 인물별 이미지가 없다.
    """
    lines: list[str] = []
    title = str(bible.get("title") or project.get("title") or "").strip()
    if title:
        lines.append(f"[제목] {title}")
    if bible.get("logline"):
        lines.append(f"[로그라인] {bible['logline']}")
    if project.get("summaryStory"):
        lines.append(f"[요약] {project['summaryStory']}")
    mood = " / ".join(str(bible.get(k) or "").strip() for k in ("genre", "tone", "visual_style") if bible.get(k))
    if mood:
        lines.append(f"[장르·톤·화풍] {mood}")
    for item in (bible.get("settings") or [])[:3]:
        if isinstance(item, dict) and item.get("name"):
            lines.append(f"[배경] {item['name']}: {item.get('description') or ''}".strip())
    beats = [b.get("beat") for b in (bible.get("plot_beats") or []) if isinstance(b, dict) and b.get("beat")]
    if beats:
        lines.append("[주요 전개]")
        lines.extend(f"{index}. {beat}" for index, beat in enumerate(beats[:8], start=1))
    body = story_text_of(project).strip()
    if body:
        lines.append("[스토리 본문 일부]")
        lines.append(body[:2500] + ("…" if len(body) > 2500 else ""))
    text = "\n".join(lines)[:6000]

    characters: list[dict] = []
    for item in (bible.get("characters") or [])[:6]:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        parts = [
            str(item.get(k) or "").strip()
            for k in ("role", "age", "occupation", "appearance", "personality")
            if item.get(k)
        ]
        characters.append({"name": str(item["name"]).strip(), "description": " · ".join(parts)[:600], "files": []})
    return text, characters


def _resumable_story_plan(vpoc_store, owner: str, project_id: str) -> dict | None:
    """이 프로젝트에서 스토리로 시작해 아직 영상이 끝나지 않은 가장 최근 계획(읽기만 한다)."""
    prefixes = vpoc_store.STORE.list_prefixes(vpoc_store.plans_prefix(owner, project_id))
    for plan_id in sorted((item.rstrip("/").rsplit("/", 1)[-1] for item in prefixes), reverse=True):
        state, _ = vpoc_store.STORE.read_json(vpoc_store.state_key(owner, project_id, plan_id))
        if not state or (state.get("source") or {}).get("kind") != "story":
            continue
        if (state["steps"]["4"] or {}).get("status") == "done":
            return None  # 가장 최근 것이 끝났으면 새로 만든다.
        current = int(state.get("currentStep") or 1)
        return {"planId": plan_id, "currentStep": current}
    return None


@app.post("/api/projects/<project_id>/video-plan-from-story")
@require_auth
def video_plan_from_story(project_id: str):
    """스토리 프로젝트의 내용 · 첨부 이미지 · 캐릭터로 영상 만들기 계획을 만든다(① 입력이 채워진 상태).

    첨부 이미지는 영상 쪽 업로드로 **복사**만 한다 — 원본 키·분석·스토리 데이터는 바꾸지 않는다.
    이미지 분석에서 바이블 인물 이름이 보인 캐릭터 시트는 그 인물의 이미지로, 나머지는 장면 이미지로 넣는다.
    """
    if not VIDEO_GEN_ENABLED:
        return response_error("이 환경에서는 영상 기능이 꺼져 있습니다.", 404, "VIDEO_DISABLED")
    project = graph_store.get_project(project_id)
    if not project:
        return response_error("프로젝트를 찾을 수 없습니다.", 404, "PROJECT_NOT_FOUND")
    if not can_write_project(project):
        return response_error("이 프로젝트를 바꿀 권한이 없습니다.", 403, "PROJECT_FORBIDDEN")

    from .vpoc import images as vpoc_images  # noqa: PLC0415
    from .vpoc import store as vpoc_store  # noqa: PLC0415

    owner = str(project.get("owner") or "")
    body = request.get_json(silent=True) or {}
    if body.get("fresh") is not True:
        # 설계자 2026-10-13: 중간에 멈춘 계획이 있으면 새로 만들지 않고 그 계획(멈춘 단계)을 연다.
        # 매번 새 계획을 만들어 ① 부터 다시 해야 했다. 새로 만들려면 화면이 fresh=true 로 다시 부른다.
        resumable = _resumable_story_plan(vpoc_store, owner, project_id)
        if resumable:
            return jsonify({"ok": True, "resumed": True, **resumable})

    stored = graph_store.get_story_bible(project_id) or {}
    bible = stored.get("bible") if isinstance(stored.get("bible"), dict) else {}
    text, characters = _seed_video_from_story(project, bible)
    if not text.strip():
        return response_error(
            "스토리 내용이 없습니다. 스토리를 먼저 넣은 뒤 영상을 만들어 주세요.", 409, "STORY_EMPTY"
        )

    by_name = {item["name"]: index for index, item in enumerate(characters)}
    scene_files: list[dict] = []
    copied = skipped = 0
    assets = [a for a in (project.get("storyAssets") or [])[:10] if isinstance(a, dict)]
    # 캐릭터 시트를 먼저 — 인물의 첫 이미지가 영상 참고 이미지가 된다.
    assets.sort(key=lambda a: (a.get("analysis") or {}).get("image_kind") != "character_sheet")
    for asset in assets:
        key = str(asset.get("s3_key") or "")
        if not key:
            continue
        name = str(asset.get("name") or key.rsplit("/", 1)[-1])
        analysis = asset.get("analysis") or {}
        seen = [
            str(c.get("name_if_visible") or "").strip()
            for c in (analysis.get("characters") or [])
            if isinstance(c, dict)
        ]
        owner_index = next((by_name[n] for n in seen if n in by_name), None)
        is_sheet = analysis.get("image_kind") == "character_sheet"
        named = {by_name[n] for n in seen if n in by_name}
        # 설계자 2026-10-13: 웹툰 컷만 올린 경우 캐릭터 시트가 없어 ③ 에서 AI 가 인물을 다른 그림으로
        # 다시 그렸다. 바이블 인물 한 명만 보이는 컷은 그 인물에 이미지가 없을 때 인물 이미지로 쓴다.
        solo = len(named) == 1 and owner_index is not None and not characters[owner_index]["files"]
        try:
            raw = vpoc_store.STORE.get_bytes(key)
            kind = "character" if (owner_index is not None and (is_sheet or solo)) else "scene"
            meta = vpoc_images.save_upload(
                owner, project_id, raw, name=name, kind=kind,
                char_index=owner_index if kind == "character" else -1,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("story→video image copy failed project=%s key=%s err=%s", project_id, key, exc)
            skipped += 1
            continue
        copied += 1
        entry = {"fileId": meta["fileId"], "name": name}
        if kind == "character":
            characters[owner_index]["files"].append({**entry, "kind": "character"})
        else:
            scene_files.append({**entry, "kind": "scene"})

    plan_id = vpoc_store.new_id("pl")
    state = vpoc_store.new_state(owner, project_id, plan_id)
    state["steps"]["1"].update(
        {"status": "saved", "text": text, "characters": characters, "sceneFiles": scene_files}
    )
    state["source"] = {"kind": "story", "at": datetime.now(timezone.utc).isoformat()}
    vpoc_store.STORE.write_json(vpoc_store.state_key(owner, project_id, plan_id), state, create_only=True)
    logger.info(
        "story→video plan project=%s plan=%s chars=%d images=%d skipped=%d by=%s",
        project_id, plan_id, len(characters), copied, skipped, g.username,
    )
    return jsonify({
        "ok": True,
        "planId": plan_id,
        "characters": len(characters),
        "images": copied,
        "skippedImages": skipped,
    })


# --------------------------------------------------------------------------- #
# 결과물: 로그라인 / 콘티
# --------------------------------------------------------------------------- #
def story_prepare(project_id: str, output_kind: str, options: dict):
    """결과물 생성의 공통 준비. (pack, story_text, callers, error_response).

    이미지 분석 → 바이블까지가 모든 결과물의 공통 앞단이다(지시서 5절의 1~2단계).
    라우트마다 복사하면 한쪽만 고쳐져 바이블 재사용 조건이 갈린다.

    권한 확인만 여기서 하고 실제 준비는 `story_build_pack` 이 한다. 백그라운드 스레드도
    같은 준비를 해야 하는데, 스레드에는 요청 컨텍스트(g, response_error)가 없다.
    """
    project, error = story_owned_project(project_id)
    if error:
        return None, None, None, error
    try:
        pack, text, callers = story_build_pack(project, output_kind, options, g.username)
    except StoryPrepareError as failure:
        return None, None, None, response_error(failure.message, failure.status, failure.code)
    return pack, text, callers, None


class StoryPrepareError(Exception):
    """생성 준비 단계에서 사용자에게 알려야 하는 실패.

    Flask 응답 대신 예외로 올리는 이유: 같은 준비 코드를 요청과 백그라운드 스레드에서
    함께 쓴다. 스레드에서 `response_error` 를 만들면 요청 컨텍스트가 없어 터진다.
    """

    def __init__(self, message: str, status: int, code: str):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


def story_build_pack(
    project: dict, output_kind: str, options: dict, actor: str
) -> tuple[dict, str, dict]:
    """이미지 분석 → 바이블까지. 요청 컨텍스트에 의존하지 않는다.

    실패는 `StoryPrepareError` 로 올린다. 호출부가 응답으로 바꾸거나 잡 상태에 적는다.
    """
    project_id = project["id"]
    if not (project.get("storyType") or ""):
        raise StoryPrepareError("스토리 타입을 먼저 선택해 주세요.", 400, "STORY_TYPE_REQUIRED")

    text = story_text_of(project)
    if not text.strip():
        raise StoryPrepareError("실제 스토리를 먼저 입력해 주세요.", 400, "STORY_REQUIRED")

    pack = story_pack_for(project, output_kind, options)
    callers = story_callers(actor, options.get("quality") or "")

    images, image_error = story_images_for(project, actor)
    if image_error:
        raise StoryPrepareError(image_error, 400, "INVALID_IMAGE")

    pack = story_router.analyze_images(pack, images, callers, _analysis_store)
    persist_story_assets(project_id, project, pack.get("image_analyses") or [])

    # 안전 플래그가 붙은 이미지는 생성으로 넘기지 않는다.
    # 사전 스크리닝(Rekognition/Guardrails)이 이 계정에 없어서 비전 분석가가 본 것이
    # 유일한 신호다. minor/nudity 는 막는다.
    blocked = roles_vision.safety_blocked(pack.get("image_analyses") or [])
    if blocked:
        raise StoryPrepareError(
            "안전 검토가 필요한 이미지가 있어 생성을 중단했습니다: "
            + ", ".join(blocked[:5])
            + ". 해당 이미지를 제거하고 다시 시도해 주세요.",
            400, "IMAGE_SAFETY_BLOCKED",
        )

    existing = (graph_store.get_story_bible(project_id) or {}).get("bible")
    try:
        pack = story_router.ensure_bible(pack, text, callers, existing)
    except story_router.StoryError as error:
        raise StoryPrepareError(str(error), 502, "BIBLE_FAILED") from error

    reused = any(
        row.get("role") == story_models.ROLE_BIBLE and row.get("cache_hit")
        for row in (pack.get("trace") or [])
    )
    if not reused:
        try:
            graph_store.save_story_bible(
                project_id, pack["bible"], story_models.spec(story_models.ROLE_BIBLE)["model"]
            )
        except graph_store.GraphError as error:
            logger.warning("바이블 저장 실패 project=%s: %s", project_id, error)

    return pack, text, callers


def story_finish_output(
    output_id: str,
    owner: str,
    project_id: str,
    kind: str,
    result: dict,
    pack: dict,
    title: str,
) -> dict:
    """결과물을 S3 에 저장하고 정점을 완료 상태로 만든다."""
    key, char_count = store_story_json(
        owner, project_id, kind,
        {
            "kind": kind,
            "aiGenerated": True,
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "payload": result["output"],
            "validatorNotes": result["notes"],
            "status": result["status"],
        },
    )
    # 컨텍스트 팩도 함께 남긴다. 무엇을 근거로 만들었는지 재현할 수 있어야 한다.
    pack_key, _ = store_story_json(owner, project_id, f"{kind}-pack", pack)

    totals = story_context.trace_totals(pack)
    return graph_store.update_story_output(
        output_id,
        {
            "status": result["status"],
            "s3Key": key,
            "title": title[:200],
            "charCount": char_count,
            "tokensIn": totals["tokensIn"],
            "tokensOut": totals["tokensOut"],
            "validatorNotesJson": json.dumps(result["notes"], ensure_ascii=False),
            "traceJson": json.dumps(pack.get("trace") or [], ensure_ascii=False),
            "contextPackRef": pack_key,
        },
    )


@app.post("/api/projects/<project_id>/outputs/logline")
@require_auth
@require_quota(QUOTA_ASSET)
def story_output_logline(project_id: str):
    """로그라인 3안. 짧아서 요청 안에서 끝낸다(수 초)."""
    pack, text, callers, error = story_prepare(project_id, story_context.OUT_LOGLINE, {})
    if error:
        return error

    output = graph_store.create_story_output(
        project_id, g.username, story_context.OUT_LOGLINE,
        model=story_models.spec(story_models.ROLE_LOGLINE)["model"],
        # 어느 스토리로 만드는지를 시작 시점에 박는다. 실패한 결과물도 근거가 남는다.
        stamp=story_context.stamp(pack, text),
    )
    try:
        result = story_router.produce_with_review(
            story_context.OUT_LOGLINE, pack, text, callers
        )
    except story_router.StoryError as error:
        graph_store.update_story_output(
            output["id"], {"status": graph_store.STORY_FAILED, "error": str(error)[:400]}
        )
        return response_error(str(error), 502, "LOGLINE_FAILED")

    options = result["output"].get("options") or []
    title = (options[0].get("logline") if options else "로그라인")[:80]
    saved = story_finish_output(
        output["id"], g.username, project_id, story_context.OUT_LOGLINE, result, pack, title
    )
    spend_quota(g.username, QUOTA_ASSET, "story:logline", output["id"])

    return jsonify(
        {
            "ok": True,
            "item": saved,
            "payload": result["output"],
            "totals": story_context.trace_totals(pack),
        }
    ), 201


@app.post("/api/projects/<project_id>/outputs/storyboard")
@require_auth
@require_quota(QUOTA_ASSET)
def story_output_storyboard(project_id: str):
    """콘티. 컷 배열을 만들고 **기존 콘티 이미지 경로로 넘긴다.**

    지시서 6절(T6): "콘티 → 이미지 생성 연결". 이미지 생성을 새로 만들지 않는다.
    기존 storyboard 정점을 만들고 그 컷을 채워 두면 run_storyboard 가 쓰던
    이미지 생성 로직(그림체 판단 1회 + 컷마다 invoke_image)을 그대로 쓸 수 있다.

    그래서 이 라우트는 결과물 정점(story_output)과 콘티 정점(storyboard)을 둘 다 만든다.
      story_output : 검수 결과·trace·컨텍스트 팩 (이 파이프라인의 기록)
      storyboard   : 컷과 그림 (기존 화면과 이미지 생성이 쓰는 구조)
    """
    payload = request.get_json(silent=True) or {}
    try:
        cut_count = int(payload.get("cutCount") or 6)
    except (TypeError, ValueError):
        return response_error("컷 수가 올바르지 않습니다.", 400, "INVALID_CUT_COUNT")
    if cut_count not in STORYBOARD_CUT_CHOICES:
        allowed = ", ".join(str(c) for c in STORYBOARD_CUT_CHOICES)
        return response_error(f"컷 수는 {allowed} 중 하나여야 합니다.", 400, "INVALID_CUT_COUNT")

    options = {"cut_count": cut_count}
    pack, text, callers, error = story_prepare(project_id, story_context.OUT_STORYBOARD, options)
    if error:
        return error

    # 동시에 여러 개를 돌리면 컷 수만큼 이미지 비용이 배로 붙는다. 기존 규칙을 따른다.
    running = [
        board
        for board in graph_store.list_storyboards(g.username, limit=5)
        if reap_stale_storyboard(board)["status"] in graph_store.SB_ACTIVE_STATUSES
    ]
    if running:
        return response_error(
            "이미 만들고 있는 콘티가 있습니다. 완료된 뒤에 다시 시도해 주세요.", 409, "ALREADY_RUNNING"
        )

    output = graph_store.create_story_output(
        project_id, g.username, story_context.OUT_STORYBOARD,
        model=story_models.spec(story_models.ROLE_STORYBOARD)["model"],
        options=options,
        stamp=story_context.stamp(pack, text),
    )
    try:
        result = story_router.produce_with_review(
            story_context.OUT_STORYBOARD, pack, text, callers
        )
    except story_router.StoryError as error:
        graph_store.update_story_output(
            output["id"], {"status": graph_store.STORY_FAILED, "error": str(error)[:400]}
        )
        return response_error(str(error), 502, "STORYBOARD_FAILED")

    plan = result["output"]
    title = plan.get("title") or (pack.get("bible") or {}).get("title") or "콘티"
    saved = story_finish_output(
        output["id"], g.username, project_id, story_context.OUT_STORYBOARD, result, pack, title
    )

    # --- 기존 콘티 경로로 넘긴다 ---
    bible = pack.get("bible") or {}
    board = graph_store.create_storyboard(
        g.username,
        title=title,
        # 요약 스토리를 prompt 로 둔다. run_storyboard 가 이것을 컷 기획의 이야기로 쓰는데,
        # 우리는 컷을 이미 만들었으므로 manual 모드로 두어 다시 나누지 않게 한다.
        prompt=(pack.get("summary_story") or bible.get("logline") or title)[:8000],
        style_prompt=str(payload.get("stylePrompt") or bible.get("visual_style") or "")[:1000],
        detail=graph_store.SB_DETAIL_NORMAL,
        aspect_ratio=str(payload.get("aspectRatio") or "16:9"),
        cut_count=len(plan.get("cuts") or []),
        project_id=project_id,
        # **manual 이 중요하다.** auto 로 두면 run_storyboard 가 documents.plan_storyboard 를
        # 불러 컷을 새로 나눈다. 우리가 만든 컷이 버려지고 모델 호출이 한 번 더 붙는다.
        mode=graph_store.SB_MODE_MANUAL,
    )
    board = graph_store.update_storyboard(
        board["id"],
        {
            "cutsJson": json.dumps(plan.get("cuts") or [], ensure_ascii=False),
            # 컷마다 공유하는 영어 블록. documents.storyboard_scene_prompt 가 이 값을 쓴다.
            "sharedJson": json.dumps(
                {"characters": plan.get("characters", ""), "setting": plan.get("setting", "")},
                ensure_ascii=False,
            ),
        },
    )

    # **결과물 → 콘티 링크를 남긴다.**
    #
    # 컷 그림은 storyboard 정점에만 있다. 이 링크가 없으면 결과물 화면은 S3 의 텍스트
    # 계획만 읽고, 그림을 어디서 찾아야 하는지 알 방법이 없다. 실제로 그래서 "콘티 내용"
    # 모달에 컷 설명 글만 떴다. 링크를 응답에만 담고 저장하지 않은 것이 원인이었다 —
    # 사용자가 나중에 목록에서 다시 열면 그 응답은 이미 사라져 있다.
    try:
        saved = graph_store.update_story_output(output["id"], {"storyboardId": board["id"]})
    except (graph_store.GraphError, graph_store.NotFoundError) as error:
        # 링크 저장 실패가 생성을 되돌릴 이유는 아니다. 그림은 콘티 화면에서 볼 수 있다.
        logger.warning("결과물-콘티 링크 저장 실패 output=%s board=%s: %s", output["id"], board["id"], error)

    thread = threading.Thread(
        target=run_storyboard,
        args=(board["id"], g.username, str(payload.get("presetId") or "").strip(),
              str(payload.get("referenceKey") or "").strip()),
        name=f"story-sb-{board['id'][:8]}",
        daemon=True,
    )
    thread.start()

    # 콘티 1장이 결과물 1건이다. 컷이 몇 개든 1건이다(기존 규칙과 같다).
    spend_quota(g.username, QUOTA_ASSET, "story:storyboard", output["id"])
    logger.info(
        "Story storyboard project=%s output=%s board=%s cuts=%s status=%s",
        project_id, output["id"], board["id"], len(plan.get("cuts") or []), result["status"],
    )
    return jsonify(
        {
            "ok": True,
            "item": saved,
            "storyboardId": board["id"],
            "payload": plan,
            "totals": story_context.trace_totals(pack),
        }
    ), 202


# --------------------------------------------------------------------------- #
# 결과물 조회 / 삭제
# --------------------------------------------------------------------------- #
def with_story_drift(item: dict, project: dict) -> dict:
    """결과물(또는 대본 잡)에 "생성 뒤 스토리가 바뀌었는가"를 붙인다.

    프로젝트는 고유한 스토리 하나를 갖고 결과물은 그 스토리로 만든 것만 보관한다.
    그런데 스토리를 고치는 것을 막을 수는 없다 — 막으면 한 번 생성한 프로젝트는
    영구히 잠긴다. 그래서 막지 않고 **어긋났다는 사실을 보이게** 한다.

    S3 를 읽지 않는다(story_context.stamp_drift 참고). 목록 조회에서 결과물마다
    S3 왕복이 붙으면 프로젝트를 여는 것만으로 수십 번 요청이 나간다.
    """
    drift = story_context.stamp_drift(item.get("story") or {}, project)
    story = dict(item.get("story") or {})
    story["drift"] = drift
    story["stale"] = bool(drift)
    return {**item, "story": story}


@app.get("/api/projects/<project_id>/outputs")
@require_auth
def story_outputs_list(project_id: str):
    project = graph_store.get_project(project_id)
    if not project:
        return response_error("프로젝트를 찾을 수 없습니다.", 404, "PROJECT_NOT_FOUND")
    if not can_read_project(project):
        return response_error("이 프로젝트에 접근할 권한이 없습니다.", 403, "PROJECT_FORBIDDEN")

    kind = str(request.args.get("kind") or "").strip()
    if kind and kind not in story_context.OUTPUT_KINDS:
        return response_error("결과물 종류가 올바르지 않습니다.", 400, "INVALID_KIND")
    items = [
        with_story_drift(item, project)
        for item in graph_store.list_story_outputs(project_id, kind=kind)
    ]
    return jsonify({"ok": True, "items": items})


def storyboard_for_output(output: dict) -> dict | None:
    """콘티 결과물에 딸린 컷 그림. 다른 종류의 결과물이면 None.

    결과물(story_output)에는 텍스트 계획만 S3 에 저장되고, 컷 그림은 storyboard
    정점에 있다. 화면이 그림을 그리려면 그 둘을 합쳐 줘야 한다.

    `storyboardId` 가 비어 있는 옛 결과물도 다룬다. 링크를 저장하기 전에 만든 것들이라
    값이 없다. 그 경우 같은 프로젝트의 콘티 중 **결과물 생성 직후에 만들어진 것**을
    고른다(콘티 정점은 항상 결과물 다음에 생긴다). 추측이므로 못 찾으면 조용히 None 이다 —
    엉뚱한 콘티의 그림을 붙이는 것보다 글만 보여 주는 편이 낫다.
    """
    if output.get("kind") != story_context.OUT_STORYBOARD:
        return None

    board = None
    if output.get("storyboardId"):
        board = graph_store.get_storyboard(output["storyboardId"])
    if not board:
        board = _guess_storyboard_for_output(output)
    if not board:
        return None
    if board.get("projectId") and board["projectId"] != output["projectId"]:
        return None

    return storyboard_payload(reap_stale_storyboard(board))


def _guess_storyboard_for_output(output: dict) -> dict | None:
    """링크가 없는 옛 결과물의 콘티를 시각으로 추정한다."""
    created = str(output.get("createdAt") or "")
    if not created:
        return None
    try:
        candidates = graph_store.list_storyboards(
            output["owner"], project_id=output["projectId"], limit=20
        )
    except graph_store.GraphError as error:
        logger.warning("콘티 목록 조회 실패 project=%s: %s", output["projectId"], error)
        return None

    # 결과물보다 나중에 만들어진 것 중 가장 이른 것. 두 정점이 몇 초 차이로 생긴다.
    after = sorted(
        (board for board in candidates if str(board.get("createdAt") or "") >= created),
        key=lambda board: str(board.get("createdAt") or ""),
    )
    return after[0] if after else None


@app.route("/api/outputs/<output_id>", methods=["GET", "DELETE"])
@require_auth
def story_output_detail(output_id: str):
    output = graph_store.get_story_output(output_id)
    if not output:
        return response_error("결과물을 찾을 수 없습니다.", 404, "OUTPUT_NOT_FOUND")
    project = graph_store.get_project(output["projectId"])
    if not project or not can_read_project(project):
        return response_error("이 결과물에 접근할 권한이 없습니다.", 403, "OUTPUT_FORBIDDEN")

    if request.method == "GET":
        payload = None
        if output.get("s3Key"):
            try:
                payload = json.loads(read_source_text(output["s3Key"]))
            except (BotoCoreError, ClientError, ValueError, json.JSONDecodeError):
                logger.warning("결과물 본문을 읽지 못했습니다 key=%s", output["s3Key"])
        return jsonify(
            {
                "ok": True,
                "item": with_story_drift(output, project),
                "payload": payload,
                # 콘티는 그림이 별도 정점에 있다. 함께 내려보내지 않으면 화면이 글만 그린다.
                "storyboard": storyboard_for_output(output),
            }
        )

    if output["owner"] != g.username and g.role != ROLE_ADMIN:
        return response_error("본인 결과물만 삭제할 수 있습니다.", 403, "OUTPUT_FORBIDDEN")
    try:
        keys = graph_store.delete_story_output(output_id)
    except graph_store.NotFoundError as error:
        return response_error(str(error), 404, "OUTPUT_NOT_FOUND")
    remove_s3_objects(keys)
    return jsonify({"ok": True, "deleted": output_id})


# 숏 크기 한글 라벨. 화면의 SB_SHOT_LABELS 와 같아야 한다 — 같은 콘티가 화면과 종이에서
# 다른 말을 쓰면 두 문서를 대조할 수 없다.
SHOT_LABELS = {
    "wide": "와이드",
    "medium": "미디엄",
    "close-up": "클로즈업",
    "close up": "클로즈업",
    "closeup": "클로즈업",
    "over-the-shoulder": "오버더숄더",
    "over the shoulder": "오버더숄더",
}


def storyboard_pdf_cuts(output: dict, body: dict) -> list[dict]:
    """PDF에 넣을 컷 목록. 텍스트 계획과 컷 그림을 합친다.

    콘티는 레코드가 둘로 갈린다. 컷 설명·대사·초는 결과물 본문(S3)에, 그림은 storyboard
    정점에 있다. 화면(buildStoryDocument)이 같은 합치기를 하고 여기서도 같은 규칙을 쓴다.

    **번호로 맞춘다.** 배열 순서로 맞추면 컷을 추가·삭제한 콘티에서 그림이 한 칸씩
    밀린다. 화면도 같은 이유로 번호를 쓴다.
    """
    board = storyboard_for_output(output) or {}
    shots = {
        int(cut.get("number") or index + 1): cut
        for index, cut in enumerate(board.get("cuts") or [])
    }

    def shot_label(value) -> str:
        raw = str(value or "").strip()
        return SHOT_LABELS.get(raw.lower(), raw)

    cuts = []
    for index, cut in enumerate(body.get("cuts") or [], start=1):
        if not isinstance(cut, dict):
            continue
        number = int(cut.get("number") or index)
        shot = shots.get(number) or {}
        cuts.append(
            {
                "number": number,
                "shot": cut.get("shot") or "",
                "shotLabel": shot_label(cut.get("shot")),
                "context": cut.get("context") or "",
                "dialogue": cut.get("dialogue") or "",
                "audio": cut.get("audio") or "",
                "note": cut.get("note") or "",
                "seconds": cut.get("seconds") or 0,
                # 그림과 그 상태는 콘티 정점이 단일 원본이다. 결과물 본문의 imageKey 는
                # 생성 시점에 빈 문자열로 저장되므로 쓰지 않는다.
                "imageKey": shot.get("imageKey") or "",
                "status": shot.get("status") or "pending",
                "error": shot.get("error") or "",
            }
        )

    # 결과물 본문에 컷이 없으면(옛 데이터) 콘티 정점의 컷을 그대로 쓴다.
    if not cuts:
        cuts = [
            {**cut, "shotLabel": shot_label(cut.get("shot"))}
            for cut in (board.get("cuts") or [])
        ]
    return cuts


@app.get("/api/outputs/<output_id>/pdf")
@require_auth
def story_output_pdf(output_id: str):
    """결과물을 PDF 파일로 내려준다. 콘티는 기획표, 로그라인은 문서.

    콘티가 이 라우트의 주된 목적이다. 브라우저 인쇄로는 사용자가 파일을 못 받고,
    받아도 문서에 접속 URL과 시각이 찍힌다(pdf_export 모듈 주석 참고).
    """
    output = graph_store.get_story_output(output_id)
    if not output:
        return response_error("결과물을 찾을 수 없습니다.", 404, "OUTPUT_NOT_FOUND")
    project = graph_store.get_project(output["projectId"])
    if not project or not can_read_project(project):
        return response_error("이 결과물에 접근할 권한이 없습니다.", 403, "OUTPUT_FORBIDDEN")
    if not output.get("s3Key"):
        return response_error("아직 만들어지지 않은 결과물입니다.", 409, "OUTPUT_NOT_READY")

    try:
        stored = json.loads(read_source_text(output["s3Key"]))
    except (BotoCoreError, ClientError, ValueError, json.JSONDecodeError):
        logger.warning("결과물 본문을 읽지 못했습니다 key=%s", output["s3Key"])
        return response_error("결과물 본문을 읽지 못했습니다.", 502, "OUTPUT_UNREADABLE")

    # 저장 형태는 {kind, aiGenerated, generatedAt, payload: 본문, ...} 이다.
    body = stored.get("payload") if isinstance(stored, dict) else None
    body = body if isinstance(body, dict) else {}

    kind = output.get("kind")
    label = {
        story_context.OUT_STORYBOARD: "콘티",
        story_context.OUT_LOGLINE: "로그라인",
    }.get(kind, "결과물")
    title = output.get("title") or body.get("title") or label
    meta = " · ".join(part for part in (label, output.get("model") or "") if part)

    try:
        if kind == story_context.OUT_STORYBOARD:
            board = storyboard_for_output(output) or {}
            sheet_meta = " · ".join(
                part for part in (
                    f"그림체 {board.get('presetLabel')}" if board.get("presetLabel") else "",
                    f"모델 {board.get('modelLabel')}" if board.get("modelLabel") else "",
                    f"인물 {body.get('characters')}" if body.get("characters") else "",
                    f"배경 {body.get('setting')}" if body.get("setting") else "",
                ) if part
            )
            data = pdf_export.storyboard_pdf(
                title,
                storyboard_pdf_cuts(output, body),
                meta=meta,
                sheet_meta=sheet_meta,
                image_loader=read_source_bytes,
            )
        elif kind == story_context.OUT_LOGLINE:
            data = pdf_export.logline_pdf(title, body, meta=meta)
        else:
            # 대본 결과물은 잡 라우트로 받는다. 여기서 조용히 빈 파일을 주지 않는다.
            return response_error(
                "이 결과물은 PDF로 만들 수 없습니다.", 400, "PDF_UNSUPPORTED_KIND"
            )
    except Exception as error:  # noqa: BLE001 - 문서 조립 실패를 500으로 흘리지 않는다
        logger.exception("결과물 PDF 생성 실패 id=%s kind=%s", output_id, kind)
        return response_error(f"PDF를 만들지 못했습니다: {error}", 502, "PDF_FAILED")

    logger.info("Output PDF id=%s kind=%s bytes=%s", output_id, kind, len(data))
    return pdf_response(data, pdf_filename(project.get("title"), label))


# --------------------------------------------------------------------------- #
# 대본 잡
# --------------------------------------------------------------------------- #
@app.get("/api/projects/<project_id>/outputs/script/options")
@require_auth
def story_script_options(project_id: str):
    """대본 모달이 쓰는 선택지와 **비용 예상**.

    지시서 9절 수용 기준: "대본 모달에 비용 예상 표시, 아웃라인 승인 전에는 회차 생성이
    시작되지 않음". 예상은 요청 파라미터에 따라 달라지므로 쿼리로 받는다.
    """
    project = graph_store.get_project(project_id)
    if not project:
        return response_error("프로젝트를 찾을 수 없습니다.", 404, "PROJECT_NOT_FOUND")
    if not can_read_project(project):
        return response_error("이 프로젝트에 접근할 권한이 없습니다.", 403, "PROJECT_FORBIDDEN")

    try:
        episode_from = int(request.args.get("from") or 1)
        episode_to = int(request.args.get("to") or 3)
    except ValueError:
        return response_error("회차 범위는 숫자여야 합니다.", 400, "INVALID_RANGE")
    length = str(request.args.get("length") or story_context.LENGTH_NORMAL)
    quality = str(request.args.get("quality") or story_models.QUALITY_STANDARD)
    if length not in story_context.LENGTHS:
        length = story_context.LENGTH_NORMAL
    if quality not in story_models.QUALITY_CHOICES:
        quality = story_models.QUALITY_STANDARD

    count = max(1, min(story_context.MAX_EPISODES_PER_JOB, episode_to - episode_from + 1))
    return jsonify(
        {
            "ok": True,
            "maxEpisodesPerJob": story_context.MAX_EPISODES_PER_JOB,
            "lengths": [
                {"id": value, "label": story_context.LENGTH_LABELS[value]}
                for value in story_context.LENGTHS
            ],
            "formats": [{"id": "drama", "label": "드라마 대본", "isDefault": True}],
            "qualities": [
                {
                    "id": story_models.QUALITY_STANDARD,
                    "label": "기본",
                    "model": story_models.spec(
                        story_models.ROLE_SCRIPT_EPISODE, quality=story_models.QUALITY_STANDARD
                    )["model"],
                },
                {
                    "id": story_models.QUALITY_HIGH,
                    "label": "고품질",
                    "model": story_models.spec(
                        story_models.ROLE_SCRIPT_EPISODE, quality=story_models.QUALITY_HIGH
                    )["model"],
                },
            ],
            "estimate": story_router.estimate_script(count, length, quality),
        }
    )


def reap_stale_script_job(job: dict) -> dict:
    """진척 없이 방치된 대본 잡을 실패로 정리한다.

    파드가 롤아웃되면 생성 스레드가 사라지지만 그래프에는 writing 이 남는다.
    이 장치가 없으면 사용자가 영원히 진행률을 보게 된다(롱테이크에 그 문제가 있었다).
    """
    if job.get("status") not in graph_store.SCRIPT_ACTIVE_STATUSES:
        return job
    updated_at = job.get("updatedAt")
    if not updated_at:
        return job
    try:
        started = datetime.fromisoformat(str(updated_at))
    except ValueError:
        return job
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) - started < timedelta(minutes=SCRIPT_STALE_MINUTES):
        return job
    try:
        return graph_store.update_script_job(
            job["id"],
            {
                "status": graph_store.SCRIPT_FAILED,
                "error": f"생성이 {SCRIPT_STALE_MINUTES}분 안에 진척되지 않았습니다. 다시 시도해 주세요.",
            },
        )
    except (graph_store.GraphError, graph_store.NotFoundError):
        return job


@app.post("/api/projects/<project_id>/outputs/script")
@require_auth
@require_quota(QUOTA_ASSET)
def story_script_start(project_id: str):
    """대본 잡을 만들고 **아웃라인만** 생성한다(202).

    회차 생성은 시작하지 않는다. 지시서 3절이 아웃라인 승인을 요구하고, 9절이
    "아웃라인 승인 전에는 회차 생성이 시작되지 않음"을 수용 기준으로 정했다.
    승인은 별도 라우트(/approve)다.
    """
    payload = request.get_json(silent=True) or {}
    try:
        episode_from = int(payload.get("episodeFrom") or 1)
        episode_to = int(payload.get("episodeTo") or 1)
    except (TypeError, ValueError):
        return response_error("회차 범위는 숫자여야 합니다.", 400, "INVALID_RANGE")
    if episode_from < 1 or episode_to < episode_from:
        return response_error(
            "회차 범위가 올바르지 않습니다. 시작화가 끝화보다 작아야 합니다.", 400, "INVALID_RANGE"
        )
    count = episode_to - episode_from + 1
    if count > story_context.MAX_EPISODES_PER_JOB:
        return response_error(
            f"한 번에 최대 {story_context.MAX_EPISODES_PER_JOB}화까지 만들 수 있습니다.",
            400, "TOO_MANY_EPISODES",
        )

    length = str(payload.get("length") or story_context.LENGTH_NORMAL)
    if length not in story_context.LENGTHS:
        return response_error("회차 분량이 올바르지 않습니다.", 400, "INVALID_LENGTH")
    quality = str(payload.get("quality") or story_models.QUALITY_STANDARD)
    if quality not in story_models.QUALITY_CHOICES:
        return response_error("품질 옵션이 올바르지 않습니다.", 400, "INVALID_QUALITY")

    options = {
        "episode_range": [episode_from, episode_to],
        "length": length,
        "quality": quality,
        "format": str(payload.get("format") or "drama"),
    }
    # 이어쓰기 판단의 근거. 화면이 원문이 몇 화까지인지 알려 준다(story_router._is_continuation).
    if payload.get("sourceEpisodeTo") is not None:
        try:
            options["source_episode_to"] = int(payload["sourceEpisodeTo"])
        except (TypeError, ValueError):
            pass

    # 동시 실행 1개. 회차당 1콜이라 두 잡이 겹치면 비용이 두 배가 되고 순서를 알 수 없다.
    running = [
        job for job in graph_store.list_script_jobs(g.username, limit=5)
        if reap_stale_script_job(job)["status"] in graph_store.SCRIPT_ACTIVE_STATUSES
    ]
    if running:
        return response_error(
            "이미 만들고 있는 대본이 있습니다. 완료된 뒤에 다시 시도해 주세요.", 409, "ALREADY_RUNNING"
        )

    project, error = story_owned_project(project_id)
    if error:
        return error

    job = graph_store.create_script_job(
        project_id, g.username,
        episode_from=episode_from, episode_to=episode_to,
        length=length, fmt=options["format"], quality=quality,
    )

    # 아웃라인 생성을 요청 안에서 하지 않는다.
    #
    # 예전에는 이미지 분석 + 바이블 + 아웃라인을 이 요청 안에서 전부 처리했다. 세 단계
    # 합쳐 1~5분이고 ALB idle timeout 은 60초다. 브라우저는 504 를 받고 사용자는 실패로
    # 보는데 서버는 계속 돌고 있었다. 더 나쁜 것은 여기서 StoryError 가 아닌 예외(읽기
    # 타임아웃 등)가 나면 잡이 planning 에 그대로 굳었고, 25분 뒤 정리 장치가
    # "생성이 25분 안에 진척되지 않았습니다" 로 실패 처리했다. 그 25분 동안 새 요청은
    # 전부 409 였다. 사용자가 본 증상이 정확히 이것이다.
    thread = threading.Thread(
        target=run_script_outline,
        args=(job["id"], project_id, g.username, options),
        name=f"outline-{job['id'][:8]}",
        daemon=True,
    )
    thread.start()

    logger.info(
        "Script outline started job=%s project=%s %s~%s화",
        job["id"], project_id, episode_from, episode_to,
    )
    return jsonify(
        {
            "ok": True,
            "item": job,
            # 아웃라인은 아직 없다. 화면은 폴링으로 awaiting 을 기다린다.
            "outline": None,
            "estimate": story_router.estimate_script(count, length, quality),
            "note": "아웃라인을 만들고 있습니다. 완성되면 확인 후 승인할 수 있습니다.",
        }
    ), 202


def run_script_outline(job_id: str, project_id: str, owner: str, options: dict) -> None:
    """이미지 분석 → 바이블 → 아웃라인. 백그라운드 스레드에서 돈다.

    끝나면 잡을 `awaiting` 으로 올려 승인 대기 상태로 만든다. 승인 전에는 회차 생성을
    시작하지 않는다는 규칙은 그대로다.

    **어떤 예외가 나도 잡을 활성 상태로 남기지 않는다.** 활성 상태로 굳으면 사용자는
    25분 동안 아무것도 못 하고(동시 실행 1개 제한), 그 뒤에 받는 메시지는 실패 원인과
    아무 관계가 없는 "진척되지 않았습니다" 다.
    """
    try:
        project = graph_store.get_project(project_id)
        if not project:
            graph_store.update_script_job(
                job_id, {"status": graph_store.SCRIPT_FAILED, "error": "프로젝트가 삭제되었습니다."}
            )
            return

        with script_job_heartbeat(job_id):
            pack, text, callers = story_build_pack(
                project, story_context.OUT_SCRIPT, options, owner
            )
            outline = story_router.plan_script(pack, text, callers)

            outline_key, _ = store_story_json(owner, project_id, "script-outline", outline)
            pack_key, _ = store_story_json(owner, project_id, "script-pack", pack)

        episodes = [
            {
                "no": item["no"],
                "status": "pending",
                "title": item.get("title", ""),
                "summary": "",
                "endingHook": item.get("ending_hook", ""),
                "s3Key": "",
                "charCount": 0,
                "validatorNotes": [],
            }
            for item in outline["episodes"]
        ]
        graph_store.update_script_job(
            job_id,
            {
                # 승인 대기. 여기서 멈추는 것이 이 단계의 핵심이다.
                "status": graph_store.SCRIPT_AWAITING,
                "outlineKey": outline_key,
                "arc": str(outline.get("arc") or "")[:2000],
                "episodesJson": json.dumps(episodes, ensure_ascii=False),
                "contextPackRef": pack_key,
                "traceJson": json.dumps(pack.get("trace") or [], ensure_ascii=False),
                "error": "",
                # 어느 스토리로 만드는 대본인지. 결과물은 생성 시점에 박지만 대본 잡은
                # 여기가 가장 이른 시점이다 — 팩(=바이블)이 이 스레드에서 만들어진다.
                **story_context.stamp(pack, text),
            },
        )
        logger.info(
            "Script outline ready job=%s project=%s continuation=%s",
            job_id, project_id, outline.get("continuation"),
        )

    except StoryPrepareError as failure:
        fail_script_job(job_id, failure.message)
    except story_router.StoryError as error:
        fail_script_job(job_id, f"아웃라인을 만들지 못했습니다: {error}")
    except Exception as error:  # noqa: BLE001 - 스레드에서 예외가 새면 상태가 굳는다
        logger.exception("Script outline crashed job=%s", job_id)
        fail_script_job(job_id, f"아웃라인 생성 중 예상치 못한 오류: {error}")


def start_script_heartbeat(job_id: str):
    """작업이 도는 동안 `updatedAt` 을 주기적으로 찍는다. 멈추는 함수를 돌려준다.

    ## 왜 단계마다 찍는 것으로는 부족한가

    `reap_stale_script_job` 이 답해야 하는 질문은 "이 작업을 만드는 스레드가 아직
    살아 있는가" 다. 그런데 판단 근거는 `updatedAt` 이고, 그 값은 **단계가 끝날 때만**
    갱신됐다. 그래서 두 가지가 동시에 틀렸다.

      - 회차 하나가 스테일 기준보다 오래 걸리면 살아 있는 작업을 죽은 것으로 판정한다.
        읽기 타임아웃 상한이 회차당 600초이고 검수 실패 시 재생성까지 하면 한 회차가
        20분을 넘을 수 있다. 기준은 25분이다.
      - 반대로 단계 중간에 스레드가 죽으면 다음 판정까지 그만큼 늦게 알아차린다.

    주기적인 하트비트는 그 질문에 정확히 답한다. 스레드가 사라지면 하트비트도 함께
    사라지므로, 스테일 기준은 "작업이 이만큼 조용하면 프로세스가 없어진 것"이라는
    원래 의미를 되찾는다.

    실패는 삼킨다. 하트비트가 생성을 멈추게 하면 안 된다.
    """
    stop = threading.Event()

    def beat() -> None:
        while not stop.wait(SCRIPT_HEARTBEAT_SECONDS):
            try:
                graph_store.touch_script_job(job_id)
            except Exception as error:  # noqa: BLE001 - 하트비트가 생성을 깨뜨리면 안 된다
                logger.warning("대본 하트비트 실패 job=%s: %s", job_id, error)

    worker = threading.Thread(target=beat, name=f"beat-{job_id[:8]}", daemon=True)
    worker.start()
    return stop.set


@contextlib.contextmanager
def script_job_heartbeat(job_id: str):
    """`start_script_heartbeat` 의 with 문 버전."""
    stop = start_script_heartbeat(job_id)
    try:
        yield
    finally:
        stop()


def fail_script_job(job_id: str, message: str) -> None:
    """잡을 실패로 확정한다. 이 호출이 실패해도 예외를 밖으로 내지 않는다.

    상태 기록에 실패하면 잡이 활성 상태로 남고, 그것이 곧 25분 대기와 409 로 이어진다.
    적어도 로그에는 남겨야 원인을 되짚을 수 있다.
    """
    try:
        graph_store.update_script_job(
            job_id, {"status": graph_store.SCRIPT_FAILED, "error": str(message)[:400]}
        )
    except (graph_store.GraphError, graph_store.NotFoundError) as error:
        logger.error("대본 잡 실패 기록에 실패했습니다 job=%s: %s", job_id, error)


def run_script_job(job_id: str, owner: str) -> None:
    """회차를 순서대로 만든다. 백그라운드 스레드에서 돈다.

    ## 왜 순차인가

    회차 N 은 회차 N-1 의 요약을 입력으로 받는다(연속성). 병렬로 만들면 그 사슬이
    끊어져 인물 상태가 회차마다 어긋난다. 지시서 5절이 정한 구조다.

    ## 회차 하나가 실패하면

    그 회차만 failed 로 남기고 다음으로 넘어간다. 전체를 멈추면 이미 만든 회차도
    쓸 수 없게 된다. 실패한 회차는 개별 재생성 라우트로 다시 만든다(지시서 7절).

    실패한 회차의 요약이 없으므로 다음 회차는 **그 앞 회차의 요약**을 이어 쓴다.
    빈 요약을 넘기면 다음 회차가 연속성 근거를 잃는다.

    ## 왜 하트비트가 필요한가

    회차마다 진행률을 저장하므로 `updatedAt` 은 회차 단위로 갱신된다. 그런데 회차 하나가
    스테일 기준(25분)보다 오래 걸릴 수 있다 — 읽기 타임아웃 상한이 회차당 600초이고
    검수 실패 시 재생성이 붙는다. 그러면 살아 있는 작업이 실패로 정리된다.
    """
    stop_heartbeat = start_script_heartbeat(job_id)
    try:
        job = graph_store.get_script_job(job_id)
        if not job:
            return
        project = graph_store.get_project(job["projectId"])
        if not project:
            graph_store.update_script_job(
                job_id, {"status": graph_store.SCRIPT_FAILED, "error": "프로젝트가 삭제되었습니다."}
            )
            return

        outline = json.loads(read_source_text(job["outlineKey"])) if job.get("outlineKey") else {}
        pack = json.loads(read_source_text(job["contextPackRef"])) if job.get("contextPackRef") else {}
        if not outline.get("episodes"):
            graph_store.update_script_job(
                job_id, {"status": graph_store.SCRIPT_FAILED, "error": "아웃라인을 읽지 못했습니다."}
            )
            return

        text = story_text_of(project)
        callers = story_callers(owner, job.get("quality") or "")
        by_no = {int(item["no"]): item for item in outline["episodes"]}
        episodes = list(job.get("episodes") or [])
        arc = job.get("arc") or ""
        previous_summary = ""

        for index, entry in enumerate(episodes):
            no = int(entry.get("no") or 0)
            # 이미 만든 회차는 건너뛴다. 재시작해도 처음부터 다시 만들지 않는다.
            if entry.get("status") == "done":
                previous_summary = entry.get("summary") or previous_summary
                continue

            outline_episode = by_no.get(no)
            if not outline_episode:
                entry.update({"status": "failed", "error": "아웃라인에 이 회차가 없습니다."})
                continue

            graph_store.touch_script_job(job_id)
            try:
                result = story_router.write_episode(
                    pack, text, callers, outline_episode,
                    arc=arc, previous_summary=previous_summary,
                )
            # StoryError 만 잡으면 안 된다. 읽기 타임아웃(botocore ReadTimeoutError)과
            # Bedrock 거부(ClientError)는 StoryError 가 아니라서 이 핸들러를 지나
            # 함수 맨 아래 except Exception 으로 떨어졌고, **잡 전체가 실패로 끝나
            # 남은 회차가 통째로 버려졌다.** 한 회차의 인프라 장애는 그 회차의 문제다.
            # 나머지는 계속 만들고, 실패한 회차만 개별 재생성하면 된다.
            except (
                story_router.StoryError,
                ClientError,
                BotoCoreError,
                ValueError,
            ) as error:
                reason = f"{type(error).__name__}: {error}" if not isinstance(
                    error, story_router.StoryError
                ) else str(error)
                logger.warning("회차 생성 실패 job=%s ep=%s: %s", job_id, no, reason)
                entry.update({"status": "failed", "error": reason[:300]})
                _save_script_progress(job_id, episodes, pack)
                continue

            episode = result["episode"]
            # 저장 실패도 그 회차의 문제로 가둔다. 여기서 예외가 새면 이미 만들어 둔
            # 앞 회차들까지 실패로 끝난다(본문은 S3에 있는데 잡은 실패로 남는다).
            try:
                key, char_count = store_story_json(
                    owner, job["projectId"], f"script-ep{no:02d}",
                    {
                        "kind": "script_episode",
                        "aiGenerated": True,
                        "generatedAt": datetime.now(timezone.utc).isoformat(),
                        "episodeNo": no,
                        "payload": episode,
                        "validatorNotes": result["notes"],
                        "status": result["status"],
                    },
                )
            except (ClientError, BotoCoreError) as error:
                logger.warning("회차 저장 실패 job=%s ep=%s: %s", job_id, no, error)
                entry.update({"status": "failed", "error": f"회차 본문을 저장하지 못했습니다: {error}"[:300]})
                _save_script_progress(job_id, episodes, pack)
                continue
            entry.update(
                {
                    "status": "done",
                    "title": episode.get("title") or outline_episode.get("title") or "",
                    "summary": episode.get("summary") or "",
                    "endingHook": episode.get("ending_hook") or "",
                    "s3Key": key,
                    "charCount": char_count,
                    "validatorNotes": result["notes"],
                    "reviewStatus": result["status"],
                    "error": "",
                }
            )
            previous_summary = entry["summary"] or previous_summary
            _save_script_progress(job_id, episodes, pack)
            logger.info(
                "Script episode done job=%s ep=%s chars=%s status=%s",
                job_id, no, char_count, result["status"],
            )

        done = sum(1 for item in episodes if item.get("status") == "done")
        failed = sum(1 for item in episodes if item.get("status") == "failed")
        if done == 0:
            graph_store.update_script_job(
                job_id,
                {"status": graph_store.SCRIPT_FAILED, "error": "모든 회차 생성이 실패했습니다.",
                 "episodesJson": json.dumps(episodes, ensure_ascii=False)},
            )
            return

        graph_store.update_script_job(
            job_id,
            {
                "status": graph_store.SCRIPT_READY,
                "error": f"{failed}개 회차를 만들지 못했습니다. 개별 재생성할 수 있습니다." if failed else "",
                "episodesJson": json.dumps(episodes, ensure_ascii=False),
                "doneCount": done,
                "failedCount": failed,
                "traceJson": json.dumps(pack.get("trace") or [], ensure_ascii=False),
            },
        )
        # 대본 한 편(잡 전체)이 결과물 1건이다. 회차마다 세면 12화 대본 한 번에
        # 한도가 12 줄어들고, 사용자는 한도를 아끼려고 회차를 쪼개 요청하게 된다.
        # 그건 이 기능을 쓰지 말라는 것과 같다(콘티가 컷 수로 세지 않는 것과 같은 이유).
        spend_quota(owner, QUOTA_ASSET, "story:script", job_id)
        logger.info("Script job ready id=%s %s/%s화", job_id, done, len(episodes))

    except Exception as error:  # noqa: BLE001 - 스레드에서 예외가 새면 상태가 굳는다
        logger.exception("Script job failed id=%s", job_id)
        fail_script_job(job_id, f"예상치 못한 오류: {error}")
    finally:
        stop_heartbeat()


def _save_script_progress(job_id: str, episodes: list[dict], pack: dict) -> None:
    """진행 상황을 회차마다 저장한다. 화면이 폴링으로 읽는다."""
    done = sum(1 for item in episodes if item.get("status") == "done")
    failed = sum(1 for item in episodes if item.get("status") == "failed")
    try:
        graph_store.update_script_job(
            job_id,
            {
                "episodesJson": json.dumps(episodes, ensure_ascii=False),
                "doneCount": done,
                "failedCount": failed,
                "traceJson": json.dumps(pack.get("trace") or [], ensure_ascii=False),
            },
        )
    except (graph_store.GraphError, graph_store.NotFoundError) as error:
        logger.warning("대본 진행 저장 실패 job=%s: %s", job_id, error)


def owned_script_job(job_id: str):
    job = graph_store.get_script_job(job_id)
    if not job:
        return None, response_error("대본 작업을 찾을 수 없습니다.", 404, "JOB_NOT_FOUND")
    if job["owner"] != g.username and g.role != ROLE_ADMIN:
        return None, response_error("본인 작업만 다룰 수 있습니다.", 403, "JOB_FORBIDDEN")
    return job, None


@app.post("/api/script-jobs/<job_id>/approve")
@require_auth
def story_script_approve(job_id: str):
    """아웃라인을 승인하고 회차 생성을 시작한다.

    이 라우트가 없으면 회차 생성이 시작되지 않는다. 지시서 9절 수용 기준이 그것이다.
    """
    job, error = owned_script_job(job_id)
    if error:
        return error
    if job["status"] != graph_store.SCRIPT_AWAITING:
        return response_error(
            f"승인할 수 있는 상태가 아닙니다(현재: {job['status']}).", 409, "NOT_AWAITING"
        )
    if job["owner"] != g.username:
        return response_error("본인 작업만 승인할 수 있습니다.", 403, "JOB_FORBIDDEN")

    job = graph_store.update_script_job(
        job_id,
        {
            "status": graph_store.SCRIPT_WRITING,
            "approvedAt": datetime.now(timezone.utc).isoformat(),
        },
    )
    thread = threading.Thread(
        target=run_script_job, args=(job_id, g.username),
        name=f"script-{job_id[:8]}", daemon=True,
    )
    thread.start()
    logger.info("Script job approved id=%s by=%s", job_id, g.username)
    return jsonify({"ok": True, "item": job}), 202


@app.post("/api/script-jobs/<job_id>/resume")
@require_auth
def story_script_resume(job_id: str):
    """실패한 대본 잡을 남은 회차부터 이어서 만든다.

    `run_script_job` 은 이미 `status == "done"` 인 회차를 건너뛴다. 즉 이어쓰기 능력은
    처음부터 있었는데 **그 함수를 다시 부를 방법이 없었다.** 그래서 파드 롤아웃이나
    한 번의 읽기 타임아웃으로 잡이 실패하면, 8화까지 만들어 놓고도 사용자가 할 수 있는
    일은 회차별 재생성을 하나씩 누르는 것뿐이었다. 이미 낸 비용을 살리는 경로가 없었다.

    아웃라인은 다시 만들지 않는다. 승인한 아웃라인이 그대로 남아 있어야 회차 사이의
    연속성 근거가 유지된다.
    """
    job, error = owned_script_job(job_id)
    if error:
        return error
    if job["owner"] != g.username:
        return response_error("본인 작업만 이어서 만들 수 있습니다.", 403, "JOB_FORBIDDEN")
    if job["status"] in graph_store.SCRIPT_ACTIVE_STATUSES:
        return response_error(
            f"이미 생성 중입니다(현재: {job['status']}).", 409, "JOB_BUSY"
        )
    if not job.get("outlineKey"):
        return response_error(
            "승인된 아웃라인이 없어 이어서 만들 수 없습니다. 대본을 새로 시작해 주세요.",
            409, "NO_OUTLINE",
        )
    remaining = [
        item for item in (job.get("episodes") or []) if item.get("status") != "done"
    ]
    if not remaining:
        return response_error("남은 회차가 없습니다.", 409, "NOTHING_TO_RESUME")

    job = graph_store.update_script_job(job_id, {"status": graph_store.SCRIPT_WRITING, "error": ""})
    thread = threading.Thread(
        target=run_script_job, args=(job_id, g.username),
        name=f"script-{job_id[:8]}", daemon=True,
    )
    thread.start()
    logger.info("Script job resumed id=%s by=%s remaining=%s", job_id, g.username, len(remaining))
    return jsonify({"ok": True, "item": job, "remaining": len(remaining)}), 202


@app.route("/api/script-jobs/<job_id>", methods=["GET", "DELETE"])
@require_auth
def story_script_detail(job_id: str):
    """진행률 조회(지시서 7절의 "GET 진행률")와 삭제."""
    job, error = owned_script_job(job_id)
    if error:
        return error

    if request.method == "GET":
        job = reap_stale_script_job(job)
        total = len(job.get("episodes") or []) or 1
        # 폴링도 같은 판정을 실어 보낸다. 목록에만 붙이면 첫 폴링 응답이 화면을
        # 덮어써서 "생성 후 스토리 수정됨" 표시가 곧 사라진다.
        project = graph_store.get_project(job.get("projectId") or "") or {}
        return jsonify(
            {
                "ok": True,
                "item": with_story_drift(job, project),
                "progress": {
                    "done": job.get("doneCount") or 0,
                    "failed": job.get("failedCount") or 0,
                    "total": total,
                    "percent": round((job.get("doneCount") or 0) / total * 100),
                },
            }
        )

    if job["owner"] != g.username:
        return response_error("본인 작업만 삭제할 수 있습니다.", 403, "JOB_FORBIDDEN")
    try:
        keys = graph_store.delete_script_job(job_id)
    except graph_store.NotFoundError as error:
        return response_error(str(error), 404, "JOB_NOT_FOUND")
    remove_s3_objects(keys)
    return jsonify({"ok": True, "deleted": job_id})


@app.get("/api/projects/<project_id>/script-jobs")
@require_auth
def story_script_jobs(project_id: str):
    project = graph_store.get_project(project_id)
    if not project:
        return response_error("프로젝트를 찾을 수 없습니다.", 404, "PROJECT_NOT_FOUND")
    if not can_read_project(project):
        return response_error("이 프로젝트에 접근할 권한이 없습니다.", 403, "PROJECT_FORBIDDEN")
    items = [
        with_story_drift(reap_stale_script_job(job), project)
        for job in graph_store.list_script_jobs(g.username, project_id=project_id)
    ]
    return jsonify({"ok": True, "items": items})


@app.get("/api/script-jobs/<job_id>/episodes/<int:episode_no>")
@require_auth
def story_episode_get(job_id: str, episode_no: int):
    """회차 본문을 읽는다."""
    job, error = owned_script_job(job_id)
    if error:
        return error
    entry = next((item for item in (job.get("episodes") or [])
                  if int(item.get("no") or 0) == episode_no), None)
    if not entry:
        return response_error("회차를 찾을 수 없습니다.", 404, "EPISODE_NOT_FOUND")
    if not entry.get("s3Key"):
        return response_error("이 회차는 아직 만들어지지 않았습니다.", 409, "EPISODE_NOT_READY")
    try:
        payload = json.loads(read_source_text(entry["s3Key"]))
    except (BotoCoreError, ClientError, ValueError, json.JSONDecodeError):
        return response_error("회차 본문을 읽지 못했습니다.", 502, "EPISODE_UNREADABLE")
    return jsonify({"ok": True, "item": entry, "payload": payload})


@app.get("/api/script-jobs/<job_id>/episodes/<int:episode_no>/pdf")
@require_auth
def story_episode_pdf(job_id: str, episode_no: int):
    """회차 하나를 PDF 파일로 내려준다. txt 버튼은 그대로 두고 이것을 더한 것이다."""
    job, error = owned_script_job(job_id)
    if error:
        return error
    entry = next((item for item in (job.get("episodes") or [])
                  if int(item.get("no") or 0) == episode_no), None)
    if not entry:
        return response_error("회차를 찾을 수 없습니다.", 404, "EPISODE_NOT_FOUND")
    if not entry.get("s3Key"):
        return response_error("이 회차는 아직 만들어지지 않았습니다.", 409, "EPISODE_NOT_READY")
    return script_pdf_response(job, [entry], f"{episode_no}화")


@app.get("/api/script-jobs/<job_id>/pdf")
@require_auth
def story_script_pdf(job_id: str):
    """대본 전체(만들어진 회차 전부)를 한 PDF로 내려준다.

    회차마다 따로 받게 하면 12화 대본이 파일 12개가 된다. 회의에는 한 권으로 돌린다.
    아직 만들어지지 않은 회차는 건너뛴다 — 5화까지 나왔으면 그 5화가 문서다.
    """
    job, error = owned_script_job(job_id)
    if error:
        return error
    made = [item for item in (job.get("episodes") or []) if item.get("s3Key")]
    if not made:
        return response_error("아직 만들어진 회차가 없습니다.", 409, "NO_EPISODES")
    numbers = [int(item.get("no") or 0) for item in made]
    span = f"{min(numbers)}~{max(numbers)}화" if len(made) > 1 else f"{numbers[0]}화"
    return script_pdf_response(job, made, span)


def script_pdf_response(job: dict, entries: list[dict], span: str):
    """회차 목록을 PDF로 만들어 내려보낸다.

    회차 본문은 S3에 있다. 한 회차를 읽지 못해도 나머지는 내려준다 — 12화 중 1화가
    깨졌다고 문서를 통째로 못 받는 것이 더 나쁘다. 대신 그 회차 자리에 사유를 남긴다.
    """
    project = graph_store.get_project(job["projectId"])
    if not project or not can_read_project(project):
        return response_error("이 작업에 접근할 권한이 없습니다.", 403, "JOB_FORBIDDEN")

    episodes = []
    for entry in entries:
        payload: dict = {}
        try:
            stored = json.loads(read_source_text(entry["s3Key"]))
            # 저장 형태는 {kind: "script_episode", ..., payload: 본문} 이다.
            candidate = stored.get("payload") if isinstance(stored, dict) else None
            payload = candidate if isinstance(candidate, dict) else {}
        except (BotoCoreError, ClientError, ValueError, json.JSONDecodeError) as error:
            logger.warning("회차 본문을 읽지 못했습니다 key=%s: %s", entry.get("s3Key"), error)
        if not payload:
            payload = {"summary": "이 회차 본문을 읽지 못했습니다."}
        episodes.append(
            {"no": entry.get("no"), "title": entry.get("title") or "", "payload": payload}
        )

    title = f"{project.get('title') or '대본'} · {span}"
    meta = " · ".join(part for part in (
        "대본",
        f"분량 {job.get('length')}" if job.get("length") else "",
        f"품질 {job.get('quality')}" if job.get("quality") else "",
    ) if part)

    try:
        data = pdf_export.script_pdf(title, episodes, meta=meta)
    except Exception as error:  # noqa: BLE001
        logger.exception("대본 PDF 생성 실패 job=%s", job["id"])
        return response_error(f"PDF를 만들지 못했습니다: {error}", 502, "PDF_FAILED")

    logger.info("Script PDF job=%s episodes=%s bytes=%s", job["id"], len(episodes), len(data))
    return pdf_response(data, pdf_filename(project.get("title"), "대본", span))


@app.post("/api/script-jobs/<job_id>/episodes/<int:episode_no>/regenerate")
@require_auth
@require_quota(QUOTA_ASSET)
def story_episode_regenerate(job_id: str, episode_no: int):
    """회차 하나만 다시 만든다(지시서 7절).

    잡 전체를 다시 돌리지 않는다. 12화 중 3화만 마음에 들지 않을 때 전부 다시 만들면
    11화분의 비용이 버려진다.

    회차 생성은 요청 안에서 하지 않는다. 회차 하나가 최대 16,000 출력 토큰이라 응답까지
    수 분이 걸리고, ALB idle timeout 아래에서는 브라우저가 반드시 먼저 끊긴다. 회차 루프와
    같은 방식으로 백그라운드에서 만들고 화면은 폴링으로 확인한다.
    """
    job, error = owned_script_job(job_id)
    if error:
        return error
    if job["owner"] != g.username:
        return response_error("본인 작업만 재생성할 수 있습니다.", 403, "JOB_FORBIDDEN")
    if job["status"] in graph_store.SCRIPT_ACTIVE_STATUSES:
        return response_error(
            "생성이 끝난 뒤에 재생성할 수 있습니다.", 409, "JOB_BUSY"
        )

    episodes = list(job.get("episodes") or [])
    position = next(
        (index for index, item in enumerate(episodes) if int(item.get("no") or 0) == episode_no),
        None,
    )
    if position is None:
        return response_error("회차를 찾을 수 없습니다.", 404, "EPISODE_NOT_FOUND")
    if not job.get("outlineKey") or not job.get("contextPackRef"):
        return response_error(
            "이 잡에는 아웃라인이 없어 재생성할 수 없습니다.", 409, "NO_OUTLINE"
        )

    # 진행 중 표시. 이 상태여야 화면이 폴링을 시작하고, 그 사이 다른 재생성이 끼어들지 않는다.
    episodes[position].update({"status": "pending", "error": ""})
    job = graph_store.update_script_job(
        job_id,
        {
            "status": graph_store.SCRIPT_WRITING,
            "error": "",
            "episodesJson": json.dumps(episodes, ensure_ascii=False),
        },
    )
    thread = threading.Thread(
        target=run_episode_regenerate,
        args=(job_id, episode_no, g.username),
        name=f"script-ep-{job_id[:8]}-{episode_no}",
        daemon=True,
    )
    thread.start()
    logger.info("Script episode regenerate started job=%s ep=%s", job_id, episode_no)
    return jsonify({"ok": True, "item": job, "episodeNo": episode_no}), 202


def run_episode_regenerate(job_id: str, episode_no: int, owner: str) -> None:
    """회차 하나를 다시 만든다. 백그라운드 스레드에서 돈다.

    끝나면 잡을 `ready` 로 되돌린다. 실패해도 되돌린다 — 잡을 writing 에 남기면
    사용자는 25분 동안 그 잡으로 아무것도 할 수 없다.
    """
    episodes: list[dict] = []
    pack: dict = {}
    note = ""
    # 잡을 되살리면 안 되는 경우를 표시한다. 아래 finally 는 기본적으로 ready 로
    # 되돌리는데, 프로젝트가 사라진 잡까지 ready 로 만들면 안 된다.
    settled = False
    stop_heartbeat = start_script_heartbeat(job_id)
    try:
        job = graph_store.get_script_job(job_id)
        if not job:
            settled = True
            return
        project = graph_store.get_project(job["projectId"])
        if not project:
            fail_script_job(job_id, "프로젝트가 삭제되었습니다.")
            settled = True
            return

        episodes = list(job.get("episodes") or [])
        position = next(
            (index for index, item in enumerate(episodes) if int(item.get("no") or 0) == episode_no),
            None,
        )
        if position is None:
            note = "회차를 찾을 수 없습니다."
            return

        outline = json.loads(read_source_text(job["outlineKey"]))
        pack = json.loads(read_source_text(job["contextPackRef"]))
        outline_episode = next(
            (item for item in outline.get("episodes") or []
             if int(item.get("no") or 0) == episode_no),
            None,
        )
        if not outline_episode:
            episodes[position].update({"status": "failed", "error": "아웃라인에 이 회차가 없습니다."})
            note = "아웃라인에 이 회차가 없습니다."
            return

        # 직전 회차 요약. 연속성을 유지해야 재생성한 회차가 앞뒤와 맞는다.
        previous_summary = ""
        for item in episodes[:position]:
            if item.get("summary"):
                previous_summary = item["summary"]

        graph_store.touch_script_job(job_id)
        text = story_text_of(project)
        callers = story_callers(owner, job.get("quality") or "")
        try:
            result = story_router.write_episode(
                pack, text, callers, outline_episode,
                arc=job.get("arc") or "", previous_summary=previous_summary,
            )
        except (story_router.StoryError, ClientError, BotoCoreError, ValueError) as error:
            reason = str(error) if isinstance(error, story_router.StoryError) else (
                f"{type(error).__name__}: {error}"
            )
            logger.warning("회차 재생성 실패 job=%s ep=%s: %s", job_id, episode_no, reason)
            episodes[position].update({"status": "failed", "error": reason[:300]})
            note = f"{episode_no}화를 다시 만들지 못했습니다: {reason}"
            return

        episode = result["episode"]
        key, char_count = store_story_json(
            owner, job["projectId"], f"script-ep{episode_no:02d}",
            {
                "kind": "script_episode",
                "aiGenerated": True,
                "generatedAt": datetime.now(timezone.utc).isoformat(),
                "episodeNo": episode_no,
                "payload": episode,
                "validatorNotes": result["notes"],
                "status": result["status"],
            },
        )
        stale = episodes[position].get("s3Key") or ""
        episodes[position].update(
            {
                "status": "done",
                "title": episode.get("title") or outline_episode.get("title") or "",
                "summary": episode.get("summary") or "",
                "endingHook": episode.get("ending_hook") or "",
                "s3Key": key,
                "charCount": char_count,
                "validatorNotes": result["notes"],
                "reviewStatus": result["status"],
                "error": "",
            }
        )
        if stale and stale != key:
            remove_s3_objects([stale])
        spend_quota(owner, QUOTA_ASSET, "story:script-episode", f"{job_id}:{episode_no}")
        logger.info(
            "Script episode regenerated job=%s ep=%s status=%s", job_id, episode_no, result["status"]
        )

    except Exception as error:  # noqa: BLE001 - 스레드에서 예외가 새면 상태가 굳는다
        logger.exception("Script episode regenerate crashed job=%s ep=%s", job_id, episode_no)
        note = f"재생성 중 예상치 못한 오류: {error}"
    finally:
        stop_heartbeat()
        if settled:
            return
        # 성공이든 실패든 잡을 활성 상태에서 빼낸다. writing 에 남기면 사용자는
        # 25분 동안 그 잡으로 아무것도 할 수 없다(동시 실행 1개 제한).
        updates: dict = {"status": graph_store.SCRIPT_READY, "error": note[:400]}
        if episodes:
            updates["episodesJson"] = json.dumps(episodes, ensure_ascii=False)
            updates["doneCount"] = sum(1 for item in episodes if item.get("status") == "done")
            updates["failedCount"] = sum(1 for item in episodes if item.get("status") == "failed")
        if pack.get("trace"):
            updates["traceJson"] = json.dumps(pack["trace"], ensure_ascii=False)
        try:
            graph_store.update_script_job(job_id, updates)
        except (graph_store.GraphError, graph_store.NotFoundError) as error:
            logger.error("재생성 결과 저장에 실패했습니다 job=%s: %s", job_id, error)


# --------------------------------------------------------------------------- #
# 역할 → 모델 표 (관리자 조회)
# --------------------------------------------------------------------------- #
@app.get("/api/admin/story/models")
@require_admin
def story_model_table():
    """지금 실제로 쓰이는 역할→모델 표.

    T0 실측에서 지시서가 지정한 1순위 모델(Sonnet 5 / Opus 5)이 이 계정에서 거부되는 것을
    확인했다. 무엇이 실제로 쓰이는지 화면에서 볼 수 있어야 나중에 권한이 열렸을 때
    바뀐 것을 확인할 수 있다.
    """
    return jsonify(
        {
            "ok": True,
            "region": AWS_REGION,
            "rows": story_models.table(),
            "note": (
                "값은 story_models._DEFAULTS 와 환경변수로 정해집니다. "
                "접근이 거부된 모델은 폴백으로 대체되고 경고가 로그에 남습니다."
            ),
        }
    )


# Whynot product adapters. The shared graph_store project implementation remains unchanged.
_project_track_repository = project_tracks.GraphProjectTrackRepository()
account_guard.verify()
graph_store.bootstrap_if_needed(ADMIN_USERNAME, ADMIN_PASSWORD, seed_user_list(), DEFAULT_COMPANY_CODE)
@app.get("/api/projects/<project_id>/story-text")
@require_auth
def story_text_get(project_id: str):
    """실제 스토리 본문을 읽는다.

    프로젝트 조회 응답에 본문을 넣지 않는 이유: 길이 제한이 없어서 목록 조회가
    수십 MB가 될 수 있다. 프로젝트 응답에는 S3 키와 글자 수만 담고(storyText),
    본문은 상세 화면이 이 라우트로 따로 읽는다.
    """
    project = graph_store.get_project(project_id)
    if not project:
        return response_error("프로젝트를 찾을 수 없습니다.", 404, "PROJECT_NOT_FOUND")
    if not can_read_project(project):
        return response_error("이 프로젝트에 접근할 권한이 없습니다.", 403, "PROJECT_FORBIDDEN")

    return jsonify(
        {
            "ok": True,
            "text": story_text_of(project),
            "charCount": (project.get("storyText") or {}).get("charCount") or 0,
            "storyType": project.get("storyType") or "",
        }
    )


# Install after every shared route has been declared so project-scoped story guards cover deep links
# and late story endpoints without changing graph_store.py or each shared route body.

# ---------------------------------------------------------------------------
# vpoc1 영상 기능. 등록은 이 한 곳뿐이다(PRD 6.2).
# 구 영상 블루프린트와 durable runner 는 제거했다. Luma Ray2 는 비동기 API 라서
# 백그라운드 작업자가 필요 없다 — 화면 조회 때 GetAsyncInvoke 로 묻는다.
# ---------------------------------------------------------------------------

_project_track_repository = project_tracks.GraphProjectTrackRepository()


class _VpocTrackCommandStore:
    """트랙 선택 기록만 남긴다.

    구 영상 상태 저장소가 이 역할을 겸하고 있었다. 그 저장소를 지우면서 기록 위치를
    vpoc 이름공간으로 옮겼다. 같은 트랙을 다시 고르면 reused 다.
    """

    def append_project_command(self, project: dict, command: dict) -> tuple[dict, bool]:
        owner = str(project.get("owner") or "")
        project_id = str(project.get("id") or "")
        key = f"users/{safe_user_prefix(owner)}/video/{project_id}/vpoc1/track-commands.json"
        document, etag = vpoc.store.STORE.read_json(key)
        document = document or {"projectId": project_id, "commands": []}
        for existing in document["commands"]:
            if existing.get("track") == command.get("track"):
                return existing, True
        document["commands"].append(command)
        vpoc.store.STORE.write_json(key, document, etag=etag)
        return command, False


_project_command_store = _VpocTrackCommandStore()

if VIDEO_GEN_ENABLED and vpoc.settings.VPOC_ENABLED:
    app.register_blueprint(
        vpoc.create_blueprint(
            vpoc.VpocDependencies(
                require_auth=require_auth,
                current_username=lambda: g.username,
                get_project=_project_track_repository.get_project,
                can_read_project=can_read_project,
                can_write_project=can_write_project,
                graph_run=graph_store.run,
            )
        )
    )

# vpoc2(2026-09-30): 명령 → 캐릭터 확정 → 15초 영상 → 리뷰. vpoc1 과 파일·라우트·저장 경로가 따로다.
# 등록은 이 한 곳뿐이다. 명세는 vibe-video 저장소 docs/spec-v1.md.
if VIDEO_GEN_ENABLED and vpoc2.settings.ENABLED:
    app.register_blueprint(
        vpoc2.create_blueprint(
            vpoc2.Vpoc2Dependencies(require_auth=require_auth, current_username=lambda: g.username)
        )
    )


# Install after every shared route has been declared so project-scoped story guards cover deep links
# and late story endpoints without changing graph_store.py or each shared route body.
project_tracks.install(app, project_tracks.ProjectTrackDependencies(
    require_auth=require_auth,
    response_error=response_error,
    current_username=lambda: g.username,
    list_projects=graph_store.list_visible_projects,
    project_payload=project_payload,
    can_read_project=can_read_project,
    can_write_project=can_write_project,
    command_store=_project_command_store,
    repository=_project_track_repository,
    story_enabled=STORY_DEV_ENABLED,
    video_enabled=VIDEO_GEN_ENABLED,
))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
