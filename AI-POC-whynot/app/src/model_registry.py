"""Layered Bedrock model catalog.

Three layers are merged by model `id`, later layers win:

1. builtin   - shipped with the image, guarantees the app always has a catalog
2. configmap - declarative, mounted at MODEL_CATALOG_PATH (GitOps friendly)
3. override  - runtime edits written by an administrator to S3 _config/models.json

Swapping a model therefore never requires a code change: edit the ConfigMap for
a permanent change, or use the admin API for an immediate one.
"""

import copy
import json
import logging
import os
import re
import threading
from datetime import datetime, timezone

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from . import model_capabilities, providers

AWS_REGION = os.getenv("AWS_REGION", "ap-northeast-2")
S3_BUCKET = os.getenv("S3_BUCKET", "mtx-test-ai-change-toon")
# 비동기 출력 버킷은 모델과 같은 리전이어야 한다. 서울에 영상 모델이 없어 타 리전 버킷을 둔다.
S3_VIDEO_BUCKET = os.getenv("S3_VIDEO_BUCKET", "")
VIDEO_REGION = os.getenv("VIDEO_OUTPUT_REGION", "us-west-2")
IMAGE_REGION = os.getenv("COVER_IMAGE_REGION", VIDEO_REGION)
CONFIG_KEY = os.getenv("MODEL_CONFIG_KEY", "_config/models.json")
CATALOG_PATH = os.getenv("MODEL_CATALOG_PATH", "/etc/AI-POC/models.json")
# 내장 이미지 모델. 환경변수 이름은 표지 기능에서 먼저 생겼지만, 지금은 이미지 생성
# 전체의 최후 기본값이다. 서울에 텍스트→이미지 모델이 없어 리전이 영상 리전과 같다.
BUILTIN_IMAGE_MODEL_ID = os.getenv("COVER_IMAGE_MODEL", "stability.sd3-5-large-v1:0")
CACHE_TTL_SECONDS = int(os.getenv("MODEL_CACHE_TTL_SECONDS", "30"))
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{1,62}$")
ALLOWED_INVOCATIONS = {"async", "sync"}
ALLOWED_KINDS = {"video", "image", "text"}
# 품질/비용 등급. styles.TIERS와 같은 값을 쓴다.
ALLOWED_TIERS = {"draft", "standard", "premium"}

logger = logging.getLogger("toon-change-ai-pj-app.model-registry")
_s3 = boto3.client("s3", region_name=AWS_REGION)
_lock = threading.Lock()
_cache: dict = {"loaded_at": 0.0, "models": None, "source": {}, "rejected": []}
# 리전/수명주기 조회 결과 캐시. 매 요청마다 부를 필요가 없는 값이다.
_bucket_regions: dict[str, str | None] = {}
_lifecycles: dict[str, str | None] = {}
_bedrock_clients: dict[str, object] = {}


def _bedrock(region: str):
    if region not in _bedrock_clients:
        _bedrock_clients[region] = boto3.client("bedrock", region_name=region)
    return _bedrock_clients[region]


def bucket_region(bucket: str) -> str | None:
    """버킷의 실제 리전. 조회에 실패하면 None을 돌려주고 검증을 건너뛴다."""
    if not bucket:
        return None
    if bucket in _bucket_regions:
        return _bucket_regions[bucket]
    try:
        location = _s3.get_bucket_location(Bucket=bucket).get("LocationConstraint")
    except (BotoCoreError, ClientError) as error:
        logger.warning("버킷 리전을 확인하지 못했습니다 (%s): %s", bucket, type(error).__name__)
        _bucket_regions[bucket] = None
        return None
    resolved = location or "us-east-1"
    _bucket_regions[bucket] = resolved
    return resolved


