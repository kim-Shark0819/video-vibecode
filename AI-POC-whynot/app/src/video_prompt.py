"""영상 모델에 보낼 프롬프트를 만든다.

사용자는 한국어로 짧게 적는다("천천히 줌인"). 그 문장을 그대로 Luma에 보내면 결과가 어색하다.
그래서 백엔드가 프롬프트를 다시 쓴다. 사용자가 프롬프트 기법을 배워야 하는 구조를 만들지 않는다.

## 왜 다시 쓰는가 (실제로 어색함을 만든 원인)

이전 구현은 "이미지가 있으면 그 안의 피사체·의상·배경·화풍을 묘사하라"고 지시했다.
이미지→영상에서는 이것이 정반대다. 첫 프레임에 이미 인물과 배경이 들어 있는데 프롬프트가
그것을 다시 말하면, 모델은 두 개의 서로 다른 지시를 받는다. 문장이 이미지와 조금이라도 어긋나면
그 차이를 애니메이션으로 메우려 하면서 얼굴이 뭉개지고 옷이 바뀐다.

그래서 두 갈래로 나눈다.

| 입력 | 프롬프트가 담는 것 |
| --- | --- |
| 첫 프레임 이미지 있음 | **변화만.** 무엇이 움직이고, 무엇이 고정되는지 |
| 텍스트만 | **전부.** 숏 크기, 피사체, 동작, 장소, 카메라, 조명, 화풍 |

## 나머지 규칙의 근거

- **카메라 움직임은 하나.** 두 개를 섞으면 어느 쪽도 제대로 나오지 않는다. 필름 용어
  (dolly, pan, tilt, orbit, crane, rack focus)를 쓴다. Ray2는 이 어휘로 학습되어 있다.
- **움직임은 작게.** 동작이 크면 모델이 보이지 않는 신체 부위를 상상해 그리면서 팔다리가 늘어난다.
- **한 컷은 하나의 연속된 동작.** 5~9초 안에 사건을 두 개 넣으면 장면이 갑자기 전환되며 뭉개진다.
- **부정문 금지.** Luma에는 negative prompt가 없다. "쓰레기 없는 거리"라고 쓰면 쓰레기가 나온다.
  그래서 모델 출력에서 부정문을 걷어내는 검사를 마지막에 한 번 더 돌린다.
- **고정할 것을 말한다.** "무엇이 변하지 않는지"를 적으면 인물이 다른 사람으로 변하는 일이 줄어든다.

이 판단은 전부 서버에서 한다. 사용자 입력창에는 아무 규칙도 요구하지 않는다.
"""

from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# 카메라 움직임 어휘
# --------------------------------------------------------------------------- #
# 프롬프트에 실제로 들어가는 문구. 이 목록을 지시문에도 그대로 보여준다.
# 크고 빠른 움직임(whip pan, crash zoom 등)은 넣지 않았다. 5~9초 클립에서 형태가 무너진다.
CAMERA_MOVES: tuple[str, ...] = (
    "a static locked-off shot",
    "a slow dolly in",
    "a slow dolly out",
    "a gentle pan left",
    "a gentle pan right",
    "a slow tilt up",
    "a slow tilt down",
    "a slow orbit around the subject",
    "a slow crane up",
    "a subtle handheld drift",
    "a slow tracking shot following the subject",
    "a rack focus onto the subject",
)

DEFAULT_CAMERA_MOVE = "a slow dolly in"

# 모델은 "camera slowly pushes in toward her face"처럼 자유롭게 쓴다. 종류와 방향을 따로 읽어
# 조합해야 "sweep to the right"처럼 어휘가 어긋난 표현도 제대로 붙는다.
# 순서가 중요하다. 위에 있는 종류가 먼저 걸린다(rack focus가 focus보다, tilt가 pan보다 앞).
_MOVE_TYPES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("static", ("static", "locked", "lock off", "lock-off", "fixed", "tripod", "motionless", "stays still", "completely still", "steady frame")),
    ("rack", ("rack focus", "focus pull", "pull focus", "shift focus", "refocus", "focus shifts")),
    ("handheld", ("handheld", "hand-held", "shaky", "documentary", "organic")),
    ("orbit", ("orbit", "arc around", "circle", "rotate", "revolve", "around the subject", "around him", "around her")),
    ("crane", ("crane", "jib", "boom")),
    ("track", ("track", "follow", "alongside", "walking with", "trails")),
    ("tilt", ("tilt", "looks up", "looks down", "look up", "look down")),
    ("pan", ("pan", "sweep", "swing", "scan across", "whip")),
    ("dolly", ("dolly", "push", "zoom", "closer", "approach", "toward", "towards", "forward", "pull", "back away", "wider", "reveal", "recede")),
)

