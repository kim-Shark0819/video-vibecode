"""웹툰 컷의 화풍을 바꿔 영상용 첫 프레임을 만든다 (2단계 파이프라인의 1단계).

    웹툰 컷 -> [여기: 화풍 교체] -> 스틸 -> [Wan 2.2 I2V] -> 영상

## 왜 이 단계가 필요한가

Wan I2V 는 첫 프레임을 유지하면서 움직임만 만든다. 그래서 **영상 품질의 상한이 첫
프레임으로 정해진다.** 웹툰 컷을 그대로 넣으면 결과는 웹툰이 움직이는 영상이 된다.
드라마 영상화가 목적이면 첫 프레임이 실사 스틸이어야 한다.

## 실측으로 확인한 이 모델의 성질

`stable-image-control-structure` 는 입력 이미지를 **구조 조건**으로만 쓴다. 같은 입력에
프롬프트만 바꿔 실사본과 웹툰본을 각각 뽑아 확인했다.

유지된 것: 구도, 인물 위치, 포즈, 시선 방향, 간판 배치, 원근
**프롬프트가 새로 만든 것: 색, 시간대, 날씨, 머리색, 의상 디테일**

웹툰본에서 밤이 낮이 되고 검은 머리가 금발이 됐다. 실사본에서는 후드가 벗겨졌다.
프롬프트에 "밤, 비, 검은 머리"를 적지 않았기 때문이다.

**그래서 이 모듈의 핵심 일은 화풍 문장을 고르는 것이 아니라, 인물·장면 정보를 프롬프트에
빠짐없이 싣는 것이다.** 화풍만 바꾸면 될 것 같지만 그렇지 않다.

정보의 출처는 이미 있다. `image_brief.analyze` 가 이미지마다 영어 한 줄 묘사
(`englishHint`)를 만들고, 확인 질문의 답변(`character`, `scene` 주제)이 인물 나이대·표정·
의상·시간대·날씨를 담는다. 그것을 모아 공통 블록으로 만든다. 콘티·롱테이크가 인물·장소
블록을 한 번 받아 모든 컷에 같은 문장으로 끼워 넣는 것과 같은 방식이다.

## 모드

| 모드 | 변환 | 쓰는 곳 |
| --- | --- | --- |
| `keep` | 안 함 | 웹툰 원작을 그대로 살릴 때. **기존 동작이고 기본값이다** |
| `photoreal` | 함 | 드라마 스틸. 실사 배우처럼 |
| `webtoon` | 함 | 원본이 사진이거나 다른 화풍일 때 웹툰으로 통일 |
| `custom` | 함 | 프리셋을 직접 고르거나 AI 판단에 맡김 |

`keep` 을 기본값으로 두는 이유: 이 기능이 들어와도 기존 사용자의 결과가 바뀌지 않아야 한다.
변환은 이미지 호출 비용과 대기 시간이 붙고, 원작 보존이 목적인 사용자에게는 손해다.

## 이 모듈이 다른 모듈을 부르지 않는 이유

`styles` 만 임포트한다. `server` 를 임포트하면 순환이 되고, 한국어->영어 변환은 호출부가
`translate` 콜러블로 넘긴다(server.to_visual_english). 테스트에서 네트워크 없이 검사할 수
있어야 하기 때문이기도 하다.
"""

from __future__ import annotations

import logging

from . import styles

logger = logging.getLogger("toon-change-ai-pj-app.toon_convert")

MODE_KEEP = "keep"
MODE_PHOTOREAL = "photoreal"
MODE_WEBTOON = "webtoon"
MODE_CUSTOM = "custom"

