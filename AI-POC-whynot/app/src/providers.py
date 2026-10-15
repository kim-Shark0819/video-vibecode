"""Provider adapters that translate a generation request into a Bedrock payload.

Adding a brand new model family means adding one adapter here. Adding another
model of a family that already exists, or a model whose request body is only
slightly different, needs no code at all: an administrator can register it in
the catalog with a `request.template` and the TemplateAdapter handles it.
"""

import copy
import re
from typing import Any

PLACEHOLDER = re.compile(r"^\{\{\s*([a-zA-Z0-9_]+)\s*\}\}$")


class ProviderError(ValueError):
    """Raised when a generation request cannot be built for a model."""


def _substitute(node: Any, values: dict[str, Any]) -> Any:
    """Replace {{placeholder}} strings anywhere inside a JSON-like structure."""
    if isinstance(node, dict):
        result = {}
        for key, value in node.items():
            replaced = _substitute(value, values)
            if replaced is not None:
                result[key] = replaced
        return result
    if isinstance(node, list):
        return [_substitute(item, values) for item in node]
    if isinstance(node, str):
        match = PLACEHOLDER.match(node.strip())
        if match:
            return values.get(match.group(1))
        return node
    return node


class BaseAdapter:
    """Contract every provider adapter implements."""

    key = "base"
    invocation = "async"

    def build_request(self, model: dict, request: dict) -> dict:
        raise NotImplementedError

    def output_location(self, model: dict, request: dict) -> str | None:
        bucket = model.get("outputBucket") or request.get("outputBucket")
        prefix = request.get("outputPrefix")
        if not bucket or not prefix:
            return None
        return f"s3://{bucket}/{prefix}"


class LumaRayAdapter(BaseAdapter):
    """Luma Ray2: 텍스트 및 이미지 기반 영상 생성 (비동기).

    Bedrock 문서 기준 사실:
      - 본문은 평평한 구조이고 duration/resolution은 숫자가 아니라 문자열이다("5s", "720p").
      - duration은 5s / 9s, resolution은 540p / 720p만 허용한다. 1080p나 1분짜리는 없다.
      - prompt는 1~5000자.
      - **이미지 입력을 지원한다.** keyframes.frame0에 base64 이미지를 넣으면 image-to-video가 된다.
        frame1까지 주면 시작/끝 프레임을 지정할 수 있다.
      - 부정 프롬프트(negative_prompt)는 없다. 원하지 않는 것을 서술하면 오히려 그것이 나타난다.
    """

    key = "luma.ray"
    invocation = "async"
    ALLOWED_DURATIONS = {5, 9}
    ALLOWED_RESOLUTIONS = {"540p", "720p"}
    ALLOWED_ASPECTS = {"1:1", "16:9", "9:16", "4:3", "3:4", "21:9", "9:21"}
    # Bedrock이 받는 이미지 형식. webp는 받지 않으므로 호출 전에 변환해야 한다.
    MEDIA_TYPES = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg"}

    def build_request(self, model: dict, request: dict) -> dict:
        prompt = (request.get("prompt") or "").strip()
        if not prompt:
            raise ProviderError("영상 생성 프롬프트가 필요합니다.")

        defaults = model.get("defaults", {})
        requested = int(request.get("durationSeconds") or defaults.get("durationSeconds", 5))
        # 허용 길이가 고정이라 가장 가까운 값으로 맞춘다.
        duration = min(self.ALLOWED_DURATIONS, key=lambda allowed: abs(allowed - requested))

        resolution = str(request.get("resolution") or defaults.get("resolution") or "720p")
        if resolution not in self.ALLOWED_RESOLUTIONS:
            raise ProviderError(f"Luma Ray2가 지원하지 않는 해상도입니다: {resolution}")

        aspect = str(request.get("aspectRatio") or defaults.get("aspectRatio") or "16:9")
        if aspect not in self.ALLOWED_ASPECTS:
            raise ProviderError(f"Luma Ray2가 지원하지 않는 화면 비율입니다: {aspect}")

        payload: dict[str, Any] = {
            "prompt": prompt[:5000],
            "aspect_ratio": aspect,
            "loop": bool(request.get("loop", defaults.get("loop", False))),
            "duration": f"{duration}s",
            "resolution": resolution,
        }

        image_base64 = request.get("imageBase64")
        if image_base64:
            image_format = str(request.get("imageFormat") or "png").lower()
            media_type = self.MEDIA_TYPES.get(image_format)
            if not media_type:
                raise ProviderError(
                    f"Luma Ray2는 PNG 또는 JPEG 이미지만 받습니다(전달된 형식: {image_format})."
                )
            # 업로드한 장면을 첫 프레임으로 고정한다. 이렇게 해야 원본 이미지가 실제로 반영된다.
            payload["keyframes"] = {
                "frame0": {
                    "type": "image",
                    "source": {"type": "base64", "media_type": media_type, "data": image_base64},
                }
            }
        return payload