def model_lifecycle(model_id: str, region: str) -> str | None:
    """Bedrock 파운데이션 모델의 수명주기 상태(ACTIVE / LEGACY).

    추론 프로파일이나 조회 권한이 없는 경우처럼 확인이 불가능하면 None을 돌려준다.
    '확인 불가'를 '사용 불가'로 취급하면 정상 모델까지 막히므로, 아는 경우에만 판단한다.
    """
    cache_key = f"{region}:{model_id}"
    if cache_key in _lifecycles:
        return _lifecycles[cache_key]
    status: str | None = None
    try:
        detail = _bedrock(region).get_foundation_model(modelIdentifier=model_id)
        status = (detail.get("modelDetails") or {}).get("modelLifecycle", {}).get("status")
    except (BotoCoreError, ClientError) as error:
        logger.info("모델 수명주기를 확인하지 못했습니다 (%s/%s): %s", region, model_id, type(error).__name__)
    _lifecycles[cache_key] = status
    return status


def effective_output_bucket(model: dict) -> str:
    """이 모델의 비동기 출력 버킷.

    모델에 지정된 값이 없으면 리전에 맞춰 고른다. 이 판단이 서버와 검증에서 갈리면
    등록은 통과했는데 생성만 실패하는 상황이 생기므로 한 곳에서만 계산한다.
    """
    explicit = str(model.get("outputBucket") or "").strip()
    if explicit:
        return explicit
    region = str(model.get("region") or AWS_REGION)
    return S3_VIDEO_BUCKET if region != AWS_REGION else S3_BUCKET


# --------------------------------------------------------------------------- #
# provider 추론
# --------------------------------------------------------------------------- #
# `provider` 는 어떤 어댑터로 요청 본문을 만들지 고르는 값이다. 카탈로그를 쓰는 쪽이
# 이것을 빠뜨리면 예전에는 "template" 이 기본값으로 잡혔고, request.template 도 없으니
# providers.resolve() 가 거부했다. 그러면 `_merge` 가 그 항목을 **조용히 버렸다.**
#
# 이것이 실제로 만든 사고: terraform 이 만드는 ConfigMap 과 S3 오버라이드에 provider 가
# 없어서 카탈로그 전체가 버려졌다. 남은 것은 이미지에 내장된 영상 모델 하나뿐이었고,
# 이미지 생성은 server.py 가 들고 있던 별도 폴백으로 내려가 관리자가 지정한 적도 없는
# 모델을 불렀다. 그 모델만 계정에 접근 권한이 없어서 사용자는
# "표지 생성 모델을 사용할 수 없습니다(AccessDeniedException)" 만 보았다.
# 화면의 모델 목록에는 영상 모델 하나만 떠 있었을 뿐, 무엇이 어디서 버려졌는지는
# 어디에도 드러나지 않았다.
#
# modelId 만 있으면 어느 계열인지 판단할 수 있다. 판단할 수 있는 것을 설정 누락으로
# 실패시킬 이유가 없다. 명시된 provider 가 있으면 그것이 항상 이긴다.
_PROVIDER_BY_MODEL_PREFIX: tuple[tuple[str, str], ...] = (
    # 더 긴 접두사를 먼저 본다. stability.stable-image-* 가 stability.* 에 먹히면 안 된다.
    ("stability.stable-image-style-guide", "stability.style-guide"),
    ("stability.stable-image-control-structure", "stability.control-structure"),
    ("stability.", "stability.image"),
    ("luma.", "luma.ray"),
)
# 추론 프로파일 접두사. 리전 라우팅 정보일 뿐이라 계열 판단에서는 떼고 본다.
# (예: us.stability.stable-image-style-guide-v1:0)
_INFERENCE_PROFILE_PREFIXES = ("global.", "us.", "apac.", "eu.")


def infer_provider(bedrock_model_id: str) -> str:
    """modelId 로 어댑터 계열을 추론한다. 모르는 계열은 "template" 로 남긴다.

    "template" 를 돌려주는 경우는 `providers.resolve()` 가 request.template 을 요구하고,
    그것이 없으면 거부한다. 즉 **모르는 모델은 여전히 거부된다.** 이 함수는 아는 것을
    설정 누락 때문에 버리지 않게 하는 것까지만 한다.
    """
    bare = str(bedrock_model_id or "").strip()
    for prefix in _INFERENCE_PROFILE_PREFIXES:
        if bare.startswith(prefix):
            bare = bare[len(prefix) :]
            break
    for prefix, provider in _PROVIDER_BY_MODEL_PREFIX:
        if bare.startswith(prefix):
            return provider
    return "template"


