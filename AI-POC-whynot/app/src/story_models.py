"""역할 → 모델 매핑표. 스토리 파이프라인의 단일 원본.

## 왜 별도 모듈인가

지시서 1절이 "역할 = Bedrock 호출 1개 = system 프롬프트 1개"로 정했다. 그러면 역할마다
어떤 모델을 쓰는지가 곧 이 기능의 비용·품질 설계다. 그 표가 코드 여러 곳에 흩어지면
모델을 바꿀 때 한 곳을 빠뜨리고, 그 역할만 조용히 옛 모델을 계속 쓴다.

`server.py` 에 상수를 더 늘리지 않는 이유도 같다. 이미 여덟 개(DOC_TEXT_MODEL,
VIDEO_PROMPT_MODEL, STYLE_ROUTER_MODEL, PROMPT_BOOST_MODEL, COVER_TEXT_MODEL ...)가
흩어져 있고, 어느 것이 무엇에 쓰이는지 읽어서 알 수 없다.

## 실측 기준 (2026-09-07, 다른 계정에서 측정)

**이 계정에서 다시 확인해야 한다.** 아래 표는 이 코드를 만든 계정의 실측이고,
Bedrock 모델 접근은 계정 단위로 갈린다. 배포 전에 `bedrock list-foundation-models`
로 대조할 것 (변경 원장 L-57).

지시서 2절은 1순위로 Claude Sonnet 5 / Opus 5 를 지정했다. **둘 다 이 계정에서 쓸 수 없다.**
세 리전(us-west-2, ap-northeast-2, us-east-1)에서 `global.*` 와 `us.*` 접두사를 모두
호출해 확인했고 전부 같은 응답이었다.

    AccessDeniedException: anthropic.claude-sonnet-5 is not available for this account.

즉 **리전 문제가 아니라 계정 단위 모델 접근 권한 문제**다. 리전을 바꿔도 열리지 않는다.
아래는 거부된 목록이다. 접근이 열리면 `_DENIED_2026_09` 에서 지우고 표의 값을 바꾼다.

    claude-sonnet-5, claude-opus-5, claude-opus-4-8, claude-opus-4-7,
    claude-fable-5, claude-fable-5-1

## 모델을 바꾸는 방법

세 가지 경로가 있고 아래로 갈수록 우선한다.

1. 이 파일의 `_DEFAULTS` 를 고친다 (배포 필요)
2. 환경변수를 준다 (k8s Deployment env, 재시작만 필요)
3. `MODEL_ENV` 에 적힌 이름으로 관리자 화면에서 덮어쓴다 (미구현, 확장 지점)

권한이 열렸을 때 할 일은 `_DEFAULTS` 의 값을 바꾸는 것 하나다. 역할 코드는 손대지 않는다.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("toon-change-ai-pj-app.story.models")

# --------------------------------------------------------------------------- #
# 역할 id
# --------------------------------------------------------------------------- #
# 이 값은 **gateway 의 purpose 문자열과 같다.** 사용량 기록(usage.purpose)과
# 프론트엔드 프롬프트 계층(prompt_layers.ROLES[].id)이 같은 축을 쓰게 하기 위한 것이다.
# 새 역할을 넣으면 prompt_layers.ROLES 에도 같은 id 로 추가해야 관리자 화면에 나온다.
ROLE_VISION = "story.vision"
ROLE_BIBLE = "story.bible"
ROLE_LOGLINE = "story.writer.logline"
ROLE_STORYBOARD = "story.writer.storyboard"
ROLE_SCRIPT_OUTLINE = "story.writer.script_outline"
ROLE_SCRIPT_EPISODE = "story.writer.script_episode"
ROLE_VALIDATOR = "story.validator"
ROLE_SUMMARY = "story.summary"

# --- GPU 영상 생성 기능 (신규) ---
# 접두사가 story. 가 아니라 video. 인 이유: 이 두 역할은 스토리 파이프라인의 결과물을
# 만들지 않는다. 영상 생성 앞단에서 샷을 쪼개고 캐릭터 외모를 텍스트로 고정한다.
# 사용량 통계에서 두 기능의 비용을 분리해 보려면 축이 갈려 있어야 한다.
ROLE_SHOT_PLANNER = "video.shot_planner"
ROLE_CHARACTER_SHEET = "video.character_sheet"

ROLES = (
    ROLE_VISION,
    ROLE_BIBLE,
    ROLE_LOGLINE,
    ROLE_STORYBOARD,
    ROLE_SCRIPT_OUTLINE,
    ROLE_SCRIPT_EPISODE,
    ROLE_VALIDATOR,
    ROLE_SUMMARY,
    ROLE_SHOT_PLANNER,
    ROLE_CHARACTER_SHEET,
)

# --------------------------------------------------------------------------- #
# 접근이 거부된 모델 (실측)
# --------------------------------------------------------------------------- #
# 표에 실수로 넣는 것을 막기 위한 가드다. `resolve()` 가 이 목록에 있는 값을 만나면
# 폴백으로 내려가고 경고를 남긴다. 조용히 500 을 내는 것보다 낫다.
#
# 권한이 열리면 여기서 지운다. 지우지 않으면 표를 바꿔도 폴백이 계속 걸린다.
_DENIED_2026_09 = frozenset(
    {
        "anthropic.claude-sonnet-5",
        "anthropic.claude-opus-5",
        "anthropic.claude-opus-4-8",
        "anthropic.claude-opus-4-7",
        "anthropic.claude-fable-5",
        "anthropic.claude-fable-5-1",
    }
)

# --------------------------------------------------------------------------- #
# 실제 값
# --------------------------------------------------------------------------- #
# 접근 가능한 것 중 가장 좋은 모델. 실측으로 확인했다.
#
# 이 세 개가 **코드펜스 없이 순수 JSON** 을 낸다는 것도 함께 확인했다. haiku 와 nova 계열은
# ```json 펜스를 붙이므로 파서가 반드시 벗겨야 한다(roles/_json.py 가 처리).
_SONNET = "global.anthropic.claude-sonnet-4-6"
_SONNET_PREV = "global.anthropic.claude-sonnet-4-5-20250929-v1:0"
_OPUS = "global.anthropic.claude-opus-4-6-v1"
_HAIKU = "global.anthropic.claude-haiku-4-5-20251001-v1:0"

# 마지막 폴백. 이 앱이 이미 오래 쓰고 있어 가장 확실하게 동작하는 값이다.
FALLBACK_MODEL = _SONNET_PREV

# 역할 → (환경변수 이름, 기본 모델, 최대 출력 토큰, 온도)
#
# max_tokens 는 실측 상한을 넘지 않게 잡았다.
#   sonnet-4-6 : 65,536 통과 / 131,072 거부
#   haiku-4-5  : 32,768 통과 /  65,536 거부
# 상한에 붙여 쓰지 않는다. 길게 잡으면 모델이 분량을 채우려 늘어지고 비용이 그대로 는다.
# 회차 분량 상한(지시서 10절 2번)은 script_episode 의 값이 근거다.
_DEFAULTS: dict[str, dict] = {
    # 이미지를 읽는 유일한 역할. 비전이 되는 모델이어야 한다.
    # gpt-oss-120b 는 접근 가능하지만 **입력 IMAGE 를 지원하지 않아** 후보가 아니다.
    ROLE_VISION: {
        "env": "STORY_VISION_MODEL",
        "model": _SONNET,
        "max_tokens": 4000,
        # 분석은 사실 판단이다. 낮게 둔다.
        "temperature": 0.2,
    },
    # 원문 + 분석을 합쳐 인물·설정·플롯을 뽑는다. 뒤의 모든 역할이 이 결과를 근거로 쓴다.
    # 여기가 흔들리면 결과물 전체가 흔들리므로 가장 좋은 모델을 쓴다.
    ROLE_BIBLE: {
        "env": "STORY_BIBLE_MODEL",
        "model": _SONNET,
        "max_tokens": 8000,
        "temperature": 0.3,
    },
    # 3안 × 2문장. 짧다.
    ROLE_LOGLINE: {
        "env": "STORY_LOGLINE_MODEL",
        "model": _SONNET,
        "max_tokens": 2000,
        # 세 안이 서로 달라야 의미가 있다. 조금 올린다.
        "temperature": 0.7,
    },
    # 컷 수만큼 분량이 늘어난다. 24컷 × (설명+대사+이미지 프롬프트) 를 담아야 한다.
    ROLE_STORYBOARD: {
        "env": "STORY_STORYBOARD_MODEL",
        "model": _SONNET,
        "max_tokens": 12000,
        "temperature": 0.5,
    },
    # 전체 회차의 뼈대. 회차 수만큼 늘어난다.
    ROLE_SCRIPT_OUTLINE: {
        "env": "STORY_SCRIPT_OUTLINE_MODEL",
        "model": _SONNET,
        "max_tokens": 10000,
        "temperature": 0.4,
    },
    # 회차 하나가 이 파이프라인에서 가장 긴 출력이다.
    # 16,000 토큰이면 한국어 대본 약 12,000~16,000자에 해당한다(장편 1회차 분량).
    ROLE_SCRIPT_EPISODE: {
        "env": "STORY_SCRIPT_EPISODE_MODEL",
        "model": _SONNET,
        "max_tokens": 16000,
        "temperature": 0.6,
    },
    # 스키마는 코드가 먼저 검사한다(무료). 이 역할은 일관성만 본다. 값싼 모델로 충분하다.
    ROLE_VALIDATOR: {
        "env": "STORY_VALIDATOR_MODEL",
        "model": _HAIKU,
        "max_tokens": 2000,
        # 판정이 흔들리면 재생성이 무작위로 돈다. 가장 낮게 둔다.
        "temperature": 0.0,
    },
    # 실제 스토리 → 요약 스토리(300자). 짧은 요약이라 값싼 모델로 충분하다.
    ROLE_SUMMARY: {
        "env": "STORY_SUMMARY_MODEL",
        "model": _HAIKU,
        "max_tokens": 1000,
        "temperature": 0.3,
    },
    # --- GPU 영상 생성 (신규) ---
    #
    # 3줄 입력 → 샷 N개. 이 기능의 핵심 가설이 "샷 분해를 앞에 두면 없던 장면이 생긴다"
    # 이므로 여기가 흔들리면 뒤가 전부 흔들린다. 좋은 모델을 쓴다.
    #
    # 샷 하나에 장소·앵글·인물·동작·길이·키프레임 프롬프트가 들어간다. 8샷이면
    # 그 여섯 항목이 8벌이고, keyframe_prompt 는 영어 시각 묘사라 길다.
    ROLE_SHOT_PLANNER: {
        "env": "VIDEO_SHOT_PLANNER_MODEL",
        "model": _SONNET,
        "max_tokens": 8000,
        # 같은 입력에 매번 다른 샷이 나오면 사용자가 결과를 비교할 수 없다.
        # 그렇다고 0 으로 두면 장면이 뻔해진다. 중간을 쓴다.
        "temperature": 0.5,
    },
    # 레퍼런스 비전 분석 여러 장 → 캐릭터 외모 텍스트 1벌.
    #
    # 이 텍스트가 키프레임 프롬프트에 그대로 삽입되어 **모든 샷의 인물 동일성을
    # 결정한다.** 요약처럼 보이지만 실패 비용이 크다. haiku 로 내리지 않는다.
    ROLE_CHARACTER_SHEET: {
        "env": "VIDEO_CHARACTER_SHEET_MODEL",
        "model": _SONNET,
        "max_tokens": 3000,
        # 외모 기술은 사실 판단이다. 낮게 둔다 — 여기서 창작이 들어가면
        # 레퍼런스에 없는 특징이 생기고 그것이 모든 샷에 퍼진다.
        "temperature": 0.2,
    },
}

# 대본 "고품질" 옵션. 사용자가 모달에서 고르면 script_outline/script_episode 가 이것을 쓴다.
# 지시서는 Opus 5 를 지정했지만 접근이 없어 접근 가능한 최상위인 opus-4-6 을 쓴다.
HQ_ENV = "STORY_SCRIPT_HQ_MODEL"
HQ_DEFAULT = _OPUS

# 대본 품질 옵션 값. 화면과 서버가 같은 문자열을 써야 한다.
QUALITY_STANDARD = "standard"
QUALITY_HIGH = "high"
QUALITY_CHOICES = (QUALITY_STANDARD, QUALITY_HIGH)


def _env_model(env_name: str, default: str) -> str:
    """환경변수가 있으면 그것, 없으면 기본값. 거부 목록에 걸리면 폴백."""
    value = (os.getenv(env_name) or "").strip() or default
    # 접두사(global./us./apac.)를 뗀 기본 이름으로 대조한다. 거부는 모델 단위이고
    # 접두사는 라우팅일 뿐이라, 접두사째로 비교하면 us. 로 적힌 것을 놓친다.
    bare = value.split(".", 1)[1] if value.split(".", 1)[0] in {"global", "us", "apac", "eu"} else value
    if bare in _DENIED_2026_09:
        logger.warning(
            "모델 %s 는 이 계정에서 접근이 거부된 목록에 있습니다. %s 로 대체합니다. "
            "권한이 열렸다면 story_models._DENIED_2026_09 에서 지우세요.",
            value,
            FALLBACK_MODEL,
        )
        return FALLBACK_MODEL
    return value


def spec(role: str, quality: str = QUALITY_STANDARD) -> dict:
    """이 역할이 쓸 {model, max_tokens, temperature}.

    `quality=high` 는 대본 역할에만 적용된다. 다른 역할에 넘겨도 무시한다 —
    로그라인이나 검수를 Opus 로 돌리는 것은 비용만 늘리고 결과가 달라지지 않는다.
    """
    entry = _DEFAULTS.get(role)
    if not entry:
        raise ValueError(f"'{role}' 은 등록되지 않은 역할입니다. story_models.ROLES 를 확인하세요.")

    script_roles = {ROLE_SCRIPT_OUTLINE, ROLE_SCRIPT_EPISODE}
    if quality == QUALITY_HIGH and role in script_roles:
        model = _env_model(HQ_ENV, HQ_DEFAULT)
    else:
        model = _env_model(entry["env"], entry["model"])

    return {
        "role": role,
        "model": model,
        "max_tokens": int(entry["max_tokens"]),
        "temperature": float(entry["temperature"]),
    }


def table() -> list[dict]:
    """지금 실제로 쓰이는 역할→모델 표. 관리자 화면과 보고서가 같은 함수를 쓴다."""
    rows = []
    for role in ROLES:
        entry = _DEFAULTS[role]
        rows.append(
            {
                "role": role,
                "env": entry["env"],
                "model": _env_model(entry["env"], entry["model"]),
                "maxTokens": entry["max_tokens"],
                "temperature": entry["temperature"],
            }
        )
    rows.append(
        {
            "role": "story.writer.script_*(고품질 옵션)",
            "env": HQ_ENV,
            "model": _env_model(HQ_ENV, HQ_DEFAULT),
            "maxTokens": _DEFAULTS[ROLE_SCRIPT_EPISODE]["max_tokens"],
            "temperature": _DEFAULTS[ROLE_SCRIPT_EPISODE]["temperature"],
        }
    )
    return rows
