"""AWS Bedrock 모델의 수명주기·가격 근거 정본.

실행계획 v3 P0-1은 ``modelId`` 문자열만 저장하는 카탈로그를 금지한다. 어떤 리전에서
어떤 API로 부르는지, 언제 수명주기를 확인했는지, EOL이 있는지 기계적으로 읽을 수 있어야
한다. 이미지·영상 카탈로그가 이 모듈의 같은 사실을 쓴다.

이 파일은 **계정의 접근 권한 정본이 아니다.** authorization/entitlement은 배포 전 AWS
API로 다시 확인한다. 여기에는 AWS가 공개한 모델 생명주기와 가격 근거만 둔다.

확인 근거 (2026-09-09, 계정 730335451955, ``--profile ai-poc-hub``):

- us-west-2: SD3.5 Large/Core/Ultra와 Luma Ray2 = ACTIVE + AUTHORIZED
- us-east-1: Nova Canvas/Reel 1.0/1.1 = LEGACY + AUTHORIZED
- Nova Canvas/Reel EOL = 2026-09-30. EOL 후 자동 전환되지 않는다.

가격 페이지가 모델에 따라 동적 표만 제공해 정확한 숫자를 안정적으로 추출하지 못하는
항목은 ``priceUsd=None``으로 둔다. **모르는 숫자를 0으로 쓰지 않는다.** Style Guide와
Control Structure는 공식 가격표에서 $0.07/생성을 확인했다.

Amazon 자체 모델은 가격 페이지를 읽지 않고 **AWS Price List API** 로 받는다 (2026-09-21)::

    aws pricing get-products --service-code AmazonBedrock --region us-east-1 \
      --filters Type=TERM_MATCH,Field=model,Value="Nova Reel"

그 결과 Nova Reel I2V/T2V(us-east-1, Medium fps HD) = USD 0.08/초, Nova Canvas
1024 Standard = USD 0.04/장을 확인했다(적용 2026-09-01, 게시 2026-09-17).
**Stability와 Luma는 이 API에 없다** — Marketplace 과금이라 ``AmazonBedrock``
서비스 코드로 조회되지 않는다. 그래서 그 모델들은 ``priceUsd=None``이 남는다.

``ap-northeast-2``에는 영상 출력 모델이 없다 (``list-foundation-models
--by-output-modality VIDEO`` 가 빈 목록). 영상은 us-east-1 또는 us-west-2로 나간다.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

LIFECYCLE_CHECKED_AT = "2026-09-09"
LIFECYCLE_SOURCE = (
    "https://docs.aws.amazon.com/bedrock/latest/userguide/model-lifecycle-legacy.html"
)
PRICE_CHECKED_AT = "2026-09-09"
PRICE_SOURCE = "https://aws.amazon.com/bedrock/pricing/"
# Price List API 로 받은 항목은 게시일까지 남는다. 모델별 priceSource 가 이 값을 덮는다.
PRICE_API_CHECKED_AT = "2026-09-21"
PRICE_API_SOURCE = "aws pricing get-products --service-code AmazonBedrock"

# 추론 프로파일 접두사는 실행 경로이고 기반 모델의 수명주기를 바꾸지 않는다.
_PROFILE_PREFIXES = ("global.", "us.", "apac.", "eu.")

# 모델별 공개 사실. 코드가 임의로 ACTIVE를 추측하지 않도록 확인한 모델만 적는다.
# priceUnit: image | generation | second. priceUsd=None은 가격 미확인이지 무료가 아니다.
MODEL_METADATA: dict[str, dict] = {
    "stability.sd3-5-large-v1:0": {
        "lifecycle": "ACTIVE",
        "apiOperation": "InvokeModel",
        "priceUnit": "image",
        "priceUsd": None,
    },
    "stability.stable-image-core-v1:1": {
        "lifecycle": "ACTIVE",
        "apiOperation": "InvokeModel",
        "priceUnit": "image",
        "priceUsd": None,
    },
    "stability.stable-image-ultra-v1:1": {
        "lifecycle": "ACTIVE",
        "apiOperation": "InvokeModel",
        "priceUnit": "image",
        "priceUsd": None,
    },
    "stability.stable-image-style-guide-v1:0": {
        "lifecycle": "ACTIVE",
        "apiOperation": "InvokeModel",
        "priceUnit": "generation",
        "priceUsd": 0.07,
    },
    "stability.stable-image-control-structure-v1:0": {
        "lifecycle": "ACTIVE",
        "apiOperation": "InvokeModel",
        "priceUnit": "generation",
        "priceUsd": 0.07,
    },
    "luma.ray-v2:0": {
        "lifecycle": "ACTIVE",
        "apiOperation": "StartAsyncInvoke",
        "priceUnit": "second",
        "priceUsd": None,
    },
    "amazon.nova-canvas-v1:0": {
        "lifecycle": "LEGACY",
        "eolAt": "2026-09-30",
        "apiOperation": "InvokeModel",
        "priceUnit": "image",
        # 1024 Standard 기준. Premium 은 0.06, 2048 은 0.06/0.08 이다.
        "priceUsd": 0.04,
        "priceTier": "1024 Standard",
        "priceRegion": "us-east-1",
        "priceCheckedAt": PRICE_API_CHECKED_AT,
        "priceSource": PRICE_API_SOURCE,
        "priceEffectiveAt": "2026-09-01",
    },
    "luma.ray-v2:0": {
        "lifecycle": "ACTIVE",
        "apiOperation": "StartAsyncInvoke",
        "priceUnit": "second",
        # 540p 0.75 USD/초 · 720p 1.50 USD/초. 540p 5초 샷 하나 = USD 3.75.
        "priceUsd": 0.75,
        "priceTier": "540p",
        "priceRegion": "us-west-2",
        "priceCheckedAt": PRICE_API_CHECKED_AT,
        "priceSource": PRICE_API_SOURCE,
        "priceEffectiveAt": "2026-09-01",
    },
}


def foundation_model_id(model_id: str) -> str:
    """추론 프로파일 ID에서 기반 모델 ID를 얻는다."""
    value = str(model_id or "").strip()
    for prefix in _PROFILE_PREFIXES:
        if value.startswith(prefix):
            return value[len(prefix):]
    return value


def metadata_for(model_id: str) -> dict:
    """모델의 확인된 공개 메타데이터. 모르는 모델이면 빈 dict."""
    known = MODEL_METADATA.get(foundation_model_id(model_id)) or {}
    if not known:
        return {}
    # 기본 출처를 먼저 깔고 모델에 적어 둔 값으로 덮는다. 순서를 뒤집으면 Price List API
    # 로 확인한 항목이 가격 페이지 날짜로 되돌아간다 (실측으로 밟은 함정이다).
    result = {
        "lifecycleCheckedAt": LIFECYCLE_CHECKED_AT,
        "lifecycleSource": LIFECYCLE_SOURCE,
        "priceCheckedAt": PRICE_CHECKED_AT,
        "priceSource": PRICE_SOURCE,
        **known,
    }
    return result


def decorate(entry: dict) -> dict:
    """카탈로그 entry에 알려진 메타데이터를 채우고 명시값은 보존한다.

    관리자 override가 최신 확인값을 명시했다면 그것이 이 정적 표보다 우선한다. 다만
    pricing object처럼 한 덩어리로 뭉개지지 않도록 필드 단위로 합친다.
    """
    result = dict(entry)
    known = metadata_for(result.get("modelId") or "")
    for key, value in known.items():
        if key not in result or result[key] in ("", None):
            result[key] = value
    return result


def _eol_date(value: str | None) -> date | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def eol_reached(eol_at: str | None, today: date | None = None) -> bool:
    target = _eol_date(eol_at)
    return bool(target and (today or datetime.now(timezone.utc).date()) >= target)


def days_until_eol(eol_at: str | None, today: date | None = None) -> int | None:
    target = _eol_date(eol_at)
    if not target:
        return None
    return (target - (today or datetime.now(timezone.utc).date())).days
