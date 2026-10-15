"""사용자가 적은 짧은 설명을 자세한 설명으로 다시 쓴다 (생성 직전 한 단계).

## 왜 필요한가

기획자는 "비 오는 골목에서 여자가 뒤돌아본다" 정도로 적는다. 그 문장만으로는
이미지·영상 모델이 채워야 할 빈칸이 너무 많고, 모델은 그 빈칸을 매번 다르게 메운다.
그래서 결과가 어색하고 재현성이 없다.

이 모듈은 그 문장을 **같은 의도로, 더 구체적으로** 다시 쓴다. 사용자에게 프롬프트
작성법을 요구하지 않는 것이 목적이다. 입력창에는 아무 규칙도 요구하지 않는다.

## 기존 단계와 무엇이 다른가

이미 비슷한 일을 하는 곳이 셋 있어서 역할이 겹치지 않게 잘라 두었다.

| 단계 | 하는 일 | 이 모듈과의 관계 |
| --- | --- | --- |
| `server.to_visual_english` | 한국어 장면을 영어로 **번역** | 이 모듈이 먼저 살을 붙이고, 그 결과를 번역한다 |
| `styles.route` | 그림체 문장을 보고 **프리셋을 고름** | 이 모듈이 문장을 자세히 만들어 판단 근거를 늘린다 |
| `video_prompt.build` | 영어 영상 프롬프트로 **재구성** | 이 모듈이 한국어 지시를 먼저 두껍게 만든다 |

즉 이 모듈은 **번역도 재구성도 하지 않는다.** 한국어를 한국어로, 같은 의도로 늘린다.
그래야 뒷단계 규칙(부정문 제거, 카메라 어휘 정규화, 매체 우선 배치)이 그대로 살아 있다.

## 실패를 다루는 방식

보강은 있으면 좋은 것이고 없으면 원문을 쓴다. 이 코드베이스의 다른 보조 단계와 같은 태도다
(`to_visual_english`, `styles.route`, `video_prompt.build` 모두 실패 시 원문·기본값으로 내려간다).
보강이 실패했다고 생성을 막으면 사용자는 이유를 알 수 없는 오류만 본다.
"""

from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger("toon-change-ai-pj-app.prompt_boost")

# 보강을 끌 수 있게 둔다. 호출이 한 번 더 늘어나 지연과 비용이 붙으므로
# 문제가 생기면 재배포 없이 환경변수로 멈출 수 있어야 한다.
ENABLED = os.getenv("PROMPT_BOOST_ENABLED", "1") not in {"0", "false", "False", ""}

# 사용자가 적은 원문 상한. 이보다 길면 잘라서 보낸다.
MAX_INPUT_CHARS = 4000

KIND_SCENE = "scene"
KIND_STYLE = "style"
KIND_VIDEO = "video"
KIND_TREATMENT = "treatment"

