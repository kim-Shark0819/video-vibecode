"""프롬프트 두 계층 관리 (지시서 작업 5~8).

## 무엇을 나누는가

지시서 6-1 / 6-2가 정한 두 계층이다.

| 계층 | 누가 바꾸는가 | 어디에 있는가 |
| --- | --- | --- |
| 백엔드 프롬프트 | 아무도 화면에서 못 바꾼다. 배포로만 | 파이썬 소스 상수 (documents / styles / video_prompt / image_brief / prompt_boost) |
| 프론트엔드 프롬프트 | Admin이 화면에서 조회·수정 | S3 `_config/prompts.json` |

최종 결합 순서는 **백엔드 → 프론트엔드 → 사용자 입력**이고 백엔드가 최상위다(5-4, 6-3).
프론트엔드 프롬프트나 사용자 입력이 백엔드를 덮어쓸 수 없다.

## 왜 '모델'이 아니라 '용도 × 모델'을 키로 쓰는가

지시서 8-2는 "모델 목록 = 백엔드 프롬프트 목록"이라고 쓰지만, 표 컬럼은
`모델명 | 용도 | 백엔드 프롬프트 유무 | 프론트엔드 프롬프트 기본값`이다. 용도 컬럼이 있다.

실제 구조가 그렇게 되어 있다. `claude-sonnet-4-5` 하나가 산출물 생성, 영상 프롬프트 변환,
참고 이미지 분석, 롱테이크 컷 나누기를 **모두** 맡는다. 각 용도의 시스템 프롬프트는 완전히
다르다. 모델 ID만 키로 쓰면 서로 다른 프롬프트 네 개가 한 칸에 뭉개진다.

그래서 키는 `gateway`가 이미 사용량 기록에 쓰는 `purpose` 문자열과 일치시킨다.
그러면 사용량 통계와 프롬프트 관리가 같은 축을 갖는다.

## 순환 임포트를 피하는 방법

이 모듈은 `documents`, `styles`, `video_prompt` 같은 앱 모듈을 **모듈 수준에서 임포트하지
않는다.** 그 모듈들이 프론트엔드 조각을 얻으려고 이 모듈을 임포트하기 때문이다.

- 프론트엔드 조각 읽기·결합(`fragment`, `compose_system`)은 의존성이 없어 누구나 부를 수 있다.
- 백엔드 프롬프트 원문 수집(`inventory`)만 함수 안에서 늦게 임포트한다.
  Admin 조회와 매핑표 생성에서만 필요하고, 호출 경로에서는 쓰지 않는다.

## 백엔드 프롬프트를 복사하지 않는다

`inventory()`는 기존 상수를 **참조**해서 읽는다. 여기에 프롬프트 원문을 다시 적으면
소스와 이 파일이 조용히 갈라진다. 실제 호출은 원래 상수를 쓰므로, 갈라지면 화면이
거짓말을 하게 된다.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import threading
from datetime import datetime, timezone

import boto3
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger("toon-change-ai-pj-app.prompt-layers")

AWS_REGION = os.getenv("AWS_REGION", "ap-northeast-2")
S3_BUCKET = os.getenv("S3_BUCKET", "mtx-test-ai-change-toon")
# 모델 카탈로그(_config/models.json)와 같은 자리에 둔다. 버킷 버저닝이 켜져 있어
# 객체 버전이 그대로 수정 이력이 된다. Neptune 속성에 수 KB 프롬프트를 넣지 않는 이유는
# 이 코드베이스가 큰 본문을 전부 S3로 빼는 방침을 이미 갖고 있기 때문이다.
CONFIG_KEY = os.getenv("PROMPT_CONFIG_KEY", "_config/prompts.json")
CACHE_TTL_SECONDS = int(os.getenv("PROMPT_CACHE_TTL_SECONDS", "30"))
# Admin이 적을 수 있는 프론트엔드 프롬프트 길이 상한.
# 이보다 길면 백엔드 지시를 희석시켜 결과가 나빠진다.
MAX_FRONTEND_CHARS = int(os.getenv("PROMPT_MAX_FRONTEND_CHARS", "2000"))

_s3 = boto3.client("s3", region_name=AWS_REGION)
_lock = threading.Lock()
_cache: dict = {"loaded_at": 0.0, "prompts": None}


class PromptError(ValueError):
    """Admin 입력이 거부될 때. 사용자에게 보여줄 수 있는 메시지를 담는다."""


# --------------------------------------------------------------------------- #
# 모델 슬롯
# --------------------------------------------------------------------------- #
# 환경변수 이름과 기본값은 실제 정의 지점과 같아야 한다.
# .build/check-prompt-layers.py 가 소스와 이 표를 대조한다.
def _model(env: str, default: str) -> str:
    return os.getenv(env, default)


_SONNET = "global.anthropic.claude-sonnet-4-5-20250929-v1:0"
_HAIKU = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
_OPUS = "global.anthropic.claude-opus-4-5-20251101-v1:0"


def model_for(env: str) -> str:
    """이 슬롯이 지금 실제로 쓰는 모델 ID."""
    return {
        "DOC_TEXT_MODEL": lambda: _model("DOC_TEXT_MODEL", _SONNET),
        "STORYBOARD_TEXT_MODEL": lambda: _model(
            "STORYBOARD_TEXT_MODEL", _model("DOC_TEXT_MODEL", _SONNET)
        ),
        "VIDEO_PROMPT_MODEL": lambda: _model("VIDEO_PROMPT_MODEL", _SONNET),
        "STYLE_ROUTER_MODEL": lambda: _model("STYLE_ROUTER_MODEL", _HAIKU),
        "PROMPT_BOOST_MODEL": lambda: _model("PROMPT_BOOST_MODEL", _OPUS),
        "COVER_TEXT_MODEL": lambda: _model("COVER_TEXT_MODEL", _HAIKU),
        # COVER_IMAGE_MODEL 슬롯은 없다. 표지 그림도 카탈로그의 image 모델을 쓰므로
        # 아래 CATALOG_IMAGE 가 그 자리다. 환경변수 기본값을 여기에 또 적어 두었더니
        # server.py 의 값과 어긋났고, 화면은 실제로 호출되지 않는 모델을 보여 주었다.
        # 카탈로그에서 고르는 슬롯. 요청 시점에 tier로 결정되므로 고정 ID가 없다.
        "CATALOG_IMAGE": lambda: "(카탈로그 image 모델 중 등급으로 선택)",
        "CATALOG_VIDEO": lambda: "(카탈로그 video 모델 중 기본값)",
        # 자체 GPU. Bedrock이 아니라 카탈로그에 없다.
        "SELF_HOSTED_WAN": lambda: "Wan-AI/Wan2.2-I2V-A14B (자체 GPU)",
        # 스토리 파이프라인. 이 슬롯들은 story_models 표가 단독으로 갖는다.
        # 여기서 기본값을 다시 적지 않는다 — 두 곳에 두면 반드시 어긋난다.
        # story_models.spec() 이 환경변수와 거부 목록까지 함께 처리한 최종 값을 준다.
        **{
            f"STORY:{role}": (lambda role=role: _story_model(role))
            for role in _STORY_ROLE_IDS
        },
    }.get(env, lambda: env)()


# 스토리 역할 id. story_models.ROLES 와 같아야 한다.
# **story_models 를 임포트하지 않는다** — prompt_layers 는 더 아래 계층이고,
# story_models 가 나중에 prompt_layers 를 참조하게 되면 순환이 된다.
# 대신 _story_model() 이 호출 시점에 늦게 임포트한다(이 파일의 _resolve_ref 와 같은 방식).
_STORY_ROLE_IDS = (
    "story.vision",
    "story.bible",
    "story.writer.logline",
    "story.writer.storyboard",
    "story.writer.script_outline",
    "story.writer.script_episode",
    "story.validator",
    "story.summary",
)


def _story_model(role: str) -> str:
    """스토리 역할이 지금 쓰는 모델 ID. story_models 가 단일 원본이다."""
    try:
        from . import story_models

        return story_models.spec(role)["model"]
    except Exception as error:  # noqa: BLE001 - 조회 실패가 관리자 화면을 막아서는 안 된다
        logger.warning("스토리 모델 조회 실패 role=%s: %s", role, error)
        return f"(조회 실패: {role})"


# --------------------------------------------------------------------------- #
# 역할 정의
# --------------------------------------------------------------------------- #
# id 는 gateway 의 purpose 문자열과 같다. 사용량 통계와 같은 축을 쓰기 위한 것이다.
#
# backendRefs 는 백엔드 프롬프트 원문이 있는 곳을 가리킨다(모듈, 심볼). 원문을 여기에
# 적지 않는다. inventory() 가 이 참조로 실제 값을 읽어 온다.
#
# frontendHint 는 Admin 화면에 적히는 안내다. 무엇을 적으면 되는지 알려 주지 않으면
# 편집 칸이 있어도 쓰지 못한다.
ROLES: list[dict] = [
    # ---- 산출물 (텍스트 LLM) : 지시서 7-4 ----
    *[
        {
            "id": f"document:{kind}",
            "label": label,
            "usage": "기획 산출물 생성",
            "kind": "text",
            "modelEnv": "DOC_TEXT_MODEL",
            "backendRefs": [
                ("documents", "BASE_RULES", "공통 역할·규칙"),
                ("documents", f"KIND_SPECS[{kind}]", "이 산출물의 목표·스키마·분량"),
            ],
            "frontendHint": "이 산출물의 톤이나 강조점을 적습니다. 예: 투자 검토용이라 리스크를 더 구체적으로.",
        }
        for kind, label in (
            ("analysis", "영상화 가능성 분석"),
            ("adaptation", "각색 방향 제안"),
            ("logline", "로그라인 구체화"),
            ("characters", "인물 설정"),
            ("treatment", "트리트먼트"),
        )
    ],
    # ---- 콘티 ----
    {
        "id": "storyboard:plan",
        "label": "콘티 컷 기획",
        "usage": "이야기를 컷으로 나누기 / 직접 적은 컷 옮기기",
        "kind": "text",
        "modelEnv": "STORYBOARD_TEXT_MODEL",
        "backendRefs": [
            ("documents", "_SB_RULES", "AI가 컷을 나눌 때의 규칙"),
            ("documents", "_MANUAL_RULES", "기획자가 적은 컷을 옮길 때의 규칙"),
            ("documents", "_SB_SCHEMA", "출력 형식"),
        ],
        "frontendHint": "컷을 나누는 성향을 적습니다. 예: 대사보다 표정과 손동작 위주로.",
    },
    # ---- 롱테이크 ----
    {
        "id": "sequence:plan",
        "label": "롱테이크 컷 나누기",
        "usage": "긴 영상을 여러 컷으로 분할",
        "kind": "text",
        "modelEnv": "VIDEO_PROMPT_MODEL",
        "backendRefs": [
            ("documents", "_SHOT_PLAN_RULES", "컷 분할 규칙"),
            ("documents", "_SHOT_PLAN_SCHEMA", "출력 형식"),
        ],
        "frontendHint": "컷 흐름의 성향을 적습니다. 예: 첫 컷은 항상 와이드로 시작.",
    },
    # ---- 영상 프롬프트 변환 ----
    {
        "id": "video:prompt",
        "label": "영상 프롬프트 변환 (Bedrock/Luma 경로)",
        "usage": "한국어 지시 → 영어 영상 프롬프트",
        "kind": "text",
        "modelEnv": "VIDEO_PROMPT_MODEL",
        "backendRefs": [
            ("video_prompt", "_IMAGE_INSTRUCTION", "첫 프레임이 있을 때"),
            ("video_prompt", "_TEXT_INSTRUCTION", "텍스트만일 때"),
        ],
        "frontendHint": "카메라·조명 성향을 적습니다. 예: 항상 얕은 심도로.",
    },
    {
        "id": "video:wan-prompt",
        "label": "영상 프롬프트 변환 (Wan 2.2 경로)",
        "usage": "대본·지문 → 영어 영상 프롬프트 (지시서 7-2)",
        "kind": "text",
        "modelEnv": "VIDEO_PROMPT_MODEL",
        "backendRefs": [
            ("video_prompt", "_WAN_INSTRUCTION", "지시서 7-2 원문"),
            ("video_prompt", "_WAN_STYLE_GUARD", "화풍 유지 절 (누락 시 서버가 강제 삽입)"),
        ],
        "frontendHint": "원작 화풍 유지 방향을 적습니다. 예: 웹툰 선을 살리고 실사화하지 않는다.",
    },
    # ---- 참고 이미지 분석 ----
    {
        "id": "image:brief",
        "label": "참고 이미지 분석",
        "usage": "파일명·그림을 읽어 역할 판단과 확인 질문 생성",
        "kind": "text",
        "modelEnv": "VIDEO_PROMPT_MODEL",
        "backendRefs": [
            ("image_brief", "_INSTRUCTION", "분석·질문 생성 규칙"),
            ("image_brief", "_TARGET_NOTES", "모델별 질문 방향"),
        ],
        "frontendHint": "추가로 확인받고 싶은 항목을 적습니다. 예: 인물 나이대를 항상 묻는다.",
    },
    # ---- 그림체 판단 ----
    {
        "id": "style:route",
        "label": "그림체 판단",
        "usage": "한국어 그림체 요구 → 프리셋·품질 등급 선택",
        "kind": "text",
        "modelEnv": "STYLE_ROUTER_MODEL",
        "backendRefs": [("styles", "_ROUTER_INSTRUCTION", "프리셋 선택 규칙")],
        "frontendHint": "프리셋 선택 성향을 적습니다. 예: 애매하면 웹툰 채색을 고른다.",
    },
    {
        "id": "image:translate",
        "label": "장면 설명 영어 변환",
        "usage": "한국어 장면 → 영어 시각 묘사",
        "kind": "text",
        "modelEnv": "STYLE_ROUTER_MODEL",
        "backendRefs": [("server", "to_visual_english 지시문", "번역 규칙")],
        "frontendHint": "번역 시 유지할 어휘를 적습니다. 예: 한복은 hanbok으로 음역.",
    },
    # ---- 사용자 입력 보강 ----
    *[
        {
            "id": f"boost:{kind}",
            "label": f"입력 보강 · {label}",
            "usage": "사용자가 적은 짧은 문장을 자세하게 다시 쓰기",
            "kind": "text",
            "modelEnv": "PROMPT_BOOST_MODEL",
            "backendRefs": [
                ("prompt_boost", f"_RULES[{kind}]", "보강 규칙"),
                ("prompt_boost", "_instruction", "공통 5개 규칙"),
            ],
            "frontendHint": "보강 방향을 적습니다. 예: 소품 묘사를 반드시 두 개 이상 넣는다.",
        }
        for kind, label in (
            ("scene", "장면 설명"),
            ("style", "그림체 설명"),
            ("video", "영상 디렉션"),
            ("treatment", "트리트먼트 세부 지시"),
        )
    ],
    # ---- 표지 ----
    {
        "id": "cover:prompt",
        "label": "표지 프롬프트 작성",
        "usage": "스토리 → 표지 이미지 영어 프롬프트",
        "kind": "text",
        "modelEnv": "COVER_TEXT_MODEL",
        "backendRefs": [("server", "build_cover_prompt 지시문", "표지 구성 규칙")],
        "frontendHint": "표지 구성 성향을 적습니다. 예: 인물 얼굴을 크게, 제목 자리를 위에 비운다.",
    },
    {
        "id": "cover:image",
        "label": "표지 이미지 생성",
        "usage": "표지 이미지 실제 생성",
        "kind": "image",
        # 표지도 카탈로그의 image 기본 모델을 쓴다(server.render_cover_image).
        "modelEnv": "CATALOG_IMAGE",
        "backendRefs": [("styles", "BASE_NEGATIVE", "공통 부정 프롬프트")],
        "frontendHint": "표지에 항상 반영할 화풍을 적습니다.",
    },
    # ---- 이미지 생성 (지시서 7-3) ----
    *[
        {
            "id": f"image:{purpose}",
            "label": f"이미지 생성 · {label}",
            "usage": "장면 + 그림체 → 이미지",
            "kind": "image",
            "modelEnv": "CATALOG_IMAGE",
            "backendRefs": [
                ("styles", "STYLE_PRESETS", "프리셋 12종의 매체 선언·세부·부정 프롬프트"),
                ("styles", "BASE_NEGATIVE", "공통 부정 프롬프트"),
                ("styles", "compose_prompt", "매체 우선 조립 순서"),
            ],
            "frontendHint": "이 용도에 항상 붙일 화풍 지시를 적습니다(영어 권장).",
        }
        for purpose, label in (
            ("scene", "장면"),
            ("storyboard", "콘티 컷"),
            ("keyart", "키비주얼"),
            ("videoFrame", "영상 첫 프레임"),
        )
    ],
    # 화풍 변환(2단계 파이프라인의 1단계). 위 image:* 와 같은 프리셋 체계를 쓰지만
    # 용도가 다르다. 여기 조각은 "웹툰 컷을 무엇으로 바꿀 때 항상 지킬 것"이고,
    # image:videoFrame 조각은 "첫 프레임을 새로 그릴 때"다. 한 칸으로 묶으면 한쪽을
    # 고칠 때 다른 쪽이 함께 바뀐다.
    {
        "id": "image:toon-convert",
        "label": "화풍 변환 (구도 유지)",
        "usage": "웹툰 컷의 구도를 유지하며 화풍만 교체 (Wan 첫 프레임 준비)",
        "kind": "image",
        "modelEnv": "CATALOG_IMAGE",
        "backendRefs": [
            ("toon_convert", "MODES", "모드별 프리셋과 구도 유지 강도"),
            ("styles", "STYLE_PRESETS", "프리셋의 매체 선언·세부·부정 프롬프트"),
            ("styles", "BASE_NEGATIVE", "공통 부정 프롬프트"),
        ],
        "frontendHint": "변환 결과에 항상 반영할 지시를 적습니다(영어 권장). 예: keep the original hairstyle silhouette.",
    },
    # ---- 영상 생성 ----
    {
        "id": "video:generate",
        "label": "영상 생성 (Bedrock/Luma)",
        "usage": "이미지 → 영상 (짧은 컷)",
        "kind": "video",
        "modelEnv": "CATALOG_VIDEO",
        "backendRefs": [
            ("video_prompt", "TECHNICAL_TAIL", "모든 프롬프트에 붙는 기술 절"),
            ("video_prompt", "sanitize", "부정문 제거 후처리"),
        ],
        "frontendHint": "영상 전반에 항상 붙일 지시를 적습니다(영어 권장).",
    },
    {
        "id": "video:sequence",
        "label": "영상 생성 · 롱테이크 (Bedrock/Luma)",
        "usage": "컷 여러 개를 이어 붙인 긴 영상",
        "kind": "video",
        "modelEnv": "CATALOG_VIDEO",
        "backendRefs": [
            ("video_prompt", "TECHNICAL_TAIL", "모든 프롬프트에 붙는 기술 절"),
            ("video_prompt", "assemble_shot", "컷별 조립"),
        ],
        "frontendHint": "롱테이크 전반에 항상 붙일 지시를 적습니다.",
    },
    {
        "id": "video:wan",
        "label": "영상 생성 (Wan 2.2 · 자체 GPU)",
        "usage": "웹툰 컷 → 영상. 지시서 9-2에 따라 영상 전용",
        "kind": "video",
        "modelEnv": "SELF_HOSTED_WAN",
        "backendRefs": [
            (
                "video-gen/patch_neg_prompt.py",
                "NEGATIVE_PROMPT",
                "고정 부정 프롬프트. 빌드 시 이미지에 구워 넣어 런타임에 바꿀 수 없다 (지시서 6-2)",
            )
        ],
        # 이 역할의 긍정 프롬프트는 video:wan-prompt 가 만든다. 여기서 또 받으면 두 번 붙는다.
        "frontendEditable": False,
        # Bedrock 이 아니라 자체 GPU(wan_client)로 간다. gateway 를 지나지 않으므로
        # 사용량 purpose 가 없다. 검사 스크립트가 purpose 대조에서 제외한다.
        "gatewayRouted": False,
        "frontendHint": "부정 프롬프트는 배포로만 바뀝니다. 긍정 프롬프트는 'Wan 2.2 경로' 항목에서 조정하세요.",
    },
    # ---- 스토리 파이프라인 (로그라인 / 콘티 / 대본) ----
    # id 가 gateway 의 purpose 이고 story_models.ROLES 와 같은 값이다. 세 곳이 같은 문자열을
    # 쓰기 때문에 사용량 통계·프론트엔드 프롬프트·모델 표가 자동으로 같은 축에 정렬된다.
    {
        "id": "story.vision",
        "label": "비전 분석가",
        "usage": "첨부 이미지 1장을 읽어 인물·배경·화풍·말풍선 텍스트를 추출",
        "kind": "text",
        "modelEnv": "STORY:story.vision",
        "backendRefs": [
            ("prompts_story", "BASE_RULES", "공통 규칙(고유명사 보존 등)"),
            ("prompts_story", "ANALYZE_IMAGE", "이미지 분석 지시와 출력 스키마"),
        ],
        "frontendHint": "추가로 읽어야 할 항목을 적습니다. 예: 간판의 상호명을 항상 전사한다.",
    },
    {
        "id": "story.bible",
        "label": "바이블 작성자",
        "usage": "원문 + 이미지 분석 → 인물·설정·플롯의 중간 표현",
        "kind": "text",
        "modelEnv": "STORY:story.bible",
        "backendRefs": [
            ("prompts_story", "BASE_RULES", "공통 규칙"),
            ("prompts_story", "BUILD_BIBLE", "바이블 스키마와 타입별 처리"),
        ],
        "frontendHint": "바이블에 꼭 담을 항목을 적습니다. 예: 인물마다 말버릇을 한 줄로 남긴다.",
    },
    {
        "id": "story.writer.logline",
        "label": "로그라인 작성자",
        "usage": "바이블 → 서로 다른 각도의 로그라인 3안",
        "kind": "text",
        "modelEnv": "STORY:story.writer.logline",
        "backendRefs": [("prompts_story", "LOGLINE", "3안 구성 규칙")],
        "frontendHint": "선호하는 로그라인 문체를 적습니다. 예: 장르 관습어를 쓰지 않는다.",
    },
    {
        "id": "story.writer.storyboard",
        "label": "콘티 작성자",
        "usage": "바이블 → 컷 배열(설명·대사·영어 이미지 프롬프트)",
        "kind": "text",
        "modelEnv": "STORY:story.writer.storyboard",
        "backendRefs": [("prompts_story", "STORYBOARD", "컷 분할·숏 크기·영어 프롬프트 규칙")],
        "frontendHint": "컷 분할 성향을 적습니다. 예: 첫 컷은 항상 와이드로 시작한다.",
    },
    {
        "id": "story.writer.script_outline",
        "label": "대본 아웃라인 작성자",
        "usage": "회차 범위의 뼈대와 엔딩 훅 (승인 대상)",
        "kind": "text",
        "modelEnv": "STORY:story.writer.script_outline",
        "backendRefs": [("prompts_story", "SCRIPT_OUTLINE", "회차 구성과 훅 규칙")],
        "frontendHint": "회차 구성 성향을 적습니다. 예: 매 회차를 인물 시점 교차로 구성한다.",
    },
    {
        "id": "story.writer.script_episode",
        "label": "대본 회차 작성자",
        "usage": "회차 하나의 대본 (씬·지문·대사)",
        "kind": "text",
        "modelEnv": "STORY:story.writer.script_episode",
        "backendRefs": [("prompts_story", "SCRIPT_EPISODE", "형식·연속성·분량 규칙")],
        "frontendHint": "대본 문체를 적습니다. 예: 지문을 짧게 끊고 감정 형용사를 쓰지 않는다.",
    },
    {
        "id": "story.validator",
        "label": "검수자",
        "usage": "결과물이 바이블과 어긋나는지 검사(2차). 1차는 코드가 무료로 한다",
        "kind": "text",
        "modelEnv": "STORY:story.validator",
        "backendRefs": [("prompts_story", "VALIDATE", "검사 항목과 판정 형식")],
        "frontendHint": (
            "추가로 확인할 일관성 항목을 적습니다. "
            "문장 다듬기나 재미 평가를 요구하지 마세요 — 재생성이 무한히 돕니다."
        ),
    },
    # ---- GPU 영상 생성 (신규) ----
    # 접두사가 story. 가 아니라 video. 다. 이 두 역할은 스토리 결과물을 만들지 않고
    # 영상 생성 앞단에서 샷을 쪼개고 인물 외모를 고정한다.
    {
        "id": "video.shot_planner",
        "label": "샷 분해",
        "usage": "사용자 입력(≤3줄) + 바이블 → 샷 목록(장소·앵글·인물·동작·키프레임 프롬프트)",
        "kind": "text",
        "modelEnv": "STORY:video.shot_planner",
        "backendRefs": [
            ("prompts_story", "BASE_RULES", "공통 규칙(고유명사 보존 등)"),
            ("prompts_story", "SHOT_PLANNER", "샷 분해 기준과 출력 스키마"),
        ],
        "frontendHint": (
            "샷을 나누는 취향을 적습니다. 예: 대화 장면은 항상 리버스 샷을 넣는다. "
            "인물 외모는 여기 적지 마세요 — 캐릭터 시트가 담당합니다."
        ),
    },
    {
        "id": "video.character_sheet",
        "label": "캐릭터 시트 작성",
        "usage": "레퍼런스 비전 분석 여러 건 → 인물 외모를 고정하는 텍스트(모든 샷에 삽입)",
        "kind": "text",
        "modelEnv": "STORY:video.character_sheet",
        "backendRefs": [
            ("prompts_story", "BASE_RULES", "공통 규칙"),
            ("prompts_story", "CHARACTER_SHEET", "외모 기술 규칙과 출력 스키마"),
        ],
        "frontendHint": (
            "외모에서 특히 놓치지 말 것을 적습니다. 예: 왼쪽 눈 아래 점을 반드시 남긴다. "
            "레퍼런스에 없는 특징을 만들라고 적지 마세요 — 모든 장면에 퍼집니다."
        ),
    },
    {
        "id": "story.summary",
        "label": "요약 스토리 생성",
        "usage": "실제 스토리 → 300자 요약(이미지 생성 참고용)",
        "kind": "text",
        "modelEnv": "STORY:story.summary",
        "backendRefs": [("prompts_story", "SUMMARIZE", "요약 규칙과 300자 상한")],
        "frontendHint": "요약에 반드시 남길 정보를 적습니다. 예: 시대 배경을 항상 포함한다.",
    },
]

_ROLES_BY_ID = {role["id"]: role for role in ROLES}

_ROLE_REQUIRED_FIELDS = frozenset(
    {"id", "label", "usage", "kind", "modelEnv", "backendRefs", "frontendHint"}
)


def register_roles(roles: list[dict]) -> None:
    """Register product-owned purpose descriptors atomically and idempotently.

    Only descriptors passed to this extension point are validated. Existing static entries are not
    rewritten or revalidated, and optional descriptor fields remain allowed.
    """
    incoming: dict[str, dict] = {}
    for source in roles:
        if not isinstance(source, dict):
            raise PromptError("등록할 prompt role은 객체여야 합니다.")
        missing = sorted(_ROLE_REQUIRED_FIELDS - set(source))
        role_id = source.get("id")
        if missing or not isinstance(role_id, str) or not role_id.strip():
            detail = ", ".join(missing) if missing else "id"
            raise PromptError(f"prompt role 필드가 올바르지 않습니다: {detail}")
        candidate = copy.deepcopy(source)
        previous = incoming.get(role_id)
        if previous is not None and previous != candidate:
            raise PromptError(f"한 등록 요청에 서로 다른 '{role_id}' 정의가 있습니다.")
        incoming[role_id] = candidate

    with _lock:
        conflicts = [
            role_id
            for role_id, role in incoming.items()
            if role_id in _ROLES_BY_ID and _ROLES_BY_ID[role_id] != role
        ]
        if conflicts:
            raise PromptError(
                "이미 다른 정의로 등록된 prompt role: " + ", ".join(sorted(conflicts))
            )
        for role_id, role in incoming.items():
            if role_id in _ROLES_BY_ID:
                continue
            ROLES.append(role)
            _ROLES_BY_ID[role_id] = role


def role_ids() -> list[str]:
    return [role["id"] for role in ROLES]


def get_role(role_id: str) -> dict | None:
    return _ROLES_BY_ID.get(role_id)


def is_editable(role_id: str) -> bool:
    role = _ROLES_BY_ID.get(role_id)
    return bool(role) and role.get("frontendEditable", True)


# --------------------------------------------------------------------------- #
# 프론트엔드 프롬프트 저장 (S3)
# --------------------------------------------------------------------------- #
def _read_store() -> dict:
    try:
        response = _s3.get_object(Bucket=S3_BUCKET, Key=CONFIG_KEY)
        payload = json.loads(response["Body"].read())
    except ClientError as error:
        code = error.response.get("Error", {}).get("Code")
        if code in {"NoSuchKey", "404", "AccessDenied"}:
            return {}
        raise
    except (BotoCoreError, json.JSONDecodeError) as error:
        # 저장소를 못 읽어도 생성은 계속되어야 한다. 프론트엔드 프롬프트는 없어도 되는 계층이다.
        logger.warning("프론트엔드 프롬프트를 읽지 못했습니다: %s", error)
        return {}
    prompts = payload.get("prompts") if isinstance(payload, dict) else None
    return prompts if isinstance(prompts, dict) else {}


def load(force: bool = False) -> dict:
    """{roleId: {text, updatedAt, updatedBy}} 형태."""
    now = datetime.now(timezone.utc).timestamp()
    with _lock:
        cached = _cache.get("prompts")
        if cached is not None and not force and now - _cache["loaded_at"] < CACHE_TTL_SECONDS:
            return copy.deepcopy(cached)
    # 네트워크 호출은 락 밖에서 한다. 락을 들고 S3를 부르면 모든 요청이 줄을 선다.
    fetched = _read_store()
    with _lock:
        _cache.update({"prompts": fetched, "loaded_at": now})
        return copy.deepcopy(fetched)


def fragment(role_id: str) -> str:
    """이 역할의 프론트엔드 프롬프트. 없으면 빈 문자열.

    호출 경로에서 쓰는 함수라 예외를 던지지 않는다. 프론트엔드 프롬프트가 없는 것은
    정상 상태이고(기본값이 빈 문자열), 저장소 장애도 생성을 막아서는 안 된다.
    """
    if not is_editable(role_id):
        return ""
    try:
        entry = load().get(role_id) or {}
    except (BotoCoreError, ClientError) as error:
        logger.warning("프론트엔드 프롬프트 조회 실패 role=%s: %s", role_id, error)
        return ""
    text = str(entry.get("text") or "").strip()
    return text[:MAX_FRONTEND_CHARS]


def save(role_id: str, text: str, actor: str) -> dict:
    """Admin이 프론트엔드 프롬프트를 저장한다. 백엔드 프롬프트는 이 경로로 바뀌지 않는다."""
    role = _ROLES_BY_ID.get(role_id)
    if not role:
        raise PromptError(f"'{role_id}'는 없는 용도입니다.")
    if not role.get("frontendEditable", True):
        # 지시서 6-2. 이 역할은 백엔드 고정만 있고 편집 대상이 없다.
        raise PromptError(
            f"'{role['label']}'은 배포로만 변경할 수 있습니다. 화면에서 수정할 수 없습니다."
        )

    value = str(text or "").strip()
    if len(value) > MAX_FRONTEND_CHARS:
        raise PromptError(
            f"프론트엔드 프롬프트는 {MAX_FRONTEND_CHARS:,}자 이하여야 합니다. 지금 {len(value):,}자입니다."
        )

    store = _read_store()
    if value:
        store[role_id] = {
            "text": value,
            "updatedAt": datetime.now(timezone.utc).isoformat(),
            "updatedBy": actor,
        }
    else:
        # 빈 값은 '기본값으로 되돌리기'다. 빈 문자열을 저장해 두면 이력이 지저분해진다.
        store.pop(role_id, None)

    body = json.dumps(
        {
            "version": 1,
            "updatedAt": datetime.now(timezone.utc).isoformat(),
            "updatedBy": actor,
            "prompts": store,
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
        _cache.update({"prompts": None, "loaded_at": 0.0})
    logger.info("프론트엔드 프롬프트 저장 role=%s by=%s chars=%s", role_id, actor, len(value))
    return store.get(role_id) or {"text": "", "updatedAt": "", "updatedBy": ""}


def history(role_id: str, limit: int = 10) -> list[dict]:
    """수정 이력. S3 객체 버전이 이력이다(버킷 버저닝이 켜져 있다).

    별도 이력 테이블을 만들지 않은 이유는 버저닝이 이미 같은 일을 하고 있고,
    두 곳에 이력을 두면 어긋날 수 있기 때문이다.
    """
    try:
        listed = _s3.list_object_versions(Bucket=S3_BUCKET, Prefix=CONFIG_KEY, MaxKeys=60)
    except (BotoCoreError, ClientError) as error:
        logger.warning("프롬프트 이력을 읽지 못했습니다: %s", error)
        return []

    versions = [
        version
        for version in (listed.get("Versions") or [])
        if version.get("Key") == CONFIG_KEY
    ]
    versions.sort(key=lambda item: item.get("LastModified") or datetime.min, reverse=True)

    rows: list[dict] = []
    seen: set[str] = set()
    for version in versions[:limit * 3]:
        if len(rows) >= limit:
            break
        try:
            obj = _s3.get_object(
                Bucket=S3_BUCKET, Key=CONFIG_KEY, VersionId=version["VersionId"]
            )
            payload = json.loads(obj["Body"].read())
        except (BotoCoreError, ClientError, json.JSONDecodeError):
            continue
        entry = (payload.get("prompts") or {}).get(role_id) or {}
        text = str(entry.get("text") or "")
        # 이 역할이 바뀌지 않은 버전은 건너뛴다. 다른 역할을 고친 저장까지 이력에 뜨면
        # 무엇이 바뀐 것인지 알 수 없다.
        if text in seen:
            continue
        seen.add(text)
        rows.append(
            {
                "text": text,
                "updatedAt": entry.get("updatedAt") or payload.get("updatedAt") or "",
                "updatedBy": entry.get("updatedBy") or payload.get("updatedBy") or "",
                "versionId": version["VersionId"],
                "savedAt": (version.get("LastModified") or datetime.min).isoformat(),
            }
        )
    return rows


# --------------------------------------------------------------------------- #
# 결합 (지시서 5-4 / 6-3)
# --------------------------------------------------------------------------- #
# 프론트엔드 조각을 감싸는 라벨. 모델이 이것을 백엔드 규칙과 같은 무게로 읽지 않도록
# '운영 지침'이라고 명시하고, 충돌 시 무엇을 따라야 하는지 못박는다.
_FRONTEND_HEADER = "[운영 지침 (관리자 설정)]"
_PRECEDENCE_NOTE = (
    "위 시스템 규칙과 이 운영 지침이 충돌하면 **시스템 규칙을 따릅니다.** "
    "운영 지침은 시스템 규칙이 허용하는 범위 안에서만 적용합니다."
)


def compose_system(role_id: str, backend_text: str) -> str:
    """시스템 프롬프트에 프론트엔드 프롬프트를 덧붙인다.

    순서가 요구사항이다(5-4). 백엔드가 먼저 오고 프론트엔드가 뒤에 온다.
    사용자 입력은 messages 로 들어가므로 항상 그 뒤다.

    백엔드가 최상위라는 것을 순서만으로 보장할 수 없어서(모델은 뒤에 온 지시를 더
    강하게 따르는 경향이 있다) 우선순위 문장을 함께 넣는다.
    """
    extra = fragment(role_id)
    if not extra:
        return backend_text
    return f"{backend_text}\n\n{_FRONTEND_HEADER}\n{extra}\n\n{_PRECEDENCE_NOTE}"


def compose_body(role_id: str, backend_prompt: str, max_chars: int = 0) -> str:
    """이미지·영상처럼 system 개념이 없는 모델의 프롬프트 문자열에 덧붙인다.

    텍스트 모델과 달리 우선순위 문장을 넣지 않는다. 확산 모델에 메타 지시를 넣으면
    그 문장 자체를 그림으로 그린다. 대신 조각을 **뒤에** 붙여 앞쪽 토큰(매체 선언)의
    비중을 건드리지 않는다. styles.compose_prompt 가 매체를 맨 앞에 두는 이유와 같다.

    `max_chars` 는 어댑터가 이미 잘라 둔 길이를 다시 넘지 않게 하는 상한이다.
    providers 의 각 어댑터는 본문을 만들 때 프롬프트를 4,000~9,000자로 자른다. 그 뒤에
    조각을 덧붙이면 상한을 넘어 모델이 요청을 거부할 수 있다.

    넘칠 때 **자르는 쪽은 조각(프론트엔드)이다.** 백엔드 프롬프트가 최상위라는 규칙
    (지시서 6-3)을 길이 처리에서도 지킨다. 백엔드를 자르면 화풍 선언이나 부정 프롬프트가
    통째로 사라져 결과가 무너진다.
    """
    extra = fragment(role_id)
    if not extra:
        return backend_prompt
    joined = backend_prompt.rstrip().rstrip(",.")
    if max_chars and len(joined) + len(extra) + 2 > max_chars:
        room = max_chars - len(joined) - 2
        if room <= 0:
            # 백엔드만으로 이미 상한이라 조각을 넣을 자리가 없다. 조용히 버리지 않고 알린다.
            logger.warning(
                "프론트엔드 프롬프트를 넣을 자리가 없어 건너뜁니다 role=%s backend=%s max=%s",
                role_id,
                len(joined),
                max_chars,
            )
            return backend_prompt
        logger.info("프론트엔드 프롬프트를 %s자로 줄였습니다 role=%s", room, role_id)
        extra = extra[:room]
    return f"{joined}, {extra}"


# --------------------------------------------------------------------------- #
# 백엔드 프롬프트 원문 수집 (Admin 조회 / 매핑표)
# --------------------------------------------------------------------------- #
def _resolve_ref(module_name: str, symbol: str) -> str:
    """참조를 실제 문자열로 바꾼다. 원문을 이 파일에 복사하지 않기 위한 장치.

    호출 경로에서는 쓰지 않으므로 여기서 늦게 임포트한다(순환 임포트 회피).
    """
    # 소스 파일에만 있고 파이썬 심볼이 아닌 것들. 설명만 돌려준다.
    if module_name.startswith("video-gen/"):
        return f"(배포 시점에 이미지에 주입: {module_name} 의 {symbol})"
    if module_name == "server" and symbol.endswith("지시문"):
        return f"(server.py 의 {symbol})"

    try:
        module = __import__(f"{__package__}.{module_name}", fromlist=[module_name])
    except ImportError as error:
        logger.warning("백엔드 프롬프트 참조 실패 %s: %s", module_name, error)
        return f"(참조 실패: {module_name}.{symbol})"

    # KIND_SPECS[analysis] / _RULES[scene] 처럼 첨자가 붙은 형태를 지원한다.
    name, _, index = symbol.partition("[")
    value = getattr(module, name, None)
    if value is None:
        return f"(없음: {module_name}.{name})"
    if index:
        key = index.rstrip("]")
        try:
            value = value[key]
        except (KeyError, TypeError, IndexError):
            return f"(없음: {module_name}.{name}[{key}])"

    if isinstance(value, str):
        return value
    if callable(value):
        doc = (getattr(value, "__doc__", "") or "").strip()
        return f"(함수 {module_name}.{name})" + (f"\n{doc}" if doc else "")
    try:
        return json.dumps(value, ensure_ascii=False, indent=2, default=str)
    except (TypeError, ValueError):
        return str(value)


def inventory(include_text: bool = True, force: bool = False) -> list[dict]:
    """역할별 백엔드/프론트엔드 프롬프트 현황.

    Admin 화면과 매핑표(지시서 8-2)가 같은 함수를 쓴다. 두 곳이 따로 계산하면
    화면과 보고서가 어긋난다.

    `force=True` 는 캐시를 건너뛴다. **관리자 조회 경로는 항상 이것을 써야 한다.**

    캐시는 프로세스마다 따로 있다(파드 2개 × gunicorn 워커 2개 = 4벌). 저장은 한 워커에서
    일어나고 그 워커만 자기 캐시를 비운다. 그래서 저장 직후 다른 워커가 조회를 받으면
    최대 30초 동안 옛 값을 돌려준다. 실제로 검증에서 저장 직후 재조회가 빈 값을 줬다.
    관리자는 저장이 실패한 줄 알고 다시 저장하게 된다.

    생성 경로(fragment)는 캐시를 그대로 쓴다. 모델 호출마다 S3 를 부르는 비용이 크고,
    그쪽은 30초 지연이 결과에 미치는 영향이 없다.
    """
    stored = load(force=force)
    rows: list[dict] = []
    for role in ROLES:
        blocks = []
        for module_name, symbol, note in role["backendRefs"]:
            block = {"source": f"{module_name}.{symbol}", "note": note}
            if include_text:
                block["text"] = _resolve_ref(module_name, symbol)
            blocks.append(block)

        entry = stored.get(role["id"]) or {}
        editable = role.get("frontendEditable", True)
        rows.append(
            {
                "role": role["id"],
                "label": role["label"],
                "usage": role["usage"],
                "kind": role["kind"],
                "modelEnv": role["modelEnv"],
                "modelId": model_for(role["modelEnv"]),
                "backend": blocks,
                "hasBackend": bool(blocks),
                "frontendEditable": editable,
                "frontendHint": role.get("frontendHint", ""),
                "frontend": str(entry.get("text") or ""),
                "frontendDefault": "",
                "updatedAt": entry.get("updatedAt") or "",
                "updatedBy": entry.get("updatedBy") or "",
            }
        )
    return rows


def mapping_table(force: bool = True) -> list[dict]:
    """지시서 8-2가 요구하는 표: 모델명 | 용도 | 백엔드 프롬프트 유무 | 프론트엔드 기본값.

    기본이 force=True 다. 보고서에 넣는 표라 옛 값이 실리면 안 된다.
    """
    return [
        {
            "modelId": row["modelId"],
            "modelEnv": row["modelEnv"],
            "kind": row["kind"],
            "role": row["role"],
            "usage": f"{row['label']} — {row['usage']}",
            "hasBackendPrompt": row["hasBackend"],
            "backendSources": [block["source"] for block in row["backend"]],
            "frontendDefault": row["frontendDefault"],
            "frontendEditable": row["frontendEditable"],
            "frontendSet": bool(row["frontend"]),
        }
        for row in inventory(include_text=False, force=force)
    ]


def missing_backend() -> list[str]:
    """백엔드 프롬프트가 없는 역할. 지시서 8-2는 이것이 비어 있어야 한다고 요구한다.

    이 판단은 ROLES 정의만 보므로 저장소를 읽을 필요가 없다. 캐시된 값으로 충분하다.
    """
    return [row["role"] for row in inventory(include_text=False) if not row["hasBackend"]]