# 숏 크기도 하나로 고정한다. 모델이 "wide shot gradually becoming a medium shot"처럼 전환을
# 적어 오는 경우가 있는데, 5~9초 클립에서 구도가 바뀌면 화면이 뭉개진다.
SHOT_TYPES: tuple[str, ...] = (
    "Wide establishing shot",
    "Wide shot",
    "Medium shot",
    "Medium close-up",
    "Close-up",
    "Over-the-shoulder shot",
)

# 좁은 것부터 본다. "medium close-up"이 "medium"이나 "close-up"보다 먼저 걸려야 한다.
_SHOT_TYPE_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Over-the-shoulder shot", ("over-the-shoulder", "over the shoulder", "osh")),
    ("Medium close-up", ("medium close", "mcu")),
    ("Close-up", ("close-up", "close up", "closeup", "extreme close", "cu ")),
    ("Wide establishing shot", ("establishing", "establisher")),
    ("Wide shot", ("wide", "long shot", "full shot", "full body", "landscape")),
    ("Medium shot", ("medium", "mid shot", "waist")),
)

DEFAULT_SHOT_TYPE = "Medium shot"

_DIRECTION_PATTERNS: dict[str, re.Pattern] = {
    "left": re.compile(r"\bleft\b", re.IGNORECASE),
    "right": re.compile(r"\bright\b", re.IGNORECASE),
    "up": re.compile(r"\b(up|upward|upwards|rise|rises|rising|ascend)\b", re.IGNORECASE),
    "down": re.compile(r"\b(down|downward|downwards|descend|lower)\b", re.IGNORECASE),
    "out": re.compile(r"\b(out|outward|back|backward|away|wider|reveal|reveals|recede|recedes|further)\b", re.IGNORECASE),
    "in": re.compile(r"\b(in|inward|closer|toward|towards|forward|approach|approaches)\b", re.IGNORECASE),
}

# 어떤 프롬프트에도 붙는 기술 절. 전부 긍정문이다.
# "컷 없이"처럼 부정으로 쓰면 Luma가 컷을 만들어낼 수 있어 "하나의 연속된 테이크"로 표현한다.
TECHNICAL_TAIL = (
    "One continuous unbroken take with steady natural motion physics, "
    "consistent lighting from start to finish, cinematic 24fps film look."
)

# Luma는 5000자를 받지만 길어질수록 지시가 서로 희석된다. 가이드들이 공통으로 짧게 쓰라고 한다.
MAX_WORDS = 150
MAX_CHARS = 4500

# 부정 표현. 이 모델에는 negative prompt가 없어서 언급 자체가 등장 확률을 올린다.
_NEGATIVE_PATTERN = re.compile(
    r"\b(no|not|never|without|avoid|avoids|avoiding|don't|do\s+not|doesn't|"
    r"free\s+of|devoid|absent|lacking|nothing|none|neither|nor|exclude|excluding|"
    r"prevent|prevents|refrain|omit|omitting)\b",
    re.IGNORECASE,
)

_MARKDOWN_PATTERN = re.compile(r"[*_`#>\[\]]")