# 이미지에 내장되는 최소 카탈로그. ConfigMap이 없어도 앱이 동작하도록 보장하는 것이 목적이다.
# 여기에는 실제로 호출 가능한 모델만 둔다. LEGACY 모델을 남겨두면 관리자가 그것을 켜서
# 생성이 전부 막히는 일이 실제로 있었다.
#
# **용도(kind)마다 하나씩 있어야 한다.** 예전에는 영상 모델만 있었고, 그래서 이미지
# 생성 쪽은 server.py 가 따로 폴백 모델을 손으로 만들어 들고 있었다. 기본 이미지 모델을
# 적은 곳이 두 군데가 되었고 두 값이 어긋났다. 카탈로그가 비었을 때 앱은 관리자가 지정한
# 적 없는 모델을 불렀고, 그 모델만 계정에 권한이 없어 표지 생성이 전부 실패했다.
BUILTIN_MODELS: list[dict] = [
    {
        "id": "luma-ray2",
        "label": "Luma Ray2 · 짧은 컷 (us-west-2)",
        "kind": "video",
        "provider": "luma.ray",
        "modelId": "luma.ray-v2:0",
        "region": VIDEO_REGION,
        "outputBucket": S3_VIDEO_BUCKET,
        "invocation": "async",
        "enabled": True,
        "isDefault": True,
        "notes": "현재 계정에서 호출 가능한 유일한 영상 모델입니다. 길이는 5초 또는 9초.",
        # imageInput 은 실제 모델 성능을 그대로 적는다. Luma Ray2 는 keyframes.frame0 으로
        # 이미지 입력을 받고 LumaRayAdapter 가 그 본문을 만든다. 여기에 False 를 적어 두면
        # ConfigMap 이 없어 이 항목으로 내려온 순간 server.py 의
        # `capabilities.imageInput` 검사가 거짓이 되어, 업로드한 첫 프레임이 오류 없이
        # 버려진다. 폴백은 기능을 줄이는 자리가 아니라 같은 모델을 계속 쓰는 자리다.
        "capabilities": {
            "imageInput": True,
            "maxDurationSeconds": 9,
            "resolutions": ["540p", "720p"],
            "durations": [5, 9],
        },
        "defaults": {
            "durationSeconds": 5,
            "fps": 24,
            "dimension": "1280x720",
            "resolution": "720p",
            "aspectRatio": "16:9",
        },
    },
    {
        "id": "builtin-image",
        "label": f"{BUILTIN_IMAGE_MODEL_ID} · 내장 기본 ({IMAGE_REGION})",
        "kind": "image",
        "tier": "standard",
        "provider": infer_provider(BUILTIN_IMAGE_MODEL_ID),
        "modelId": BUILTIN_IMAGE_MODEL_ID,
        "region": IMAGE_REGION,
        "invocation": "sync",
        "enabled": True,
        "isDefault": True,
        # 빈 자리를 메우는 항목이다. 카탈로그에 쓸 수 있는 image 모델이 하나라도 있으면
        # `_merge` 가 이 항목을 목록에서 뺀다. 그러지 않으면 관리자 화면에 같은 모델이
        # 두 줄로 뜨고, 등급 라우팅이 관리자가 지정한 항목 대신 이것을 고를 수 있다.
        "fallbackOnly": True,
        "notes": (
            "ConfigMap 카탈로그에 이미지 모델이 없을 때만 쓰이는 내장 기본값입니다. "
            "카탈로그에 이미지 모델이 하나라도 있으면 이 항목은 목록에서 사라집니다."
        ),
        "capabilities": {
            "imageInput": False,
            "stylePrompt": True,
            "aspectRatios": ["1:1", "2:3", "3:2", "4:5", "16:9", "9:16", "21:9", "9:21", "5:4"],
        },
        "defaults": {"aspectRatio": "16:9"},
    },
]


class CatalogError(ValueError):
    """Raised when a catalog entry is rejected."""