# control_strength 권장값이 모드마다 다른 이유.
#
# 이 값은 **구조를 얼마나 강하게 붙잡을지** 다. 높이면 구도가 굳고 프롬프트의 자유가 줄어든다.
#
#   photoreal 0.50 : 웹툰은 눈이 크고 비율이 과장돼 있다. 구조를 너무 강하게 붙잡으면
#                    실사 질감에 웹툰 비율이 얹혀 어색해진다. 조금 풀어 준다.
#   webtoon   0.70 : 같은 시각 언어 안에서 다시 그리는 것이라 구도를 더 지켜도 문제가 없고,
#                    지켜야 원본 컷과 대응이 된다.
#   custom    0.60 : 어느 화풍으로 갈지 모르므로 중간값.
MODES: list[dict] = [
    {
        "id": MODE_KEEP,
        "label": "원본 그대로",
        "hint": "올린 컷을 그대로 첫 프레임으로 씁니다. 변환하지 않습니다.",
        "converts": False,
        "presetId": "",
        "strength": None,
    },
    {
        "id": MODE_PHOTOREAL,
        "label": "실사 변환",
        "hint": "구도를 유지하면서 실사 드라마 스틸로 바꿉니다.",
        "converts": True,
        "presetId": "cinematic-real",
        "strength": 0.50,
    },
    {
        "id": MODE_WEBTOON,
        "label": "웹툰 화풍",
        "hint": "구도를 유지하면서 웹툰 그림체로 다시 그립니다.",
        "converts": True,
        "presetId": "webtoon-color",
        "strength": 0.70,
    },
    {
        "id": MODE_CUSTOM,
        "label": "직접 지정",
        "hint": "그림체를 직접 고르거나 적으면 그대로 따릅니다.",
        "converts": True,
        "presetId": "",
        "strength": 0.60,
    },
]

_MODES_BY_ID = {mode["id"]: mode for mode in MODES}
DEFAULT_MODE = MODE_KEEP

# control_strength 허용 범위. 화면에서 조절하게 하되 극단값은 막는다.
# 0 에 가까우면 구조 조건이 사실상 사라져 원본과 무관한 그림이 나오고,
# 1 이면 화풍이 거의 바뀌지 않아 이 단계를 쓸 이유가 없어진다.
MIN_STRENGTH = 0.2
MAX_STRENGTH = 0.9


def describe() -> dict:
    """화면이 모드 선택 UI 를 그릴 때 쓴다."""
    return {
        "modes": [
            {
                "id": mode["id"],
                "label": mode["label"],
                "hint": mode["hint"],
                "converts": mode["converts"],
                "strength": mode["strength"],
            }
            for mode in MODES
        ],
        "defaultMode": DEFAULT_MODE,
        "strengthRange": {"min": MIN_STRENGTH, "max": MAX_STRENGTH},
    }


def get_mode(mode_id: str) -> dict:
    """모드를 찾는다. 모르는 값은 기본 모드로 내려간다.

    예외를 던지지 않는 이유: 화면이 새 모드를 보내는데 서버가 아직 옛 버전인 경우
    영상 생성 자체가 막히는 것보다 변환을 건너뛰는 편이 낫다. 어떤 값이 왔는지는 로그에 남는다.
    """
    mode = _MODES_BY_ID.get((mode_id or "").strip())
    if mode:
        return mode
    if mode_id:
        logger.info("알 수 없는 변환 모드 '%s' -> %s 로 처리합니다.", mode_id, DEFAULT_MODE)
    return _MODES_BY_ID[DEFAULT_MODE]


def clamp_strength(value, mode: dict) -> float:
    """control_strength 를 허용 범위로 자른다. 값이 없으면 모드 권장값."""
    if value is None or value == "":
        return float(mode.get("strength") or 0.6)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(mode.get("strength") or 0.6)
    return max(MIN_STRENGTH, min(MAX_STRENGTH, number))


def _answer_lines(answers: list[dict] | None) -> list[str]:
    """확인 질문 답변에서 프롬프트에 실을 문장을 뽑는다.

    답변은 한국어다. 여기서는 원문을 모아 두고 호출부의 translate 가 한 번에 옮긴다.
    질문마다 따로 번역하면 호출 수가 답변 수만큼 늘어난다.
    """
    lines = []
    for item in answers or []:
        answer = (item.get("answer") or "").strip()
        if not answer:
            continue
        question = (item.get("question") or "").strip()
        lines.append(f"{question} {answer}".strip() if question else answer)
    return lines[:10]