# --------------------------------------------------------------------------- #
# 정규화와 검사
# --------------------------------------------------------------------------- #
def normalize_camera_move(raw: str) -> str:
    """모델이 자유롭게 쓴 카메라 표현을 허용된 문구 하나로 맞춘다.

    그대로 두면 컷마다 표현이 달라지고 두 개를 섞어 쓰기도 한다. 하나로 고정해야 결과가 일정하다.
    종류와 방향을 따로 읽어 조합하므로 "sweep to the right"처럼 어휘가 어긋난 표현도 붙는다.
    """
    text = " ".join((raw or "").split()).lower()
    if not text:
        return DEFAULT_CAMERA_MOVE

    # 이미 허용된 문구를 그대로 썼으면 그것을 쓴다.
    for phrase in CAMERA_MOVES:
        if phrase[2:] in text:  # "a " 를 뗀 나머지
            return phrase

    move_type = ""
    for name, hints in _MOVE_TYPES:
        if any(hint in text for hint in hints):
            move_type = name
            break
    if not move_type:
        return DEFAULT_CAMERA_MOVE

    def has(direction: str) -> bool:
        return bool(_DIRECTION_PATTERNS[direction].search(text))

    if move_type == "static":
        return "a static locked-off shot"
    if move_type == "rack":
        return "a rack focus onto the subject"
    if move_type == "handheld":
        return "a subtle handheld drift"
    if move_type == "orbit":
        return "a slow orbit around the subject"
    if move_type == "crane":
        return "a slow crane up"
    if move_type == "track":
        return "a slow tracking shot following the subject"
    if move_type == "tilt":
        return "a slow tilt down" if has("down") else "a slow tilt up"
    if move_type == "pan":
        return "a gentle pan right" if has("right") else "a gentle pan left"
    # dolly. 방향을 못 읽으면 들어가는 쪽이 안전하다. 빠지는 움직임은 배경이 많이 필요해 왜곡이 잦다.
    return "a slow dolly out" if has("out") and not has("in") else "a slow dolly in"


def normalize_shot_type(raw: str) -> str:
    """숏 크기를 하나로 맞춘다. 전환을 적어 온 경우 넓은 쪽 하나만 남긴다."""
    text = " ".join((raw or "").split()).lower()
    if not text:
        return DEFAULT_SHOT_TYPE
    for canonical, hints in _SHOT_TYPE_HINTS:
        if any(hint in text for hint in hints):
            return canonical
    return DEFAULT_SHOT_TYPE


def _lower_first(text: str) -> str:
    """문장 중간에 끼워 넣을 조각의 첫 글자를 내린다.

    모델은 필드 값을 "A young woman..."처럼 대문자로 시작해서 준다. 그대로 이어 붙이면
    "medium shot of A young woman"이 되어 읽기가 어색하고, 문장 경계로 오해될 수 있다.
    고유명사는 두 번째 글자가 대문자인 경우(예: "McKay")를 제외해 보호한다.
    """
    if not text:
        return text
    if len(text) > 1 and text[1].isupper():
        return text
    return text[0].lower() + text[1:]


def sanitize(text: str) -> str:
    """모델 출력을 그대로 믿지 않고 마지막으로 손질한다.

    지시문에 "부정문을 쓰지 마라"라고 적어도 모델은 종종 쓴다. 프롬프트 한 줄이 결과를 망치고
    그 비용은 그대로 청구되므로, 보내기 직전에 문장 단위로 걸러낸다.

    다만 필터가 과하게 잘라내 내용이 사라지면 원문을 쓴다. 프롬프트가 비는 것이 더 나쁘다.
    """
    cleaned = _MARKDOWN_PATTERN.sub("", text or "")
    cleaned = " ".join(cleaned.split())
    if not cleaned:
        return ""

    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", cleaned) if part.strip()]
    kept = [part for part in sentences if not _NEGATIVE_PATTERN.search(part)]

    # 전부 걸러졌을 때만 원문을 쓴다. 프롬프트가 비는 것이 부정문보다 나쁘다.
    # 조립된 프롬프트는 항상 긍정문인 기술 절을 갖고 있어 실제로 비는 경우는 거의 없다.
    if not kept:
        logger.warning("Every sentence in the video prompt was negative; keeping the original")
        result = cleaned
    else:
        if len(kept) < len(sentences):
            logger.info(
                "Dropped %s negative sentence(s) from video prompt", len(sentences) - len(kept)
            )
        result = " ".join(kept)

    words = result.split()
    if len(words) > MAX_WORDS:
        result = " ".join(words[:MAX_WORDS])
    return result[:MAX_CHARS]


def _sentence(value: object, fallback: str = "") -> str:
    """조각을 한 문장으로 만든다. 첫 글자를 올리고 마침표가 없으면 붙인다.

    모델은 필드 값을 소문자로 시작하는 구절로 준다. 그대로 이어 붙이면 문장 경계가 흐려져
    지시가 서로 섞여 읽힌다.
    """
    text = " ".join(str(value or "").split())
    if not text:
        text = " ".join(str(fallback or "").split())
    if not text:
        return ""
    text = text[0].upper() + text[1:]
    if text[-1] not in ".!?":
        text += "."
    return text