def validate_entry(entry: dict) -> dict:
    if not isinstance(entry, dict):
        raise CatalogError("모델 정의는 객체여야 합니다.")

    model_id = str(entry.get("id", "")).strip().lower()
    if not ID_PATTERN.match(model_id):
        raise CatalogError("모델 id는 소문자, 숫자, '.', '_', '-' 조합으로 2~63자여야 합니다.")

    bedrock_model_id = str(entry.get("modelId", "")).strip()
    if not bedrock_model_id or len(bedrock_model_id) > 200:
        raise CatalogError("Bedrock modelId는 1~200자 문자열이어야 합니다.")

    region = str(entry.get("region", AWS_REGION)).strip()
    if not re.match(r"^[a-z]{2}(-[a-z]+)+-\d$", region):
        raise CatalogError(f"리전 형식이 올바르지 않습니다: {region}")

    invocation = str(entry.get("invocation", "async")).strip()
    if invocation not in ALLOWED_INVOCATIONS:
        raise CatalogError(f"invocation은 {sorted(ALLOWED_INVOCATIONS)} 중 하나여야 합니다.")

    # 영상용과 콘티 이미지용을 같은 카탈로그에 두고 용도로 구분한다.
    kind = str(entry.get("kind") or "video").strip().lower()
    if kind not in ALLOWED_KINDS:
        raise CatalogError(f"kind는 {sorted(ALLOWED_KINDS)} 중 하나여야 합니다.")

    # 품질/비용 등급. 스타일 라우터가 용도에 따라 이 값으로 모델을 고른다.
    tier = str(entry.get("tier") or "standard").strip().lower()
    if tier not in ALLOWED_TIERS:
        raise CatalogError(f"tier는 {sorted(ALLOWED_TIERS)} 중 하나여야 합니다.")

    defaults = entry.get("defaults") or {}
    if not isinstance(defaults, dict):
        raise CatalogError("defaults는 객체여야 합니다.")
    duration = int(defaults.get("durationSeconds", 6))
    if not 1 <= duration <= 120:
        raise CatalogError("defaults.durationSeconds는 1~120 사이여야 합니다.")

    request_block = entry.get("request") or {}
    if request_block and not isinstance(request_block, dict):
        raise CatalogError("request는 객체여야 합니다.")

    normalized_defaults = dict(defaults)
    # provider별 기본값(aspectRatio, resolution, fidelity, loop)을 버리지 않는다.
    # 공통 세 필드만 타입을 고정한다.
    normalized_defaults.update(
        {
            "durationSeconds": duration,
            "fps": int(defaults.get("fps", 24)),
            "dimension": str(defaults.get("dimension", "1280x720")),
        }
    )

    normalized = {
        "id": model_id,
        "label": str(entry.get("label") or model_id)[:120],
        "kind": kind,
        "tier": tier,
        "provider": str(entry.get("provider") or "").strip() or infer_provider(bedrock_model_id),
        "modelId": bedrock_model_id,
        "region": region,
        "invocation": invocation,
        "enabled": bool(entry.get("enabled", True)),
        "isDefault": bool(entry.get("isDefault", False)),
        "notes": str(entry.get("notes") or "")[:400],
        "capabilities": entry.get("capabilities") or {},
        "defaults": normalized_defaults,
    }
    normalized = model_capabilities.decorate(
        {
            **normalized,
            **{
                key: entry[key]
                for key in (
                    "lifecycle", "lifecycleCheckedAt", "lifecycleSource", "eolAt",
                    "apiOperation", "priceUnit", "priceUsd", "priceCheckedAt", "priceSource",
                )
                if key in entry
            },
        }
    )
    if request_block:
        normalized["request"] = request_block
    if entry.get("fallbackOnly"):
        normalized["fallbackOnly"] = True
    if entry.get("outputBucket"):
        normalized["outputBucket"] = str(entry["outputBucket"]).strip()
    for field in ("updatedAt", "updatedBy", "source"):
        if entry.get(field):
            normalized[field] = entry[field]

    # adapter와 카탈로그의 호출 방식이 다르면 InvokeModel/StartAsyncInvoke를 서로
    # 바꿔 부르는 오류가 되므로 유료 제출 전에 거부한다.
    adapter = providers.resolve(normalized)
    if adapter.invocation != invocation:
        raise CatalogError(
            f"'{model_id}' invocation={invocation} 이 adapter({adapter.invocation})와 다릅니다."
        )
    return normalized


