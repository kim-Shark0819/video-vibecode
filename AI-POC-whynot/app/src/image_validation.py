"""생성 모델이 돌려준 이미지 바이트의 최종 검증.

Bedrock 호출이 200이어도 응답의 ``images`` 값이 실제 이미지라는 보장은 호출부가 해야
한다. 예전 경로는 base64 decode만 하고 무조건 ``image/png``로 S3에 저장했다. 잘린 바이트,
다른 형식, 0×0 이미지가 성공 자산으로 등록될 수 있었다(P0-2).

이 모듈은 모델을 호출하지 않는 순수 경계다. 유효한 이미지는 PNG로 정규화해 저장 형식과
Content-Type을 일치시킨다.
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import asdict, dataclass

from PIL import Image, UnidentifiedImageError

MAX_GENERATED_IMAGE_BYTES = 25 * 1024 * 1024
MAX_GENERATED_EDGE = 8192


class GeneratedImageError(ValueError):
    """모델 응답이 저장 가능한 이미지가 아닐 때."""


@dataclass(frozen=True)
class GeneratedImage:
    data: bytes
    width: int
    height: int
    source_format: str
    sha256: str

    def metadata(self) -> dict:
        result = asdict(self)
        result.pop("data", None)
        result["format"] = "png"
        result["sizeBytes"] = len(self.data)
        return result


def validate_generated_image(raw: bytes) -> GeneratedImage:
    """완전 디코딩 후 PNG로 정규화한다.

    ``Image.verify()``만으로는 헤더 뒤에 잘린 픽셀 데이터를 놓칠 수 있어 새로 연 뒤
    ``load()``까지 수행한다. 애니메이션이면 첫 프레임만 사용한다 — 생성 자산 계약은
    단일 기준 이미지다.
    """
    if not isinstance(raw, (bytes, bytearray)) or not raw:
        raise GeneratedImageError("이미지 모델 응답이 비어 있습니다.")
    if len(raw) > MAX_GENERATED_IMAGE_BYTES:
        raise GeneratedImageError(
            f"생성 이미지가 {MAX_GENERATED_IMAGE_BYTES // (1024 * 1024)}MB 제한을 넘었습니다."
        )

    source = bytes(raw)
    try:
        with Image.open(io.BytesIO(source)) as probe:
            source_format = str(probe.format or "").upper()
            probe.verify()
        with Image.open(io.BytesIO(source)) as decoded:
            decoded.seek(0)
            decoded.load()
            width, height = decoded.size
            if width < 1 or height < 1:
                raise GeneratedImageError("생성 이미지의 크기가 올바르지 않습니다.")
            if max(width, height) > MAX_GENERATED_EDGE:
                raise GeneratedImageError(
                    f"생성 이미지의 한 변이 {MAX_GENERATED_EDGE}px 제한을 넘었습니다."
                )
            # 투명도는 보존한다. 그 외 모드는 브라우저·ffmpeg가 안정적으로 읽는 RGB로 맞춘다.
            image = decoded.convert("RGBA" if "A" in decoded.getbands() else "RGB")
            output = io.BytesIO()
            image.save(output, format="PNG", optimize=True)
            png = output.getvalue()
    except GeneratedImageError:
        raise
    except (UnidentifiedImageError, OSError, ValueError) as error:
        raise GeneratedImageError("이미지 모델 응답을 완전히 디코딩하지 못했습니다.") from error

    if not png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise GeneratedImageError("생성 이미지를 PNG로 정규화하지 못했습니다.")
    return GeneratedImage(
        data=png,
        width=width,
        height=height,
        source_format=source_format or "UNKNOWN",
        sha256=hashlib.sha256(png).hexdigest(),
    )