# --------------------------------------------------------------------------- #
# 지시문
# --------------------------------------------------------------------------- #
_MOVES_LIST = ", ".join(CAMERA_MOVES)
_SHOT_TYPES_LIST = ", ".join(SHOT_TYPES)

_IMAGE_INSTRUCTION = f"""You are a cinematographer preparing a single shot for the Luma Ray2 video model.
A Korean drama studio is animating an existing still frame. That frame is attached.

The frame is the first frame of the video. The model already has the character, the clothing,
the set and the art style from it. Your job is ONLY to say how that frame comes to life.

Critical rule: never re-describe what the subject looks like. Do not mention clothing colour,
hair, face, art style or the setting. Every word of appearance you add can disagree with the
frame, and the model resolves that disagreement by warping the character. Look at the image only
to judge what could plausibly move in it.

Fill this JSON. Every value is plain English, no markdown:
{{
  "subjectMotion": "One continuous small action the main subject performs, 6-20 words. If the framing is tight, keep it to a head turn, a breath, a blink, a slow glance.",
  "secondaryMotion": "What else in the frame moves: hair, fabric, steam, rain, dust, water, flickering signage, drifting smoke. 6-20 words.",
  "cameraMove": "Exactly one of: {_MOVES_LIST}",
  "atmosphere": "How light or air evolves across the shot, 6-18 words. For example light slowly warming, shadows lengthening, rain intensifying.",
  "hold": "What must stay identical to the first frame, 5-15 words. For example the character's face, outfit and the background layout stay unchanged."
}}

Rules:
- One action only. Two events in a 5-9 second clip make the shot jump and smear.
- Keep motion small and physically plausible. Large motion makes the model invent limbs.
- Positive statements only. This model has no negative prompt, so naming a thing you do not
  want makes it appear. Write what happens, never what is absent.
- Reply with the JSON object and nothing else."""

_TEXT_INSTRUCTION = f"""You are a cinematographer writing a single shot for the Luma Ray2 video model.
A Korean drama studio turns webtoon stories into live footage. There is no reference frame,
so your description is the only thing the model has.

Fill this JSON. Every value is plain English, no markdown:
{{
  "shotType": "Exactly one framing, chosen from: {_SHOT_TYPES_LIST}. One framing only; a shot that changes size mid-clip smears.",
  "subject": "The main subject with concrete visible detail: age band, clothing, distinguishing features. 8-25 words. Use 'young woman' or 'man in his forties' rather than a number.",
  "action": "One continuous action with intensity, 6-20 words. Prefer a precise verb: strides, hesitates, grips, exhales.",
  "setting": "Place, time of day and weather, 6-20 words.",
  "cameraMove": "Exactly one of: {_MOVES_LIST}",
  "lighting": "Where the light comes from and its quality, 5-18 words. For example low winter sun raking through blinds, cool neon spill from a shopfront.",
  "styleClause": "Rendering look in 4-12 words, for example photorealistic cinematic footage, shallow depth of field, cinematic colour grading."
}}

Rules:
- One subject, one action, one camera move. A 5-9 second clip holds exactly one beat.
- Concrete visual nouns rather than taste words. 'Beautiful' and 'amazing' give the model nothing.
- Keep motion small and physically plausible so the frame stays stable.
- Positive statements only. This model has no negative prompt, so naming a thing you do not
  want makes it appear. Write what happens, never what is absent.
- Reply with the JSON object and nothing else."""