def assert_usable(entry: dict) -> None:
    """활성화하려는 모델이 실제로 호출 가능한지 확인한다.

    등록 시점에 막지 않으면 사용자가 생성 버튼을 누른 뒤에야 실패를 보게 된다.
    실제로 그런 일이 있었다. LEGACY 모델이 기본값으로 지정되어 영상 생성이 전부 막혔다.
    """
    model_id = entry["modelId"]
    region = entry["region"]

    if model_capabilities.eol_reached(entry.get("eolAt")):
        raise CatalogError(
            f"'{model_id}'는 {entry.get('eolAt')}에 EOL이 되어 사용할 수 없습니다. "
            "ACTIVE 모델로 전환해 주세요."
        )
    status = model_lifecycle(model_id, region) or entry.get("lifecycle")
    if status and str(status).upper() == "LEGACY":
        suffix = f" EOL: {entry.get('eolAt')}." if entry.get("eolAt") else ""
        raise CatalogError(
            f"'{model_id}'는 {region}에서 LEGACY 상태여서 신규 기본 모델로 사용할 수 없습니다."
            f"{suffix} 다른 ACTIVE 모델을 사용해 주세요."
        )

    # 비동기 호출은 출력 버킷이 모델과 같은 리전이어야 한다.
    if entry.get("invocation") == "async":
        bucket = effective_output_bucket(entry)
        if not bucket:
            raise CatalogError(
                f"'{entry['id']}'는 비동기 모델인데 출력 버킷이 없습니다. "
                f"{region} 리전 버킷을 outputBucket으로 지정해 주세요."
            )
        resolved = bucket_region(bucket)
        if resolved and resolved != region:
            raise CatalogError(
                f"'{entry['id']}'는 {region}에서 실행되는데 출력 버킷 {bucket}은 {resolved}에 있습니다. "
                f"{region} 리전 버킷을 outputBucket으로 지정해 주세요."
            )


# --------------------------------------------------------------------------- #
# 기능이 특정 provider 계열에 의존하는 지점
# --------------------------------------------------------------------------- #
# `resolve_for_generation(provider=...)` 로 **계열을 지정해서** 모델을 찾는 기능들이다.
# 그 계열의 활성 모델이 하나도 없으면 그 기능만 죽는다. 다른 기능은 멀쩡하다.
#
# 이 표가 필요한 이유는 실제로 겪은 사고 때문이다. 관리자 화면에서 `control-structure`
# 모델을 비활성화하자 화풍 변환 미리보기가 전부 실패했다. 화면은 "비활성"이라고만 보여 줬고,
# 그것이 어느 기능을 죽이는지는 어디에도 적혀 있지 않았다. 사용자에게는
# `'stability.control-structure' 계열의 사용 가능한 모델이 없습니다.` 라는, 내부 식별자가
# 그대로 노출된 문구만 보였다. 원인을 찾는 데 클러스터 안까지 들어가야 했다.
#
# 또 배포로는 복구되지 않는다. S3 오버라이드가 ConfigMap 선언보다 우선하므로 몇 번을
# 배포해도 비활성 상태가 유지된다. 그래서 끄는 시점에 막는 것이 유일하게 효과적인 지점이다.
PROVIDER_FEATURES: dict[str, str] = {
    "stability.control-structure": "NEW_영상 제작의 화풍 변환 미리보기",
    "stability.style-guide": "원작 그림체 매칭(참조 이미지로 그리기)",
}