# 종류별 규칙.
#   skipAbove : 원문이 이미 이 길이를 넘으면 보강하지 않는다. 기획자가 충분히 적었다는 뜻이고,
#               그 경우 다시 쓰면 의도가 흐려지고 비용만 든다.
#   target    : 결과 목표 길이(글자). 지시문에 그대로 들어간다.
#   maxTokens : 한국어는 글자당 토큰이 1보다 크다. 목표의 3배 + 여유로 잡는다.
_RULES: dict[str, dict] = {
    KIND_SCENE: {
        "label": "장면 설명",
        "skipAbove": 220,
        "target": (90, 220),
        "maxTokens": 900,
        "role": "당신은 드라마 촬영 콘티를 정리하는 조연출입니다.",
        "goal": "기획자가 적은 장면을 그림으로 그릴 수 있을 만큼 구체적인 장면 설명으로 다시 씁니다.",
        "add": (
            "- 화면에 몇 명이 있고 각자 무엇을 하고 있는지\n"
            "- 인물의 자세, 시선의 방향, 손이 무엇을 하고 있는지\n"
            "- 장소가 어디이고 화면에 보이는 사물 두세 개\n"
            "- 시간대와 광원의 위치(창, 조명, 간판 등)\n"
            "- 카메라가 인물을 어느 거리에서 어느 높이로 보는지"
        ),
        "avoid": (
            "- 그림체나 화풍(수채화, 웹툰풍 같은 말)은 넣지 않습니다. 그것은 따로 받습니다.\n"
            "- 이야기의 앞뒤 사정, 인물의 속마음, 감정 형용사는 넣지 않습니다. 보이는 것만 씁니다.\n"
            "- 나이를 숫자로 쓰지 않습니다. '이십 대 여성'처럼 씁니다."
        ),
    },
    KIND_STYLE: {
        "label": "그림체 설명",
        "skipAbove": 160,
        "target": (50, 160),
        "maxTokens": 700,
        "role": "당신은 웹툰 IP를 드라마로 옮기는 제작사의 아트 디렉터입니다.",
        "goal": "기획자가 적은 그림체 요구를 더 또렷한 그림체 설명으로 다시 씁니다.",
        "add": (
            "- 매체(연필, 펜, 수채, 디지털 채색, 실사 촬영 등)\n"
            "- 선의 성격(굵기가 일정한지, 거친지, 없는지)\n"
            "- 색의 처리(흑백인지, 채도가 높은지, 색이 몇 개인지)\n"
            "- 명암의 양(평면인지, 단계가 있는지, 없는지)\n"
            "- 참고할 만한 시대나 결(90년대 애니, 필름 그레인 등)"
        ),
        "avoid": (
            "- 장면 내용(누가 무엇을 한다)은 절대 넣지 않습니다. 그림체만 씁니다.\n"
            "- 특정 작가나 작품 이름을 새로 만들어 넣지 않습니다.\n"
            "- 기획자가 적지 않은 매체로 바꾸지 않습니다. 적은 것을 더 자세히 만듭니다."
        ),
    },
    KIND_VIDEO: {
        "label": "영상 디렉션",
        "skipAbove": 220,
        "target": (90, 220),
        "maxTokens": 900,
        "role": "당신은 5~9초짜리 한 컷을 준비하는 촬영감독입니다.",
        "goal": "기획자가 적은 지시를 한 컷 안에서 실제로 촬영 가능한 디렉션으로 다시 씁니다.",
        "add": (
            "- 무엇이 움직이는지, 그 동작 하나를 구체적으로(고개를 돌린다, 숨을 내쉰다 수준으로 작게)\n"
            "- 인물 외의 무엇이 함께 움직이는지(머리카락, 옷자락, 김, 비, 먼지, 간판 불빛)\n"
            "- 카메라 움직임 하나(고정, 천천히 들어가기, 좌우로 팬, 따라가기 중 하나)\n"
            "- 빛이나 공기가 어떻게 변하는지"
        ),
        "avoid": (
            "- 동작을 두 개 이상 넣지 않습니다. 5~9초에 사건이 둘이면 화면이 무너집니다.\n"
            "- 장면 전환, 컷 나누기, 여러 장소를 넣지 않습니다. 하나의 연속된 테이크입니다.\n"
            "- 큰 동작(뛰기, 격투, 회전)을 새로 만들지 않습니다. 모델이 없는 신체를 상상해 그립니다.\n"
            "- 그림체나 화풍은 넣지 않습니다. 그것은 따로 받습니다."
        ),
    },
    KIND_TREATMENT: {
        "label": "트리트먼트 세부 지시",
        "skipAbove": 500,
        "target": (150, 500),
        "maxTokens": 1600,
        "role": "당신은 드라마 기획 개발 담당자입니다.",
        "goal": "기획자가 적은 요구사항을 작가가 그대로 따를 수 있는 구체적 지시로 다시 씁니다.",
        "add": (
            "- 적힌 요구가 이야기의 어느 부분에 어떻게 적용되는지\n"
            "- 요구를 지키기 위해 반드시 유지할 설정과 바꿔도 되는 설정의 구분\n"
            "- 판단이 갈릴 수 있는 지점에 대한 기준"
        ),
        "avoid": (
            "- 트리트먼트 본문을 쓰지 않습니다. 지시만 씁니다.\n"
            "- 기획자가 말하지 않은 새 설정이나 인물을 만들지 않습니다.\n"
            "- 요구를 반대로 바꾸지 않습니다. 적힌 것을 더 또렷하게 만듭니다."
        ),
    },
}

_MARKDOWN = re.compile(r"[*_`#>\[\]]")
# 모델이 붙이는 머리말. 지시로 막아도 종종 쓴다.
_PREAMBLE = re.compile(
    r"^\s*(?:다시\s*쓴|보강한|수정한|개선된|정리한)?\s*(?:장면\s*설명|그림체\s*설명|디렉션|지시)?\s*[:：]\s*",
)


def kinds() -> list[str]:
    return list(_RULES)


def _clean(text: str) -> str:
    """모델 응답을 손질한다. 머리말과 마크다운을 걷어낸다."""
    value = _MARKDOWN.sub("", text or "").strip()
    # 따옴표로 감싸 오는 경우가 있다.
    if len(value) > 1 and value[0] in "\"'“‘" and value[-1] in "\"'”’":
        value = value[1:-1].strip()
    value = _PREAMBLE.sub("", value, count=1)
    # 문단 사이 빈 줄은 살리고 그 밖의 공백은 정리한다.
    paragraphs = [" ".join(part.split()) for part in value.split("\n\n")]
    return "\n\n".join(part for part in paragraphs if part).strip()