class StabilityImageAdapter(BaseAdapter):
    """Stability 계열 이미지 모델: 동기 invoke_model.

    표지와 콘티가 같은 어댑터를 쓰고 화면 비율만 다르게 준다.
    """

    key = "stability.image"
    invocation = "sync"
    ALLOWED_ASPECTS = {"1:1", "2:3", "3:2", "4:5", "5:4", "9:16", "16:9", "9:21", "21:9"}

    def build_request(self, model: dict, request: dict) -> dict:
        prompt = (request.get("prompt") or "").strip()
        if not prompt:
            raise ProviderError("이미지 생성 프롬프트가 필요합니다.")

        defaults = model.get("defaults", {})
        aspect = str(request.get("aspectRatio") or defaults.get("aspectRatio") or "16:9")
        if aspect not in self.ALLOWED_ASPECTS:
            raise ProviderError(f"지원하지 않는 화면 비율입니다: {aspect}")

        payload: dict[str, Any] = {
            "prompt": prompt[:4000],
            "mode": "text-to-image",
            "aspect_ratio": aspect,
            "output_format": "png",
        }
        negative = (request.get("negativePrompt") or defaults.get("negativePrompt") or "").strip()
        if negative:
            payload["negative_prompt"] = negative[:1000]
        seed = request.get("seed")
        if seed is not None:
            payload["seed"] = int(seed)
        return payload


class StabilityStyleGuideAdapter(BaseAdapter):
    """Stability Style Guide: 참조 이미지의 화풍으로 새 장면을 그린다.

    프롬프트로는 특정 IP의 그림체를 재현할 수 없다. 원작 컷을 참조로 주면 그 화풍을 추출해
    다른 장면을 같은 그림체로 그려 준다. 웹툰 원작을 가진 고객에게는 이게 핵심이다.

    주의: 이 모델은 온디맨드 호출을 지원하지 않는다. `us.` 추론 프로파일 ID로 불러야 한다.
    (실측: stability.stable-image-style-guide-v1:0 직접 호출 시 ValidationException)
    """

    key = "stability.style-guide"
    invocation = "sync"

    def build_request(self, model: dict, request: dict) -> dict:
        prompt = (request.get("prompt") or "").strip()
        if not prompt:
            raise ProviderError("이미지 생성 프롬프트가 필요합니다.")
        reference = request.get("referenceBase64")
        if not reference:
            raise ProviderError("이 모델은 그림체 참조 이미지가 필요합니다.")

        defaults = model.get("defaults", {})
        aspect = str(request.get("aspectRatio") or defaults.get("aspectRatio") or "16:9")
        if aspect not in StabilityImageAdapter.ALLOWED_ASPECTS:
            raise ProviderError(f"지원하지 않는 화면 비율입니다: {aspect}")

        # get(key, default)는 키가 있고 값이 None이면 default를 쓰지 않는다. 명시적으로 확인한다.
        raw_fidelity = request.get("styleFidelity")
        if raw_fidelity is None:
            raw_fidelity = defaults.get("fidelity", 0.6)
        try:
            fidelity = float(raw_fidelity)
        except (TypeError, ValueError):
            raise ProviderError("styleFidelity는 0~1 사이 숫자여야 합니다.")
        if not 0 <= fidelity <= 1:
            raise ProviderError("styleFidelity는 0~1 사이여야 합니다.")

        payload: dict[str, Any] = {
            "prompt": prompt[:9000],
            "image": reference,
            "aspect_ratio": aspect,
            "output_format": "png",
            "fidelity": fidelity,
        }
        negative = (request.get("negativePrompt") or defaults.get("negativePrompt") or "").strip()
        if negative:
            payload["negative_prompt"] = negative[:9000]
        seed = request.get("seed")
        if seed is not None:
            payload["seed"] = int(seed)
        return payload