def assert_not_last_for_feature(entry: dict) -> None:
    """이 모델을 끄면 특정 기능이 통째로 멈추는 경우 거부한다.

    kind 기본 모델과는 다른 문제다. 기본 모델이 없어지면 `_merge` 가 다른 활성 모델을
    자동 승격하지만, 계열을 지정해서 찾는 기능은 **대체 후보 자체가 없다.**

    거부만 하고 우회로를 알려 주지 않으면 관리자가 막힌다. 그래서 무엇을 먼저 하면 되는지를
    메시지에 담는다(같은 계열 모델을 하나 더 등록한 뒤에 끄는 것).
    """
    provider = entry.get("provider")
    feature = PROVIDER_FEATURES.get(provider)
    if not feature:
        return

    others = [
        model
        for model in enabled_models(entry.get("kind"))
        if model.get("provider") == provider and model["id"] != entry["id"]
    ]
    if others:
        return

    raise CatalogError(
        f"'{entry['id']}'는 {feature} 에 쓰이는 마지막 '{provider}' 계열 모델입니다. "
        "지금 끄면 그 기능이 전부 실패합니다. 같은 계열 모델을 하나 더 등록한 뒤에 끄거나, "
        "기능을 정말 중단해야 한다면 모델 카탈로그 설정(ConfigMap)에서 함께 정리해 주세요."
    )


def _read_configmap() -> list[dict]:
    try:
        with open(CATALOG_PATH, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError) as error:
        logger.warning("ConfigMap 카탈로그를 읽지 못했습니다: %s", error)
        return []
    return payload.get("models", []) if isinstance(payload, dict) else []


def _read_override() -> list[dict]:
    try:
        response = _s3.get_object(Bucket=S3_BUCKET, Key=CONFIG_KEY)
        payload = json.loads(response["Body"].read())
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") in {"NoSuchKey", "404", "AccessDenied"}:
            return []
        raise
    except (BotoCoreError, json.JSONDecodeError) as error:
        logger.warning("S3 모델 오버라이드를 읽지 못했습니다: %s", error)
        return []
    return payload.get("models", []) if isinstance(payload, dict) else []


def _merge(layers: list[tuple[str, list[dict]]]) -> tuple[list[dict], list[dict]]:
    """(사용할 모델, 거부된 정의) 를 돌려준다.

    거부 목록을 함께 돌려주는 이유: 항목이 버려지면 그 모델을 쓰는 기능이 죽는데,
    예전에는 그것이 컨테이너 로그의 warning 한 줄로만 남았다. 관리자 화면에는 모델이
    처음부터 없었던 것처럼 보였고, 사용자는 엉뚱한 폴백 모델의 오류를 받았다.
    무엇이 왜 버려졌는지는 그 판단을 한 자리에서만 알 수 있으므로 여기서 들고 나간다.
    """
    merged: dict[str, dict] = {}
    dropped: list[dict] = []
    for source, entries in layers:
        for raw in entries:
            try:
                entry = validate_entry(raw)
            except (CatalogError, providers.ProviderError) as error:
                entry_id = str((raw or {}).get("id") or "(id 없음)") if isinstance(raw, dict) else "(객체 아님)"
                # warning 이 아니라 error 다. 항목이 하나 버려지면 그 모델을 쓰는 기능이 멈춘다.
                logger.error(
                    "모델 정의를 거부했습니다 (%s / %s): %s 이 모델을 쓰는 기능이 동작하지 않습니다.",
                    source,
                    entry_id,
                    error,
                )
                dropped.append({"id": entry_id, "source": source, "reason": str(error)})
                continue
            entry["source"] = source
            existing = merged.get(entry["id"], {})
            if existing:
                # **이 계층이 실제로 적은 키만 덮는다.**
                #
                # `validate_entry` 는 빠진 필드를 기본값으로 채워 완전한 dict 를 돌려준다.
                # 그것을 그대로 덮으면 일부 필드만 적은 정의가 앞 계층의 나머지 필드를
                # 기본값으로 **지운다.** 덮어쓰기가 아니라 초기화가 되는 것이다.
                #
                # 실제 사고: S3 오버라이드(`_config/models.json`)는 terraform 이 한 번만
                # 심고 이후 손대지 않는다(ignore_changes). 그 씨앗 값에는 tier 와
                # capabilities 가 없다. 오버라이드가 가장 높은 계층이므로, 그대로 덮으면
                # ConfigMap 이 제대로 적어 둔 tier(draft/premium)가 전부 standard 로
                # 뭉개지고 capabilities.imageInput 이 사라진다. 등급 라우팅이 무력해지고
                # 업로드한 첫 프레임이 조용히 버려진다.
                #
                # id 와 modelId 는 항목의 정체라서 항상 유지한다. source 는 어느 계층이
                # 최종적으로 이겼는지를 가리켜야 하므로 이것도 항상 새 값을 쓴다.
                specified = set(raw) | {"id", "modelId", "source"}
                entry = {key: value for key, value in entry.items() if key in specified}
            merged[entry["id"]] = {**existing, **entry}

    models = list(merged.values())

    # 빈 자리를 메우는 항목(fallbackOnly)은 같은 용도에 진짜 모델이 있으면 뺀다.
    # 남겨 두면 같은 모델이 관리자 화면에 두 줄로 뜨고, 등급 라우팅이 관리자가 등록한
    # 항목 대신 내장 항목을 고를 수 있다.
    real_kinds = {
        model.get("kind", "video")
        for model in models
        if model.get("enabled") and not model.get("fallbackOnly")
    }
    models = [
        model
        for model in models
        if not (model.get("fallbackOnly") and model.get("kind", "video") in real_kinds)
    ]

    # 기본 모델은 용도(kind)별로 하나만 둔다. 영상 기본값과 콘티 기본값은 별개다.
    # 여러 계층이 같은 kind 에서 기본값을 주장하면 **나중 계층이 이긴다.** 계층 순서가
    # 곧 우선순위라고 문서에 적어 두고 여기서만 다르게 처리하면, 관리자가 지정한 기본
    # 모델이 내장 항목에 밀려 조용히 무시된다.
    rank = {source: index for index, (source, _entries) in enumerate(layers)}
    for kind in {model.get("kind", "video") for model in models}:
        same_kind = [model for model in models if model.get("kind", "video") == kind]
        defaults = [model for model in same_kind if model.get("isDefault") and model.get("enabled")]
        if not defaults:
            for model in same_kind:
                model["isDefault"] = False
            enabled = next((model for model in same_kind if model.get("enabled")), None)
            if enabled:
                enabled["isDefault"] = True
        elif len(defaults) > 1:
            winner = max(defaults, key=lambda model: rank.get(model.get("source", ""), -1))
            for model in defaults:
                model["isDefault"] = model is winner
    ordered = sorted(
        models, key=lambda item: (item.get("kind", "video"), not item["isDefault"], item["id"])
    )
    return ordered, dropped