def scene_source(brief: dict | None, answers: list[dict] | None, primary_index: int) -> tuple[str, str]:
    """프롬프트에 실을 장면 정보를 모은다.

    Returns:
        (영어로 이미 준비된 부분, 번역이 필요한 한국어 부분)

    영어 부분은 `image_brief` 가 이미지마다 만들어 둔 `englishHint` 다. 첫 프레임으로 쓰는
    이미지의 것을 먼저 놓는다. 그것이 이 장면 자체의 묘사이기 때문이다.

    한국어 부분은 확인 질문 답변과 분석 요약이다. 이것이 인물 나이대·표정·의상·시간대·
    날씨를 담고 있고, **이 정보가 없으면 모델이 지어낸다**(실측: 밤 -> 낮, 검은머리 -> 금발).

    primary_index 는 1부터다. `image_brief.prompt_context` 와 같은 규약을 쓴다.
    """
    english_parts: list[str] = []
    korean_parts: list[str] = []

    images = (brief or {}).get("images") or []
    primary = None
    for item in images:
        if not isinstance(item, dict):
            continue
        if int(item.get("index") or 0) == primary_index:
            primary = item
            break
    if primary is None and images:
        primary = images[0] if isinstance(images[0], dict) else None

    if primary:
        hint = (primary.get("englishHint") or "").strip()
        if hint:
            english_parts.append(hint)
        description = (primary.get("description") or "").strip()
        if description:
            korean_parts.append(description)

    # 나머지 이미지는 캐릭터 시트나 컨셉 아트다. 인물 외형의 근거가 되므로 함께 싣는다.
    # 다만 장면 묘사가 아니라 참고이므로 뒤에 둔다.
    for item in images:
        if not isinstance(item, dict) or item is primary:
            continue
        hint = (item.get("englishHint") or "").strip()
        if hint:
            english_parts.append(hint)

    korean_parts.extend(_answer_lines(answers))

    return " ".join(english_parts)[:1200], " ".join(korean_parts)[:2000]


def build_scene_english(
    brief: dict | None,
    answers: list[dict] | None,
    primary_index: int,
    user_text: str = "",
    translate=None,
) -> str:
    """장면·인물 정보를 영어 한 덩어리로 만든다.

    translate 는 (한국어, ) -> 영어 콜러블이다(server.to_visual_english 를 부분 적용해 넘긴다).
    없거나 실패하면 영어 부분만 쓴다. 번역 실패가 변환을 막을 이유는 없다.
    """
    english, korean = scene_source(brief, answers, primary_index)
    extra_korean = (user_text or "").strip()
    if extra_korean:
        korean = f"{korean} {extra_korean}".strip()

    if korean and translate:
        try:
            translated = (translate(korean) or "").strip()
        except Exception as error:  # noqa: BLE001 - 번역 실패가 변환을 막아서는 안 된다
            logger.warning("장면 설명 번역에 실패해 영어 부분만 사용합니다: %s", error)
            translated = ""
        if translated:
            english = f"{english} {translated}".strip() if english else translated

    return english[:2000]


def build_request(
    mode_id: str,
    *,
    scene_english: str,
    preset_id: str = "",
    style_extra: str = "",
    strength=None,
) -> dict:
    """변환 요청에 넣을 프롬프트·부정 프롬프트·강도를 만든다.

    Returns:
        {mode, converts, presetId, presetLabel, prompt, negativePrompt, controlStrength}

    `converts` 가 False 면 나머지 값은 쓰이지 않는다. 호출부가 그 경우 원본을 그대로 쓴다.
    """
    mode = get_mode(mode_id)
    result = {
        "mode": mode["id"],
        "modeLabel": mode["label"],
        "converts": mode["converts"],
        "presetId": "",
        "presetLabel": "",
        "prompt": "",
        "negativePrompt": "",
        "controlStrength": clamp_strength(strength, mode),
    }
    if not mode["converts"]:
        return result

    # 모드가 프리셋을 정해 두면 그것을 쓴다. custom 은 호출부가 고른 프리셋을 받는다.
    chosen = (mode["presetId"] or preset_id or styles.DEFAULT_PRESET_ID).strip()
    preset = styles.get_preset(chosen)

    detail = preset["prompt"]
    extra = (style_extra or "").strip()
    if extra:
        detail = f"{detail}, {extra[:300]}"

    # styles.compose_prompt 와 같은 조립 규칙을 쓴다. **매체를 문장 맨 앞에 둔다.**
    # 장면을 앞에 두고 화풍을 뒤에 붙이면 확산 모델이 화풍 지시를 약하게 받는다
    # (이 프로젝트에서 러프 콘티가 사실적인 사진으로 나온 원인이 그것이었다).
    decision = {"lead": preset["lead"], "stylePrompt": detail}
    result["prompt"] = styles.compose_prompt(scene_english, decision)
    result["negativePrompt"] = ", ".join(
        part for part in [preset["negative"], styles.BASE_NEGATIVE] if part
    )
    result["presetId"] = preset["id"]
    result["presetLabel"] = preset["label"]
    return result