class StabilityControlStructureAdapter(BaseAdapter):
    """Stability Control Structure: 입력 이미지의 구도를 유지하며 화풍만 교체한다.

    웹툰 컷을 실사 스틸로 바꾸는 단계에 쓴다. 이 모델이 필요한 이유가 분명하다.

    일반 image-to-image(`mode=image-to-image` + `strength`)로는 이 일을 할 수 없다.
    `strength`를 낮추면 만화 느낌이 그대로 남고, 높이면 원본 구도가 무너진다. 둘 사이에
    "구도는 그대로, 렌더링만 교체"에 해당하는 지점이 없다. 이 모델은 입력을 **구조
    조건**으로만 쓰고 겉모습은 프롬프트가 정하므로 그 요구에 정확히 맞는다.

    ## 실측한 허용 필드

        image, prompt, control_strength, negative_prompt, seed, output_format, style_preset

    없는 필드를 넣어 호출하면 오류가 목록을 그대로 알려 준다. 이 프로젝트에서 `style_preset`
    문제를 그렇게 알아냈고, 여기서도 같은 방법으로 확인했다.

    **`aspect_ratio`가 없다.** 출력 비율이 입력 이미지 비율을 따른다. 그래서 호출부가
    원하는 비율로 만들려면 입력 이미지를 먼저 그 비율로 손질해야 한다.
    StabilityImageAdapter / StyleGuide 를 재사용할 수 없는 이유가 이것이다(둘 다
    `aspect_ratio`를 보낸다).

    ## `style_preset`을 쓰지 않는 이유

    이 모델은 받지만 보내지 않는다. 이 서비스의 그림체는 `styles.py`의 프리셋(lead +
    prompt + negative 문장)으로 표현한다. Stability 자체 `style_preset` 열거값을 함께
    쓰면 그림체를 정하는 체계가 두 벌이 되어, 결과가 기대와 다를 때 어느 쪽이 이겼는지
    알 수 없다. 한 체계로 유지한다.

    ## 구조만 조건이라는 것의 실제 의미 (실측)

    같은 입력에 프롬프트만 바꿔 실사본과 웹툰본을 뽑아 보았다. 구도·포즈·시선·간판 위치는
    유지됐지만 **색·시간대·날씨·머리색은 프롬프트가 새로 만들었다.** 밤이 낮이 되고 검은
    머리가 금발이 됐다. 즉 인물 외형과 장면 정보를 프롬프트가 싣지 않으면 모델이 지어낸다.
    호출부(`toon_convert.py`)가 그 블록을 만들어 넣는 것이 이 때문이다.
    """

    key = "stability.control-structure"
    invocation = "sync"
    # 구조를 얼마나 붙잡을지. 0에 가까우면 프롬프트가 자유롭고 1에 가까우면 구도가 굳는다.
    # 0.6은 실측에서 구도를 유지하면서 화풍이 완전히 바뀌는 지점이었다.
    #
    # 카탈로그 defaults 에 두지 않는 이유: model_registry.validate_entry 가 defaults 를
    # durationSeconds/fps/dimension 세 키로 깎아내므로 그 밖의 값은 어댑터까지 오지 않는다
    # (style-guide 의 defaults.fidelity 도 실제로는 쓰이지 않고 어댑터 상수가 쓰인다).
    DEFAULT_CONTROL_STRENGTH = 0.6

    def build_request(self, model: dict, request: dict) -> dict:
        prompt = (request.get("prompt") or "").strip()
        if not prompt:
            raise ProviderError("이미지 생성 프롬프트가 필요합니다.")
        image = request.get("imageBase64")
        if not image:
            raise ProviderError("이 모델은 구도를 가져올 원본 이미지가 필요합니다.")

        raw_strength = request.get("controlStrength")
        if raw_strength is None:
            raw_strength = self.DEFAULT_CONTROL_STRENGTH
        try:
            strength = float(raw_strength)
        except (TypeError, ValueError):
            raise ProviderError("controlStrength는 0~1 사이 숫자여야 합니다.")
        if not 0 <= strength <= 1:
            raise ProviderError("controlStrength는 0~1 사이여야 합니다.")

        payload: dict[str, Any] = {
            "image": image,
            "prompt": prompt[:9000],
            "control_strength": strength,
            "output_format": "png",
        }
        defaults = model.get("defaults", {})
        negative = (request.get("negativePrompt") or defaults.get("negativePrompt") or "").strip()
        if negative:
            payload["negative_prompt"] = negative[:9000]
        seed = request.get("seed")
        if seed is not None:
            payload["seed"] = int(seed)
        return payload