def load(force: bool = False) -> list[dict]:
    now = datetime.now(timezone.utc).timestamp()
    with _lock:
        cached = _cache.get("models")
        if cached is not None and not force and now - _cache["loaded_at"] < CACHE_TTL_SECONDS:
            return copy.deepcopy(cached)
        models, dropped = _merge(
            [
                ("builtin", copy.deepcopy(BUILTIN_MODELS)),
                ("configmap", _read_configmap()),
                ("override", _read_override()),
            ]
        )
        _cache.update({"models": models, "loaded_at": now, "rejected": dropped})
        return copy.deepcopy(models)


def rejected() -> list[dict]:
    """마지막 로드에서 거부된 카탈로그 정의. [{id, source, reason}]

    빈 목록이 정상이다. 비어 있지 않으면 그 모델을 쓰는 기능이 지금 동작하지 않는다.
    """
    load()  # 아직 로드하지 않았다면 여기서 채워진다.
    with _lock:
        return copy.deepcopy(_cache.get("rejected") or [])


def enabled_models(kind: str | None = None) -> list[dict]:
    models = [model for model in load() if model.get("enabled")]
    if kind:
        models = [model for model in models if model.get("kind", "video") == kind]
    return models


def get(model_id: str) -> dict | None:
    for model in load():
        if model["id"] == model_id:
            return model
    return None