def _instruction(rule: dict) -> str:
    low, high = rule["target"]
    return (
        f"{rule['role']}\n"
        f"{rule['goal']}\n\n"
        "[반드시 지킬 것]\n"
        "1. 기획자가 적은 의도를 바꾸지 않습니다. 없는 사실을 새로 만들지 않습니다.\n"
        "   적히지 않아 비어 있는 부분만 자연스럽게 채웁니다.\n"
        "2. 한국어로 씁니다. 번역하지 않습니다.\n"
        f"3. {low}~{high}자로 씁니다. 이 범위를 벗어나면 쓸 수 없습니다.\n"
        "4. 결과 문장만 출력합니다. 머리말, 설명, 목록 기호, 코드펜스를 붙이지 않습니다.\n"
        "5. 부정문으로 쓰지 않습니다. 무엇이 없는지가 아니라 무엇이 있는지를 씁니다.\n\n"
        f"[채워야 할 것]\n{rule['add']}\n\n"
        f"[넣지 말 것]\n{rule['avoid']}"
    )


def boost(caller, text: str, kind: str, *, context: str = "") -> dict:
    """짧은 설명을 자세한 설명으로 다시 쓴다.

    Args:
        caller: `(system, user, max_tokens, temperature) -> (text, truncated)`.
                `gateway.text_caller`가 만들어 주는 형태와 같다.
                이 모듈은 boto3를 직접 다루지 않는다(사용량 기록을 우회하지 않기 위해).
        text: 사용자가 적은 원문.
        kind: `scene` / `style` / `video` / `treatment`.
        context: 참고 정보(작품 제목, 앞 단계 결과 요약 등). 없어도 된다.

    Returns:
        {"text": 쓸 문장, "original": 원문, "changed": 바뀌었는지, "degraded": 실패했는지,
         "skipped": 건너뛴 이유 또는 ""}
    """
    original = (text or "").strip()
    result = {
        "text": original,
        "original": original,
        "changed": False,
        "degraded": False,
        "skipped": "",
    }

    rule = _RULES.get(kind)
    if not rule:
        # 호출 지점의 오타를 조용히 넘기지 않는다. 종류가 늘 때 여기를 함께 고쳐야 한다.
        raise ValueError(f"보강 종류가 올바르지 않습니다: {kind}")

    if not ENABLED:
        result["skipped"] = "disabled"
        return result
    if not caller:
        result["skipped"] = "no-caller"
        return result
    if len(original) < 2:
        # 빈 입력은 보강할 것이 없다. 뒷단계가 기본값으로 처리한다.
        result["skipped"] = "too-short"
        return result
    if len(original) >= rule["skipAbove"]:
        # 이미 충분히 적었다. 다시 쓰면 의도가 흐려지고 비용만 든다.
        result["skipped"] = "long-enough"
        return result

    user_text = original[:MAX_INPUT_CHARS]
    if context.strip():
        user_text = f"[참고 정보]\n{context.strip()[:1200]}\n\n[기획자가 적은 내용]\n{user_text}"

    try:
        raw, truncated = caller(_instruction(rule), user_text, rule["maxTokens"], 0.4)
    except Exception as error:  # noqa: BLE001 - 보강 실패가 생성을 막아서는 안 된다
        logger.warning("프롬프트 보강에 실패해 원문을 사용합니다 (kind=%s): %s", kind, error)
        result["degraded"] = True
        return result

    boosted = _clean(raw)
    if truncated:
        # 잘린 문장을 쓰면 마지막 문장이 끊긴 채 모델에 들어간다. 원문이 낫다.
        logger.warning("프롬프트 보강 출력이 잘려 원문을 사용합니다 (kind=%s)", kind)
        result["degraded"] = True
        return result
    if not boosted:
        logger.warning("프롬프트 보강이 빈 응답을 돌려줘 원문을 사용합니다 (kind=%s)", kind)
        result["degraded"] = True
        return result

    # 원문보다 짧아지면 보강이 아니다. 모델이 요약해 버린 경우다.
    if len(boosted) < len(original):
        logger.info(
            "보강 결과가 원문보다 짧아 원문을 사용합니다 (kind=%s, %s -> %s자)",
            kind,
            len(original),
            len(boosted),
        )
        result["skipped"] = "shorter"
        return result

    # 지나치게 늘어난 경우도 막는다. 목표 상한의 두 배를 넘으면 지시를 따르지 않은 것이다.
    ceiling = rule["target"][1] * 2
    if len(boosted) > ceiling:
        boosted = boosted[:ceiling].rstrip()
        logger.info("보강 결과가 너무 길어 %s자로 잘랐습니다 (kind=%s)", ceiling, kind)

    result["text"] = boosted
    result["changed"] = boosted != original
    logger.info(
        "프롬프트 보강 kind=%s %s자 -> %s자", kind, len(original), len(boosted)
    )
    return result
