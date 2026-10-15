"""비전 분석가 — 이미지 바이트를 받는 유일한 역할.

## 왜 장당 1콜인가

`image_brief.py` 는 여러 장을 **한 번에** 넣는다(Bedrock Converse 의 요청당 이미지 상한
20장이 그 설계 근거다). 이쪽은 장당 1콜이고 이유가 다르다.

1. **캐시.** 지시서 1절이 "sha256+model 로 캐시"를 요구한다. 여러 장을 한 요청에 넣으면
   조합이 달라질 때마다 캐시가 빗나간다. 10장 중 1장만 바꿔도 전부 다시 분석해야 한다.
   장당 1콜이면 바뀐 1장만 다시 분석한다.
2. **병렬.** 10장을 순차로 하면 장당 3~5초가 그대로 쌓여 40초가 된다. 독립된 호출이라
   함께 보내는 것이 맞다.
3. **부분 실패.** 한 장이 안전 필터에 걸려도 나머지 9장의 분석은 살아남는다.
   한 요청에 묶으면 전부 잃는다.

## 이미지 크기

호출부(server)가 리사이즈해서 넘긴다(지시서 7절: 긴 변 1568px, ≤5MB).
1568px 은 Claude 비전이 이미지를 내부적으로 축소하는 상한이라, 그보다 크게 보내면
토큰만 늘고 정보는 늘지 않는다.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from .. import prompts_story, story_context, story_models
from . import _json

logger = logging.getLogger("toon-change-ai-pj-app.story.vision")

# Converse 가 받는 이미지 포맷. 이 밖의 값을 보내면 ValidationException 이다.
_FORMATS = {"png", "jpeg", "gif", "webp"}

# 병렬 상한. 이미지 10장을 한꺼번에 보내면 Bedrock 이 ThrottlingException 을 준다.
# 4면 10장이 3배치로 끝나 체감 지연이 충분히 짧다.
_MAX_WORKERS = 4


def _one(caller, image: dict, story_type: str, summary: str) -> dict:
    """이미지 한 장 분석. 실패하면 빈 분석을 돌려준다(생성을 막지 않는다)."""
    fmt = (image.get("format") or "png").lower()
    if fmt == "jpg":
        fmt = "jpeg"
    if fmt not in _FORMATS:
        fmt = "png"

    name = (image.get("name") or "").strip() or "(파일 이름 없음)"
    header = [
        f"[원문 타입] {story_context.STORY_TYPE_LABELS.get(story_type, story_type)}",
        f"[파일 이름] {name}",
    ]
    if summary.strip():
        # 요약 스토리를 주는 이유: 같은 그림이라도 어떤 이야기의 자료인지 알면
        # setting 과 usable_as 판단이 정확해진다.
        header.append(f"[이 작품의 요약] {summary.strip()[:story_context.SUMMARY_MAX_CHARS]}")

    content = [
        {"text": "\n".join(header)},
        {"image": {"format": fmt, "source": {"bytes": image["bytes"]}}},
    ]

    spec = story_models.spec(story_models.ROLE_VISION)
    try:
        payload, _meta = _json.call_json(
            caller,
            prompts_story.ANALYZE_IMAGE,
            content,
            spec["max_tokens"],
            spec["temperature"],
            "analyze_image",
        )
    except _json.RoleError as error:
        # 분석 실패가 전체를 막아서는 안 된다. 빈 분석으로 내려가면 바이블은 원문만 본다.
        logger.warning("이미지 분석 실패 name=%s: %s", name, error)
        return _empty(image, note=str(error)[:200])
    except Exception as error:  # noqa: BLE001 - 안전 필터 거부 등 어떤 오류도 전체를 막지 않는다
        logger.warning("이미지 분석에서 예상치 못한 오류 name=%s: %s", name, error)
        return _empty(image, note=str(error)[:200])

    return normalize(payload, image)


def _empty(image: dict, note: str = "") -> dict:
    return {
        "asset_id": image.get("key") or "",
        "sha256": image.get("sha256") or "",
        "name": (image.get("name") or "").strip(),
        "image_kind": "other",
        "characters": [],
        "setting": "",
        "mood_lighting": "",
        "art_style": "",
        "text_in_image": "",
        "usable_as": [],
        "safety_flags": [],
        "degraded": True,
        "note": note,
        "edited_by_user": False,
    }


_KINDS = {"webtoon_cut", "character_sheet", "concept_art", "photo", "document", "other"}
_USABLE = {"character", "setting", "mood", "plot", "style"}
_FLAGS = {"minor", "nudity", "violence", "real_person"}


def normalize(payload: dict, image: dict) -> dict:
    """모델 응답을 저장·화면이 바로 쓸 수 있는 형태로 다듬는다.

    모델이 목록에 없는 값을 넣는 경우가 있어 화이트리스트로 거른다. 거르지 않으면
    화면이 모르는 image_kind 를 받아 라벨을 못 찾고, 바이블 입력에도 쓰레기가 섞인다.
    """
    kind = str(payload.get("image_kind") or "").strip()
    if kind not in _KINDS:
        kind = "other"

    characters = []
    for item in payload.get("characters") or []:
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

    return {
        "asset_id": image.get("key") or "",
        "sha256": image.get("sha256") or "",
        "name": (image.get("name") or "").strip(),
        "image_kind": kind,
        "characters": characters[:8],
        "setting": str(payload.get("setting") or "").strip()[:300],
        "mood_lighting": str(payload.get("mood_lighting") or "").strip()[:300],
        "art_style": str(payload.get("art_style") or "").strip()[:300],
        # 말풍선 전사는 길 수 있다. 웹툰 컷 하나에 대사가 여러 개다.
        "text_in_image": str(payload.get("text_in_image") or "").strip()[:2000],
        "usable_as": [v for v in (payload.get("usable_as") or []) if v in _USABLE],
        "safety_flags": [v for v in (payload.get("safety_flags") or []) if v in _FLAGS],
        "degraded": False,
        "note": "",
        # 기획자가 화면에서 고쳤는지. 고친 분석은 바이블 재생성의 근거가 되고
        # 다시 분석해서 덮어쓰지 않는다(지시서 9절 수용 기준).
        "edited_by_user": False,
    }


def analyze_many(caller, images: list[dict], story_type: str, summary: str = "") -> list[dict]:
    """여러 장을 병렬로 분석한다. 입력 순서를 유지해 돌려준다.

    Args:
        caller: (system, content, max_tokens, temperature) -> (text, truncated)
        images: [{"key", "sha256", "name", "bytes", "format"}] — 캐시 히트한 것은
                호출부가 미리 걸러서 넘긴다. 이 함수는 받은 것을 전부 분석한다.
    """
    usable = [item for item in images if item.get("bytes")]
    if not usable:
        return []

    if len(usable) == 1:
        return [_one(caller, usable[0], story_type, summary)]

    workers = min(_MAX_WORKERS, len(usable))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_one, caller, item, story_type, summary) for item in usable]
        results = []
        for index, future in enumerate(futures):
            try:
                results.append(future.result())
            except Exception as error:  # noqa: BLE001
                logger.warning("이미지 분석 스레드가 실패했습니다: %s", error)
                results.append(_empty(usable[index], note=str(error)[:200]))
    return results


def safety_blocked(analyses: list[dict]) -> list[str]:
    """안전 플래그가 붙은 이미지 이름.

    지시서 2절이 Rekognition 을 안전 계층으로 지정했지만 이 계정에 그 배선이 없다
    (사전 스크리닝이 전혀 없다). 그 전까지는 비전 분석가가 본 것이 유일한 신호다.
    `minor` 는 특히 중요하다 — 아동으로 보이는 인물이 있으면 이미지 생성으로 넘기지 않는다.
    """
    blocked = []
    for item in analyses:
        flags = set(item.get("safety_flags") or [])
        if flags & {"minor", "nudity"}:
            blocked.append(item.get("name") or item.get("asset_id") or "(이름 없음)")
    return blocked