def resolve_for_generation(
    model_id: str | None, kind: str = "video", tier: str | None = None, provider: str | None = None
) -> dict:
    """호출할 모델을 고른다.

    우선순위: 명시된 model_id > (provider, tier) 조건 > 해당 kind의 기본 모델.
    tier로 고르는 경우가 스타일 라우터 경로다. 요청한 등급이 없으면 기본 모델로 내려간다.
    """
    models = enabled_models(kind)
    if not models:
        # 거부된 정의가 있으면 그것이 원인이다. "모델이 없다" 로 끝내면 관리자가
        # 카탈로그에 항목이 있는데도 없다는 말을 듣고 원인을 찾지 못한다.
        dropped = rejected()
        if dropped:
            raise CatalogError(
                f"'{kind}' 용도로 쓸 수 있는 모델이 없습니다. "
                f"카탈로그 정의 {len(dropped)}건이 거부되었습니다: "
                + "; ".join(f"{item['source']}/{item['id']} - {item['reason']}" for item in dropped[:3])
            )
        raise CatalogError(f"'{kind}' 용도로 등록된 모델이 없습니다. 관리자에게 문의해 주세요.")

    if model_id:
        selected = next((model for model in models if model["id"] == model_id), None)
        if not selected:
            raise CatalogError(f"모델 '{model_id}'을 사용할 수 없습니다.")
    else:
        candidates = models
        if provider:
            candidates = [model for model in candidates if model.get("provider") == provider]
            if not candidates:
                raise CatalogError(f"'{provider}' 계열의 사용 가능한 모델이 없습니다.")
        if tier:
            by_tier = [model for model in candidates if model.get("tier") == tier]
            if by_tier:
                candidates = by_tier
            else:
                logger.info("요청한 등급(%s) 모델이 없어 기본 모델을 사용합니다.", tier)
        selected = next((model for model in candidates if model.get("isDefault")), candidates[0])
    # 오래된 오버라이드로 사용 불가 모델이 남아 있을 수 있어 호출 직전에 한 번 더 확인한다.
    assert_usable(selected)
    return selected


def _write_override(models: list[dict], actor: str) -> None:
    body = json.dumps(
        {
            "version": 1,
            "updatedAt": datetime.now(timezone.utc).isoformat(),
            "updatedBy": actor,
            "models": models,
        },
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8")
    _s3.put_object(
        Bucket=S3_BUCKET,
        Key=CONFIG_KEY,
        Body=body,
        ContentType="application/json",
        ServerSideEncryption="AES256",
    )
    with _lock:
        _cache.update({"models": None, "loaded_at": 0.0})


def upsert(entry: dict, actor: str) -> dict:
    """Register or update a model. Administrator action."""
    validated = validate_entry(entry)
    if validated.get("enabled"):
        assert_usable(validated)
    validated["updatedAt"] = datetime.now(timezone.utc).isoformat()
    validated["updatedBy"] = actor
    validated.pop("source", None)

    overrides = {model["id"]: model for model in _read_override() if isinstance(model, dict) and model.get("id")}
    overrides[validated["id"]] = validated

    if validated.get("isDefault"):
        for model_id, model in overrides.items():
            if model_id != validated["id"]:
                model["isDefault"] = False

    _write_override(list(overrides.values()), actor)
    return get(validated["id"]) or validated


def set_default(model_id: str, actor: str) -> dict:
    target = get(model_id)
    if not target:
        raise CatalogError(f"모델 '{model_id}'을 찾을 수 없습니다.")
    if not target.get("enabled"):
        raise CatalogError("비활성 모델은 기본 모델로 지정할 수 없습니다.")
    assert_usable(target)

    overrides = {model["id"]: model for model in _read_override() if isinstance(model, dict) and model.get("id")}
    merged = {**target, "isDefault": True}
    merged.pop("source", None)
    overrides[model_id] = merged
    for other_id, model in overrides.items():
        if other_id != model_id:
            model["isDefault"] = False
    _write_override(list(overrides.values()), actor)
    return get(model_id) or merged


def set_enabled(model_id: str, enabled: bool, actor: str) -> dict:
    target = get(model_id)
    if not target:
        raise CatalogError(f"모델 '{model_id}'을 찾을 수 없습니다.")
    if enabled:
        assert_usable(target)
    else:
        assert_not_last_for_feature(target)

    overrides = {model["id"]: model for model in _read_override() if isinstance(model, dict) and model.get("id")}
    merged = {**target, "enabled": enabled}
    merged.pop("source", None)
    if not enabled:
        merged["isDefault"] = False
    overrides[model_id] = merged
    _write_override(list(overrides.values()), actor)
    return get(model_id) or merged