class TemplateAdapter(BaseAdapter):
    """Config-driven adapter: the catalog entry carries the request body."""

    key = "template"

    def __init__(self, invocation: str = "async"):
        self.invocation = invocation

    def build_request(self, model: dict, request: dict) -> dict:
        template = (model.get("request") or {}).get("template")
        if not isinstance(template, dict):
            raise ProviderError(
                f"모델 '{model.get('id')}'에 request.template 정의가 없습니다. 관리자 설정을 확인해 주세요."
            )
        prompt = (request.get("prompt") or "").strip()
        if not prompt:
            raise ProviderError("영상 생성 프롬프트가 필요합니다.")
        defaults = model.get("defaults", {})
        values = {
            "prompt": prompt[:4000],
            "durationSeconds": int(request.get("durationSeconds") or defaults.get("durationSeconds", 6)),
            "fps": int(defaults.get("fps", 24)),
            "dimension": str(defaults.get("dimension", "1280x720")),
            "resolution": str(request.get("resolution") or defaults.get("resolution", "720p")),
            "aspectRatio": str(request.get("aspectRatio") or defaults.get("aspectRatio", "16:9")),
            "negativePrompt": request.get("negativePrompt") or defaults.get("negativePrompt"),
            "seed": request.get("seed"),
            "imageBase64": request.get("imageBase64"),
            "imageFormat": request.get("imageFormat", "png"),
        }
        return _substitute(copy.deepcopy(template), values)


_ADAPTERS: dict[str, BaseAdapter] = {
    LumaRayAdapter.key: LumaRayAdapter(),
    StabilityImageAdapter.key: StabilityImageAdapter(),
    StabilityStyleGuideAdapter.key: StabilityStyleGuideAdapter(),
    StabilityControlStructureAdapter.key: StabilityControlStructureAdapter(),
}


def resolve(model: dict) -> BaseAdapter:
    provider = str(model.get("provider") or "").strip()
    adapter = _ADAPTERS.get(provider)
    if adapter:
        return adapter
    # Unknown provider is still usable when the admin supplied a request template.
    if (model.get("request") or {}).get("template"):
        return TemplateAdapter(invocation=model.get("invocation", "async"))
    raise ProviderError(
        f"'{provider}' 어댑터를 찾을 수 없습니다. 어댑터를 추가하거나 request.template을 등록해 주세요."
    )


def known_providers() -> list[str]:
    return sorted(_ADAPTERS.keys()) + ["template"]