def _extract_json(text: str) -> dict:
    """모델 응답에서 JSON 객체를 꺼낸다. 앞뒤 설명이 붙어 오는 경우가 있다."""
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("프롬프트 응답에서 JSON을 찾지 못했습니다.")
        parsed = json.loads(raw[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("프롬프트 응답이 객체가 아닙니다.")
    return parsed


# --------------------------------------------------------------------------- #
# 조립
# --------------------------------------------------------------------------- #
def _assemble_image_prompt(fields: dict, style_clause: str) -> tuple[str, str]:
    """이미지가 있을 때. 움직임을 앞에 두고 고정할 것을 뒤에 둔다."""
    move = normalize_camera_move(str(fields.get("cameraMove") or ""))
    parts = [
        _sentence(fields.get("subjectMotion"), "The subject breathes and shifts weight almost imperceptibly."),
        _sentence(fields.get("secondaryMotion")),
        _sentence(f"The camera holds {move}"),
        _sentence(fields.get("atmosphere")),
        _sentence(
            fields.get("hold"),
            "The subject's face, outfit and the background layout stay exactly as in the opening frame.",
        ),
    ]
    if style_clause:
        parts.append(_sentence(style_clause))
    parts.append(TECHNICAL_TAIL)
    return " ".join(part for part in parts if part), move


def _assemble_text_prompt(fields: dict, style_clause: str) -> tuple[str, str]:
    """텍스트만일 때. 숏 크기와 피사체를 앞에 두어 구도를 먼저 정한다."""
    move = normalize_camera_move(str(fields.get("cameraMove") or ""))
    shot_type = normalize_shot_type(str(fields.get("shotType") or ""))
    subject = _lower_first(" ".join(str(fields.get("subject") or "a solitary figure").split()))
    setting = _lower_first(" ".join(str(fields.get("setting") or "").split()))

    opening = f"{shot_type} of {subject.rstrip('.')}"
    if setting:
        opening += f", {setting.rstrip('.')}"
    parts = [
        _sentence(opening),
        _sentence(fields.get("action"), "The subject stands still and slowly turns to face the camera."),
        _sentence(f"The camera holds {move}"),
        _sentence(fields.get("lighting")),
    ]
    # 사용자가 그림체를 적었으면 모델이 쓴 문구보다 그것을 우선한다.
    parts.append(_sentence(style_clause or fields.get("styleClause") or "Photorealistic cinematic footage, shallow depth of field."))
    parts.append(TECHNICAL_TAIL)
    return " ".join(part for part in parts if part), move


def assemble_shot(fields: dict, style_clause: str = "") -> tuple[str, str]:
    """텍스트만으로 만드는 한 컷을 조립한다. 롱테이크가 컷마다 부른다.

    컷을 이어 붙이려면 인물·장소·조명 문구가 컷마다 **글자 그대로 같아야** 한다.
    그래서 호출부가 공통 블록을 한 번만 받아 모든 컷에 같은 값을 넣는다.
    """
    prompt, move = _assemble_text_prompt(fields, style_clause)
    return sanitize(prompt), move


def _fallback_prompt(user_prompt: str, has_image: bool, style_clause: str) -> str:
    """모델이 JSON을 주지 못했을 때 쓰는 최소 프롬프트.

    생성 자체를 막지 않는다. 사용자 문장을 그대로 붙이고 나머지는 안전한 기본값으로 채운다.
    """
    move = normalize_camera_move(user_prompt)
    if has_image:
        body = (
            f"The subject moves gently and continuously. The camera holds {move}. "
            "The subject's face, outfit and the background layout stay exactly as in the opening frame."
        )
    else:
        body = (
            f"Medium shot of the described scene. The camera holds {move}. "
            "Soft directional light shapes the subject."
        )
    if style_clause:
        body += f" {_sentence(style_clause)}"
    return sanitize(f"{body} {TECHNICAL_TAIL}")


def build(
    converse,
    *,
    image_bytes: bytes | None,
    image_format: str,
    user_prompt: str,
    style_prompt: str = "",
    extra_context: str = "",
) -> dict:
    """영상 프롬프트를 만든다.

    Args:
        converse: (system, content, max_tokens) -> str. gateway를 감싼 호출자.
                  이 모듈은 boto3를 직접 만지지 않는다(사용량 기록을 우회하지 않기 위해).
        image_bytes: 첫 프레임 이미지. 있으면 "변화만" 쓰는 모드로 간다.
        image_format: png/jpeg/gif/webp.
        user_prompt: 사용자가 적은 한국어 지시.
        style_prompt: 그림체 프리셋이 정한 영어 스타일 문구.

    Returns:
        {"prompt": str, "mode": "image"|"text", "cameraMove": str, "fields": dict}
    """
    has_image = bool(image_bytes)
    instruction = _IMAGE_INSTRUCTION if has_image else _TEXT_INSTRUCTION
    style_clause = " ".join((style_prompt or "").split())[:400]

    direction = user_prompt.strip() or (
        "(no direction given: choose one natural, subtle motion that suits this frame)"
        if has_image
        else "(no direction given: choose a simple, cinematic single-subject shot)"
    )
    request_text = (
        "Planner's direction, written in Korean. Translate the intent faithfully:\n"
        f"{direction[:2000]}"
    )
    if (extra_context or "").strip():
        # 참고 이미지 분석과 기획자가 확인해 준 사실. 한국어로 들어온다.
        # 첫 프레임 이미지는 모델이 직접 보므로 여기에는 그 외의 자료만 담겨 있다.
        request_text += (
            "\n\nReference material and facts the planner confirmed. Written in Korean. "
            "Use it to keep the character and art style consistent, but do not introduce "
            "these reference images as new elements in the frame:\n"
            f"{extra_context.strip()[:4000]}"
        )
    if style_clause:
        request_text += (
            "\n\nArt style the studio asked for. Keep it in mind, and put it in styleClause "
            "if the field exists:\n"
            f"{style_clause}"
        )

    content: list[dict] = [{"text": request_text}]
    if has_image:
        vision_format = image_format if image_format in {"png", "jpeg", "gif", "webp"} else "png"
        content.append({"image": {"format": vision_format, "source": {"bytes": image_bytes}}})

    fields: dict = {}
    try:
        raw = converse(instruction, content, 700)
        fields = _extract_json(raw)
    except (ValueError, json.JSONDecodeError) as error:
        # 한 번 더 시도하지 않는다. 같은 지시로 두 번 부르면 비용만 두 배가 되고 결과는 비슷하다.
        logger.warning("Video prompt JSON parse failed, using fallback: %s", error)
        return {
            "prompt": _fallback_prompt(user_prompt, has_image, style_clause),
            "mode": "image" if has_image else "text",
            "cameraMove": normalize_camera_move(user_prompt),
            "fields": {},
            "degraded": True,
        }

    if has_image:
        assembled, move = _assemble_image_prompt(fields, style_clause)
    else:
        assembled, move = _assemble_text_prompt(fields, style_clause)

    prompt = sanitize(assembled)
    if not prompt:
        prompt = _fallback_prompt(user_prompt, has_image, style_clause)

    logger.info(
        "Video prompt built mode=%s camera=%s words=%s",
        "image" if has_image else "text",
        move,
        len(prompt.split()),
    )
    return {
        "prompt": prompt,
        "mode": "image" if has_image else "text",
        "cameraMove": move,
        "fields": fields,
        "degraded": False,
    }


# --------------------------------------------------------------------------- #
# Wan 2.2 I2V-A14B 전용 (자체 GPU 배포)
# --------------------------------------------------------------------------- #
# 지시서 4-2 / 7-2 가 정한 백엔드 프롬프트다. Luma 경로와 분리한 이유:
#
# 1. Wan에는 부정 프롬프트가 있다. 추론 서버 이미지에 고정값으로 구워 넣었다
#    (video-gen/patch_neg_prompt.py). Luma에는 없어서 긍정문만 써야 했는데,
#    그 제약이 Wan에는 필요하지 않다. 다만 프롬프트를 긍정형으로 쓰는 것이
#    여전히 결과가 좋아 규칙은 유지한다.
# 2. Wan은 자유 문장 한 단락을 받는다. Luma 경로처럼 항목을 받아 서버가 조립하지 않고,
#    지시서 7-2가 정한 구조대로 모델이 한 단락을 쓰게 한다.
#    [주체 묘사] + [주체의 동작] + [배경/환경] + [조명과 분위기] + [카메라 워크] + [스타일 키워드]
# 3. 입력이 대본·지문·트리트먼트 같은 **긴 서사형 텍스트**다. 그것을 그대로 모델에 넣지 않고
#    한 장면으로 압축하는 것이 이 단계의 핵심이다(지시서 4-2).
#
# 화풍 유지 문구를 반드시 넣는 것도 지시서 7-2 규칙 2다. 첨부 이미지가 웹툰 컷일 때
# 실사로 변해 버리는 것을 막는다.
# 하드 상한. 이 이상은 서버가 자른다.
WAN_MAX_WORDS = 130
# 모델에게 요구하는 목표 분량. 상한보다 낮게 둔 이유는 화풍 유지 절(약 12단어)을 뒤에
# 붙일 자리를 남겨야 하기 때문이다. 목표를 상한과 같게 두었더니 모델이 130단어를 꽉 채워
# 써서, 문구를 붙일 자리가 없어 화풍 키워드 문장이 잘려 나갔다.
WAN_TARGET_WORDS = 110

_WAN_INSTRUCTION = f"""당신은 영상 생성 모델(Wan 2.2 I2V-A14B)용 프롬프트 엔지니어입니다.
사용자가 제공한 대본·지문·트리트먼트·캐릭터 컨셉과 첨부 이미지를 분석하여,
하나의 장면을 묘사하는 영어 영상 프롬프트 1개를 생성하세요.

규칙:
1. 반드시 다음 순서를 따르세요:
   [주체 묘사] + [주체의 동작] + [배경/환경] + [조명과 분위기] + [카메라 워크] + [스타일 키워드]
2. 첨부 이미지의 화풍(웹툰/일러스트/실사 등)과 캐릭터 외형을 유지하는 표현을 반드시
   포함하세요. 예: "maintaining the original webtoon art style and character design".
   이 문구가 빠지면 웹툰 컷이 실사로 변합니다.
3. 카메라 워크는 한 가지만 지정하세요. 다음 중에서 고릅니다:
   {", ".join(CAMERA_MOVES)}
   두 가지를 섞으면 어느 쪽도 제대로 나오지 않습니다.
4. 조명·분위기 키워드를 포함하세요. 예: soft cinematic lighting, golden hour, moody atmosphere.
5. 5초 내외 클립에서 표현 가능한 **단일 동작**만 묘사하세요.
   장면 전환, 컷 편집, 다중 시퀀스 묘사를 넣지 마세요.
6. 입력이 긴 서사(대본, 여러 씬)여도 **가장 영상으로 만들 가치가 있는 한 순간**만 골라
   묘사하세요. 줄거리를 요약하지 마세요.
7. 동작은 작고 물리적으로 자연스럽게 유지하세요. 동작이 크면 모델이 보이지 않는 신체를
   상상해 그리면서 팔다리가 늘어납니다.
8. 폭력·선정성 등 부적절한 묘사는 순화하여 변환하세요.
9. 무엇이 없는지를 쓰지 말고 무엇이 있는지만 쓰세요.
10. 출력은 영상 프롬프트 텍스트만, {WAN_TARGET_WORDS - 30}~{WAN_TARGET_WORDS}단어의 영어 단문으로
   작성하세요. 설명, 머리말, 코드펜스, 따옴표를 붙이지 마세요."""

# 화풍 유지 문구가 빠졌을 때 붙일 기본 절. 규칙 2가 지켜지지 않는 경우가 있어 검사한다.
_WAN_STYLE_GUARD = "maintaining the original art style and character design of the source image"
_STYLE_GUARD_HINTS = ("art style", "character design", "original style", "source image")


def _wan_fallback(user_prompt: str, style_clause: str) -> str:
    """프롬프트 변환이 실패했을 때 쓰는 최소 프롬프트. 생성을 막지 않는다."""
    move = normalize_camera_move(user_prompt)
    body = (
        "The subject in the frame moves gently and continuously with small natural motion. "
        f"The camera holds {move}. Soft cinematic lighting, calm atmosphere. "
        f"{_WAN_STYLE_GUARD}."
    )
    if style_clause:
        body += f" {_sentence(style_clause)}"
    return sanitize(f"{body} {TECHNICAL_TAIL}")


def build_wan(
    converse,
    *,
    image_bytes: bytes | None,
    image_format: str,
    user_text: str,
    style_prompt: str = "",
    extra_context: str = "",
) -> dict:
    """대본·지문 같은 긴 텍스트와 이미지를 Wan용 영어 프롬프트 한 단락으로 바꾼다.

    Args:
        converse: (system, content, max_tokens) -> str. gateway를 감싼 호출자.
        image_bytes: 첫 프레임 이미지. Wan I2V는 이미지가 필수다.
        image_format: png/jpeg/gif/webp.
        user_text: 사용자가 적은 대본·지문·트리트먼트·캐릭터 컨셉(한국어여도 된다).
        style_prompt: 그림체 프리셋이 정한 영어 스타일 문구.
        extra_context: 참고 이미지 분석과 기획자 답변(image_brief.prompt_context 결과).
                       첫 프레임 외의 이미지들과 확인된 사실이 여기 들어온다.

    Returns:
        {"prompt": str, "cameraMove": str, "degraded": bool}
    """
    style_clause = " ".join((style_prompt or "").split())[:400]
    direction = (user_text or "").strip() or (
        "(별도 지시가 없습니다. 첨부 이미지에서 자연스럽게 일어날 수 있는 작은 움직임 하나를 고르세요.)"
    )

    request_text = (
        "아래는 기획자가 적은 대본·지문·트리트먼트 또는 캐릭터 컨셉입니다. "
        "이 중에서 한 순간을 골라 영상 프롬프트로 옮기세요.\n\n"
        f"{direction[:6000]}"
    )
    if (extra_context or "").strip():
        # 참고 이미지 설명과 기획자가 확인해 준 사실. 첨부된 첫 프레임과 함께 읽어야
        # 인물이 캐릭터 시트의 누구인지 같은 판단이 가능해진다.
        request_text += f"\n\n{extra_context.strip()[:4000]}"
    if style_clause:
        request_text += (
            "\n\n[제작사가 요청한 화풍] 아래 문구를 스타일 키워드에 반영하세요.\n"
            f"{style_clause}"
        )

    content: list[dict] = [{"text": request_text}]
    if image_bytes:
        vision_format = image_format if image_format in {"png", "jpeg", "gif", "webp"} else "png"
        content.append({"image": {"format": vision_format, "source": {"bytes": image_bytes}}})

    try:
        raw = converse(_WAN_INSTRUCTION, content, 900)
    except Exception as error:  # noqa: BLE001 - 변환 실패가 생성을 막아서는 안 된다
        logger.warning("Wan prompt conversion failed, using fallback: %s", error)
        return {
            "prompt": _wan_fallback(user_text, style_clause),
            "cameraMove": normalize_camera_move(user_text),
            "degraded": True,
        }

    prompt = sanitize(raw)
    if not prompt:
        return {
            "prompt": _wan_fallback(user_text, style_clause),
            "cameraMove": normalize_camera_move(user_text),
            "degraded": True,
        }

    # 카메라 어휘를 허용 목록으로 정규화한다. 모델이 자유롭게 쓴 표현을 그대로 두면
    # 컷마다 표현이 달라지고 두 개를 섞어 쓰기도 한다.
    move = normalize_camera_move(prompt)

    # 규칙 2가 지켜졌는지 확인한다. 빠지면 웹툰 컷이 실사로 변하므로 서버가 붙인다.
    #
    # **자르기를 먼저 하고 붙이기를 나중에 한다.** 순서를 반대로 했다가 실제로 실패했다.
    # 문구를 붙인 뒤 WAN_MAX_WORDS로 자르면, 방금 붙인 그 절이 끝에 있어서 잘려 나갔다.
    # 검증에서 "화풍 유지 문구 포함"이 False로 나온 원인이 이것이었다.
    lowered = prompt.lower()
    needs_guard = not any(hint in lowered for hint in _STYLE_GUARD_HINTS)
    guard_clause = f"{_WAN_STYLE_GUARD.capitalize()}."
    budget = WAN_MAX_WORDS - (len(guard_clause.split()) if needs_guard else 0)

    words = prompt.split()
    if len(words) > budget:
        # 문장 중간에서 끊기면 지시가 반쪽만 남는다. 마지막 온전한 문장까지만 남긴다.
        clipped = " ".join(words[:budget])
        cut = max(clipped.rfind("."), clipped.rfind("!"), clipped.rfind("?"))
        prompt = clipped[: cut + 1] if cut > len(clipped) // 2 else clipped
    if needs_guard:
        prompt = f"{prompt.rstrip('.')}. {guard_clause}"
        logger.info("Wan prompt lacked the style-preservation clause; appended it")

    logger.info("Wan prompt built words=%s camera=%s", len(prompt.split()), move)
    return {"prompt": prompt, "cameraMove": move, "degraded": False}
