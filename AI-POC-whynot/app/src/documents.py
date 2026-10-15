"""웹툰 IP를 드라마로 옮기기 위한 AI 산출물 생성.

산출물마다 출력 스키마를 고정하고 JSON으로 받는다. 마크다운 대신 JSON을 쓰는 이유는
뒷단계가 앞단계 결과를 프로그램으로 다시 써야 하기 때문이다.
예를 들어 대본 단계는 인물 목록을, 콘티 단계는 주요 씬 목록을 그대로 받아야 한다.

모델은 DOC_TEXT_MODEL로 교체할 수 있다. 코드에는 모델 계열을 가정한 분기를 두지 않는다.
"""

import json
import logging
import os
import re

# 화수 기본값은 기획 방향과 같은 값을 써야 한다. 상수를 복제하지 않고 가져온다.
from . import video_prompt
from .graph_store import DIRECTION_DEFAULTS

logger = logging.getLogger("toon-change-ai-pj-app.documents")

AWS_REGION = os.getenv("AWS_REGION", "ap-northeast-2")
TEXT_MODEL = os.getenv("DOC_TEXT_MODEL", "global.anthropic.claude-sonnet-4-5-20250929-v1:0")
# 원본 자료가 길면 앞부분만 넣는다. 회차 순서가 앞에서부터 중요한 자료 특성을 따른다.
MAX_SOURCE_CHARS = int(os.getenv("DOC_MAX_SOURCE_CHARS", "24000"))
# 앞 단계 산출물을 맥락으로 넣을 때 문서 하나당 상한.
MAX_CONTEXT_CHARS_PER_DOC = int(os.getenv("DOC_MAX_CONTEXT_CHARS", "8000"))
# 화별 시놉시스를 한 번에 몇 화씩 쓸지. 너무 크게 잡으면 출력이 잘린다.
EPISODE_BATCH_SIZE = int(os.getenv("DOC_EPISODE_BATCH_SIZE", "4"))

# --- 트리트먼트 분량 ---
# A4 한 장에 들어가는 한국어 분량. 참고 자료로 받은 실제 트리트먼트
# (변호인 트리트먼트 1.5고, 54장 60,290자)를 페이지별로 세어 나온 값이다.
# 페이지 평균 1,116자였고 표지·에필로그 같은 짧은 장을 빼면 1,100~1,250자 범위였다.
CHARS_PER_A4_PAGE = int(os.getenv("DOC_CHARS_PER_A4", "1100"))
# PoC 범위. 10장을 넘기면 한 번에 쓰기도 어렵고 기획 회의에서 검토하기도 어렵다.
TREATMENT_MAX_PAGES = int(os.getenv("DOC_TREATMENT_MAX_PAGES", "10"))
TREATMENT_DEFAULT_PAGES = int(os.getenv("DOC_TREATMENT_DEFAULT_PAGES", "4"))
# 기획자가 적는 세부사항 상한. 프롬프트 전체를 밀어내지 않을 정도로 잡는다.
TREATMENT_MAX_DETAILS_CHARS = 2000


class DocumentError(RuntimeError):
    """생성 실패. 사용자에게 보여줄 수 있는 메시지를 담는다."""


# --------------------------------------------------------------------------- #
# 트리트먼트 본문 구성
# --------------------------------------------------------------------------- #
# 참고 자료로 받은 실제 트리트먼트의 구성을 따른다.
# 그 문서는 로그라인 -> 등장인물 -> 시놉시스 -> 씬 단위 트리트먼트 순서였고,
# 기획자가 요구한 필수 구성은 서론 / 등장인물 소개 / 주요 사건 / 클라이맥스 / 결말 다섯이다.
#
# weight: 전체 페이지 예산을 섹션에 나누는 비율. 합이 1.0이다.
#   주요 사건이 가장 두꺼운 것은 실제 트리트먼트에서 전개부가 문서의 대부분을 차지하기 때문이다.
_TREATMENT_SECTIONS = (
    {
        "id": "intro",
        "label": "서론",
        "weight": 0.12,
        "guide": (
            "작품이 어떤 세계에서 시작하는지, 주인공이 어떤 상태에 놓여 있는지, "
            "무엇이 균형을 깨뜨리는지 서술한다. 기획 의도를 설명하지 말고 이야기로 쓴다."
        ),
    },
    {
        "id": "characterIntro",
        "label": "등장인물 소개",
        "weight": 0.20,
        "guide": (
            "인물마다 이름과 나이를 앞에 두고 줄을 나눠 소개한다. 겉으로 드러난 처지, "
            "숨은 욕망, 다른 인물과의 관계를 적는다. 성격 형용사를 늘어놓지 않고 행동으로 드러낸다."
        ),
    },
    {
        "id": "mainEvents",
        "label": "주요 사건",
        "weight": 0.36,
        "guide": (
            "이야기가 굴러가는 사건을 시간 순서대로 서술한다. 사건마다 누가 무엇을 하고 "
            "그 결과로 상황이 어떻게 바뀌는지 인과가 보이게 쓴다. 요약이 아니라 장면 서술로 쓴다."
        ),
    },
    {
        "id": "climax",
        "label": "클라이맥스",
        "weight": 0.20,
        "guide": (
            "갈등이 가장 높아지는 지점을 서술한다. 주인공이 무엇을 걸고 어떤 선택을 하는지, "
            "그 선택이 왜 되돌릴 수 없는지 적는다."
        ),
    },
    {
        "id": "ending",
        "label": "결말",
        "weight": 0.12,
        "guide": (
            "갈등이 해소된 뒤 인물들이 어디에 서 있는지 서술한다. 주인공이 처음과 어떻게 "
            "달라졌는지 드러낸다. 열린 결말이면 무엇이 남았는지 적는다."
        ),
    },
)

# 한 번에 다 쓰게 하면 10장 분량에서 출력이 잘린다. 무게를 비슷하게 나눠 배치로 만든다.
# 주요 사건은 혼자서도 전체의 36%라 단독 배치다.
_TREATMENT_SECTION_BATCHES = (("intro", "characterIntro"), ("mainEvents",), ("climax", "ending"))

# 상세도. 페이지 수가 '얼마나 길게'를 정하고, 상세도가 '얼마나 파고들지'를 정한다.
# 둘을 하나로 묶지 않은 이유: 짧지만 촘촘한 문서와 길지만 개괄적인 문서가 모두 필요하다.
DOC_DETAIL_BRIEF = "brief"
DOC_DETAIL_NORMAL = "normal"
DOC_DETAIL_FULL = "full"
_TREATMENT_DETAIL_RULES = {
    DOC_DETAIL_BRIEF: {
        "label": "개요",
        "hint": "문단 서술만. 장면을 나누지 않는다.",
        "rule": (
            "각 섹션을 이어지는 문단으로만 쓴다. 장면 번호나 목록을 만들지 않는다. "
            "대사는 넣지 않는다."
        ),
    },
    DOC_DETAIL_NORMAL: {
        "label": "보통",
        "hint": "문단 서술 + 섹션별 핵심 장면 목록.",
        "rule": (
            "각 섹션을 문단으로 쓰고, 그 섹션의 핵심 장면을 keyBeats에 3~5개 적는다. "
            "대사는 꼭 필요한 곳에만 한두 줄 넣는다."
        ),
    },
    DOC_DETAIL_FULL: {
        "label": "상세",
        "hint": "문단 서술 + 장면 목록 + 대사 예시.",
        "rule": (
            "각 섹션을 문단으로 쓰고, keyBeats에 장면을 5~8개 적는다. "
            "인물의 성격이 드러나는 대사를 실제 문장으로 인용해 본문에 섞는다. "
            "공간과 시간대를 구체적으로 적는다."
        ),
    },
}
DOC_DETAILS = (DOC_DETAIL_BRIEF, DOC_DETAIL_NORMAL, DOC_DETAIL_FULL)


def treatment_details() -> list[dict]:
    """프론트가 상세도 선택 UI를 그릴 때 쓴다."""
    return [
        {"id": key, "label": value["label"], "hint": value["hint"]}
        for key, value in _TREATMENT_DETAIL_RULES.items()
    ]


def treatment_page_choices() -> list[dict]:
    """A4 몇 장으로 만들지 선택지. 실측 페이지당 글자 수로 예상 분량을 함께 준다."""
    return [
        {"value": pages, "label": f"{pages}장", "estimateChars": pages * CHARS_PER_A4_PAGE}
        for pages in (2, 4, 6, 8, TREATMENT_MAX_PAGES)
    ]


def normalize_treatment_options(raw: dict | None) -> dict:
    """요청에서 온 옵션을 검증해 정규화한다. 잘못된 값은 DocumentError로 알린다.

    서버 라우트와 생성기가 같은 규칙을 쓰도록 여기 한 곳에 둔다.
    """
    raw = raw or {}

    pages_raw = raw.get("pages")
    if pages_raw in (None, ""):
        pages = TREATMENT_DEFAULT_PAGES
    else:
        try:
            pages = int(pages_raw)
        except (TypeError, ValueError):
            raise DocumentError("A4 장수가 올바르지 않습니다.")
        if not 1 <= pages <= TREATMENT_MAX_PAGES:
            raise DocumentError(f"A4 장수는 1~{TREATMENT_MAX_PAGES} 사이여야 합니다.")

    detail = str(raw.get("detail") or DOC_DETAIL_NORMAL)
    if detail not in _TREATMENT_DETAIL_RULES:
        raise DocumentError("상세도가 올바르지 않습니다.")

    details_text = str(raw.get("details") or "").strip()
    if len(details_text) > TREATMENT_MAX_DETAILS_CHARS:
        raise DocumentError(f"세부사항은 {TREATMENT_MAX_DETAILS_CHARS:,}자 이내로 적어 주세요.")

    return {"pages": pages, "detail": detail, "details": details_text}


# --------------------------------------------------------------------------- #
# 산출물 정의
# --------------------------------------------------------------------------- #
# schema: 모델에게 그대로 보여주는 출력 형태. 값은 각 필드의 작성 지침이다.
KIND_SPECS: dict[str, dict] = {
    "analysis": {
        "label": "영상화 가능성 분석",
        "goal": "원작을 드라마로 만들 수 있는지 판단하고, 어떻게 하면 되는지 추천안을 제시한다.",
        "inputTypes": ("webtoon",),
        "requiresSource": True,
        "maxTokens": 6000,
        "limits": "strengths, risks, productionNotes, comparableWorks는 각각 3~4개. 각 detail은 3문장 이내.",
        "schema": {
            "verdict": "적합 / 조건부 적합 / 부적합 중 하나",
            "score": "영상화 적합도 0~100 정수",
            "summary": "판단 근거를 담은 3~4문장 총평",
            "strengths": [{"title": "강점 제목", "detail": "2~3문장 설명, 원작의 구체적 요소를 근거로"}],
            "risks": [{"title": "위험 요소", "detail": "무엇이 문제인지", "mitigation": "어떻게 해결할지"}],
            "formatRecommendation": {
                "format": "예: 미니시리즈 8부작",
                "runtime": "예: 회당 45분",
                "platform": "적합한 플랫폼과 이유",
                "reason": "이 형식을 추천하는 이유 2~3문장",
            },
            "productionNotes": [{"title": "제작 고려사항", "detail": "예산, 로케이션, VFX, 캐스팅 난이도 등"}],
            "comparableWorks": [{"title": "비교 작품", "why": "어떤 점이 비슷하고 무엇을 참고할지"}],
        },
    },
    "adaptation": {
        "label": "각색 방향 제안",
        "goal": "원작을 어떤 방향으로 각색할지 서로 다른 선택지를 제시하고 하나를 추천한다.",
        "inputTypes": ("webtoon",),
        "requiresSource": True,
        "maxTokens": 7000,
        "limits": "options는 서로 뚜렷하게 다른 2~3개. keeps/changes/pros/cons는 각각 3개 이내. keyScenes는 3~4개.",
        "schema": {
            "summary": "각색의 핵심 판단을 담은 2~3문장",
            "options": [
                {
                    "name": "방향 이름 (예: 원작 충실형)",
                    "logline": "이 방향으로 갔을 때의 한 문장 로그라인",
                    "approach": "이 방향의 전략 3~4문장",
                    "keeps": ["원작에서 반드시 살릴 요소"],
                    "changes": ["바꾸거나 덜어낼 요소와 그 이유"],
                    "pros": ["장점"],
                    "cons": ["단점 또는 감수해야 할 것"],
                    "fit": "타겟 플랫폼과 시청층에 얼마나 맞는지",
                }
            ],
            "recommended": "options 중 추천하는 방향의 name과 동일한 문자열",
            "recommendReason": "추천 이유 3~4문장",
            "toneAndManner": "연출 톤앤매너 구체적으로 3~4문장",
            "keyScenes": [{"title": "영상으로 반드시 살려야 할 장면", "why": "이유"}],
        },
    },
    "logline": {
        "label": "로그라인 구체화",
        "goal": "짧은 소재나 로그라인을 기획서 수준으로 구체화한다.",
        "inputTypes": ("logline",),
        "requiresSource": True,
        "maxTokens": 6000,
        "limits": "genreMix 2~3개, worldSetting.rules 3~4개, hooks 3~4개, openQuestions 3~4개.",
        "schema": {
            "logline": "정제된 한 문장 로그라인. 주인공, 목표, 장애, 훅이 모두 보이게",
            "premise": "프리미스 4~5문장",
            "theme": "작품이 다루는 주제 한 문장",
            "genreMix": ["장르 조합"],
            "worldSetting": {
                "time": "시대와 시간 배경",
                "place": "공간 배경",
                "rules": ["이 세계의 규칙이나 설정"],
            },
            "conflict": {
                "external": "외적 갈등",
                "internal": "주인공의 내적 갈등",
                "antagonistForce": "적대 세력 또는 대립 축",
            },
            "hooks": [{"title": "시청자를 붙잡는 훅", "detail": "왜 궁금해지는지"}],
            "seasonArc": "시즌 전체 흐름 4~5문장. 시작과 끝이 어디인지",
            "openQuestions": ["기획자가 결정해야 할 남은 선택지"],
        },
    },
    "characters": {
        "label": "인물 설정",
        "goal": "주요 인물의 성격, 말투, 배경, 관계를 대본을 쓸 수 있는 수준으로 정한다.",
        "inputTypes": ("webtoon", "logline"),
        "requiresSource": True,
        "maxTokens": 10000,
        "limits": (
            "characters는 주요 인물 4~5명만. 주인공과 대립자를 먼저 쓰고, 주변인은 꼭 필요한 경우만 넣는다. "
            "personality 3~4개, signaturePhrases 3개, relationships 2~3개. background는 3문장 이내."
        ),
        "schema": {
            "summary": "인물 구도를 설명하는 2~3문장",
            "characters": [
                {
                    "name": "이름",
                    "role": "주인공 / 대립자 / 조력자 / 주변인 중 하나",
                    "age": "나이 또는 연령대",
                    "occupation": "직업이나 신분",
                    "personality": ["성격 키워드와 짧은 설명"],
                    "desire": "겉으로 원하는 것",
                    "need": "실제로 필요한 것",
                    "wound": "과거의 상처나 결핍",
                    "fear": "가장 두려워하는 것",
                    "arc": "1화부터 마지막화까지의 변화",
                    "speechStyle": "말투 특징. 문장 길이, 존댓말 여부, 습관적 표현, 감정이 드러나는 방식",
                    "signaturePhrases": ["이 인물이 실제로 할 것 같은 대사 예시"],
                    "background": "배경 서사 3~4문장",
                    "relationships": [{"with": "상대 인물 이름", "type": "관계", "detail": "관계의 긴장 또는 결속"}],
                    "castingDirection": "캐스팅 방향 한두 문장",
                }
            ],
        },
    },
    "treatment": {
        "label": "트리트먼트",
        "goal": (
            "서론, 등장인물 소개, 주요 사건, 클라이맥스, 결말을 갖춘 트리트먼트 본문을 쓰고 "
            "시즌 구조와 화별 시놉시스까지 완성한다."
        ),
        # 대본을 이미 가진 프로젝트에서도 트리트먼트를 만든다. 대본에서 트리트먼트를 거꾸로
        # 뽑아 기획 자료로 쓰는 경우가 실제로 있다(투자·편성 검토용).
        "inputTypes": ("webtoon", "logline", "script"),
        "requiresSource": True,
        # 본문·설계·화별 시놉시스를 나눠 생성한다(_generate_treatment). 이 값은 설계 단계 기준이다.
        "maxTokens": 6000,
        "contextKinds": ("adaptation", "logline", "characters", "analysis"),
        "limits": "themes 2~3개, mainCharacters 4~5명, productionNotes 3~4개.",
        # 생성 시점에 기획자가 고르는 값. describe_kinds()가 이 선언을 프론트로 내보낸다.
        "options": {
            "pages": {
                "type": "int",
                "label": "A4 분량",
                "hint": f"본문을 A4 몇 장으로 쓸지 정합니다 (최대 {TREATMENT_MAX_PAGES}장).",
            },
            "detail": {"type": "choice", "label": "상세도", "hint": "얼마나 파고들지 정합니다."},
            "details": {
                "type": "text",
                "label": "세부사항",
                "hint": "꼭 반영할 내용을 적으면 본문에 반영합니다.",
            },
        },
        "schema": {
            "title": "작품 제목 (가제여도 무방)",
            "logline": "한 문장 로그라인",
            "overview": "기획 의도와 전체 개요 4~5문장",
            "toneAndManner": "연출 톤앤매너 2~3문장",
            "format": {
                "episodeCount": "총 화수 정수",
                "runtime": "회당 분량",
                "platform": "타겟 플랫폼",
            },
            "themes": ["작품이 다루는 주제"],
            "mainCharacters": [
                {
                    "name": "이름",
                    "role": "주인공 / 대립자 / 조력자 / 주변인",
                    "summary": "한 줄 소개",
                    "arc": "시즌 전체에서의 변화",
                }
            ],
            "structure": {
                "act1": "1막(도입)에서 벌어지는 일과 끝나는 지점",
                "act2": "2막(전개와 위기)의 흐름",
                "act3": "3막(절정과 해소)의 흐름",
            },
            # 기획자가 실제로 인쇄해서 읽는 본문. 섹션 구성과 분량은 옵션이 정한다.
            "narrative": {
                section["id"]: f"{section['label']} 본문" for section in _TREATMENT_SECTIONS
            },
            "episodes": [
                {
                    "number": "화 번호 정수",
                    "title": "부제",
                    "synopsis": "5~7문장 시놉시스. 무슨 일이 일어나고 어떻게 끝나는지 구체적으로",
                    "keyScenes": ["이 화의 핵심 장면"],
                    "hook": "다음 화를 보게 만드는 마지막 지점",
                    "characterFocus": ["이 화에서 중심이 되는 인물 이름"],
                }
            ],
            "seasonEnding": "시즌 마지막 상태와 다음 시즌 가능성 2~3문장",
            "productionNotes": ["제작 시 고려할 점"],
        },
    },
}

SOURCE_LABELS = {
    "webtoon": "웹툰 원작(또는 원작 소설) 내용",
    "logline": "소재 / 로그라인 메모",
    "treatment": "트리트먼트",
    "script": "미리 논의된 대본",
}

BASE_RULES = (
    "당신은 웹툰 IP를 드라마로 옮기는 국내 제작사의 기획 개발 담당자입니다. "
    "실제 기획 회의에 올릴 문서를 쓴다고 생각하고 작성하세요.\n"
    "규칙:\n"
    "1. 반드시 한국어로 작성합니다. 인물 이름은 원본 자료의 표기를 그대로 씁니다.\n"
    "2. 원본 자료에 있는 사실과 충돌하는 내용을 만들지 않습니다. "
    "자료에 없어 추론한 부분은 자연스럽게 제안 형태로 씁니다.\n"
    "3. 일반론이나 교과서적 조언을 쓰지 않습니다. 이 작품에만 해당하는 구체적인 판단을 씁니다.\n"
    "4. 기획 방향이 주어지면 그 지시를 우선합니다.\n"
    "5. 빈 값이나 '미정' 같은 회피성 답변을 넣지 않습니다. 판단이 어려우면 근거와 함께 가정을 밝힙니다.\n"
    "6. 출력은 지정된 JSON 하나만 씁니다. 설명, 인사말, 코드펜스를 붙이지 않습니다."
)


def allowed_kinds(input_type: str) -> list[str]:
    """이 진입점에서 만들 수 있는 산출물 종류."""
    return [kind for kind, spec in KIND_SPECS.items() if input_type in spec["inputTypes"]]


def spec_for(kind: str) -> dict:
    spec = KIND_SPECS.get(kind)
    if not spec:
        raise DocumentError("산출물 종류가 올바르지 않습니다.")
    return spec


def describe_kinds() -> list[dict]:
    """프론트가 단계 목록과 생성 옵션 UI를 그릴 때 쓰는 메타데이터.

    선택지·기본값을 프론트에 하드코딩하지 않는다. 값이 두 곳에 있으면 서버 검증과
    화면이 조용히 어긋난다(콘티는 같은 이유로 /api/storyboards/options 를 둔다).
    """
    items = []
    for kind, spec in KIND_SPECS.items():
        item = {
            "kind": kind,
            "label": spec["label"],
            "goal": spec["goal"],
            "inputTypes": list(spec["inputTypes"]),
            "requiresSource": spec["requiresSource"],
        }
        if kind == "treatment":
            item["options"] = {
                "labels": spec["options"],
                "pageChoices": treatment_page_choices(),
                "maxPages": TREATMENT_MAX_PAGES,
                "defaultPages": TREATMENT_DEFAULT_PAGES,
                "charsPerPage": CHARS_PER_A4_PAGE,
                "details": treatment_details(),
                "defaultDetail": DOC_DETAIL_NORMAL,
                "maxDetailsChars": TREATMENT_MAX_DETAILS_CHARS,
                "sections": [
                    {"id": section["id"], "label": section["label"]}
                    for section in _TREATMENT_SECTIONS
                ],
            }
        items.append(item)
    return items


# --------------------------------------------------------------------------- #
# 프롬프트
# --------------------------------------------------------------------------- #
def _direction_block(direction: dict) -> str:
    if not direction:
        return ""
    lines = []
    labels = [
        ("targetPlatform", "타겟 플랫폼"),
        ("genres", "장르"),
        ("episodeCount", "에피소드 수"),
        ("tone", "톤앤매너"),
        ("audience", "주 시청층"),
        ("freeDirection", "기획자 추가 지시"),
    ]
    for key, label in labels:
        value = direction.get(key)
        if isinstance(value, list):
            value = ", ".join(str(item) for item in value if str(item).strip())
        if value in (None, "", 0):
            continue
        if key == "episodeCount":
            value = f"{value}부작"
        lines.append(f"- {label}: {value}")
    if not lines:
        return ""
    return "기획 방향 (반드시 반영):\n" + "\n".join(lines)


CONTEXT_LABELS = {
    "analysis": "앞서 만든 영상화 가능성 분석",
    "adaptation": "앞서 만든 각색 방향 제안",
    "logline": "앞서 만든 로그라인 구체화",
    "characters": "앞서 만든 인물 설정",
    "treatment": "확정된 트리트먼트",
}


def _context_block(context: dict | None) -> str:
    """앞 단계 산출물을 그대로 붙인다.

    필드를 골라 요약하지 않는 이유는, 어떤 항목이 뒷단계에 필요한지 미리 정하기 어렵고
    잘못 고르면 정보가 조용히 사라지기 때문이다. 대신 문서별 길이만 제한한다.
    """
    if not context:
        return ""
    blocks = []
    for kind, payload in context.items():
        if not payload:
            continue
        body = json.dumps(payload, ensure_ascii=False, indent=2)[:MAX_CONTEXT_CHARS_PER_DOC]
        label = CONTEXT_LABELS.get(kind, f"앞 단계 산출물({kind})")
        blocks.append(f"[{label}]\n{body}")
    if not blocks:
        return ""
    return (
        "아래는 이 프로젝트에서 이미 확정된 내용입니다. 여기서 정한 인물, 설정, 방향과 "
        "충돌하지 않게 이어서 작성하세요.\n\n" + "\n\n".join(blocks)
    )


def _system_text(kind: str, extra_instruction: str = "") -> str:
    spec = spec_for(kind)
    schema = json.dumps(spec["schema"], ensure_ascii=False, indent=2)
    text = (
        f"{BASE_RULES}\n\n"
        f"[이번 작업] {spec['label']}\n목표: {spec['goal']}\n\n"
        f"[출력 형식] 아래 JSON 구조를 그대로 따르되, 각 필드의 값은 설명이 아니라 실제 내용으로 채웁니다.\n{schema}\n\n"
        f"[분량] {spec.get('limits', '각 배열은 3~4개 항목으로 씁니다.')}\n"
        "분량 제한을 넘겨 출력이 잘리면 결과를 쓸 수 없습니다. 반드시 JSON을 끝까지 닫아 주세요."
    )
    if extra_instruction:
        text += f"\n\n[추가 지시] {extra_instruction}"
    return text


def _project_block(project: dict, source_text: str, context: dict | None, source_limit: int) -> str:
    """작품 정보 + 기획 방향 + 앞 단계 결과 + 원본 자료를 담은 사용자 메시지."""
    parts = [f"[작품 제목]\n{project.get('title') or '(제목 미정)'}"]

    story = (project.get("story") or "").strip()
    if story:
        parts.append(f"[기획자가 적은 소개]\n{story}")

    direction_block = _direction_block(project.get("direction") or {})
    if direction_block:
        parts.append(direction_block)

    context_block = _context_block(context)
    if context_block:
        parts.append(context_block)

    if source_text:
        source_label = SOURCE_LABELS.get(project.get("inputType") or "webtoon", "원본 자료")
        parts.append(f"[{source_label}]\n{source_text[:source_limit]}")
        if len(source_text) > source_limit:
            parts.append(
                f"(원본 자료가 길어 앞부분 {source_limit:,}자만 제공했습니다. "
                "제공된 범위를 근거로 판단하고, 뒷부분 확인이 필요한 지점은 그렇게 밝혀 주세요.)"
            )
    return "\n\n".join(parts)


# --------------------------------------------------------------------------- #
# 생성
# --------------------------------------------------------------------------- #
_FENCE_PATTERN = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def extract_json(text: str) -> dict:
    """모델 응답에서 JSON 객체를 꺼낸다.

    코드펜스나 앞뒤 설명이 붙는 경우가 있어 중괄호 범위를 잘라 다시 시도한다.
    """
    if not text or not text.strip():
        raise DocumentError("모델이 빈 응답을 반환했습니다.")

    candidate = _FENCE_PATTERN.sub("", text.strip())
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start < 0 or end <= start:
            raise DocumentError("모델 응답에서 JSON을 찾지 못했습니다.")
        try:
            parsed = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError as error:
            raise DocumentError(f"모델 응답을 JSON으로 해석하지 못했습니다: {error}")

    if not isinstance(parsed, dict):
        raise DocumentError("모델 응답이 JSON 객체가 아닙니다.")
    return parsed


# 출력이 잘렸을 때 두 번째 시도에 붙이는 지시. 같은 구조를 유지하면서 분량만 줄이게 한다.
_CONCISE_HINT = (
    "이전 시도에서 출력이 길어 JSON이 잘렸습니다. 구조는 그대로 유지하되 "
    "각 배열은 최소 개수만 쓰고 문장을 짧게 줄여, 반드시 JSON을 끝까지 완성하세요."
)


def _request_text(caller, system_text: str, user_text: str, max_tokens: int, log_label: str) -> str:
    """평문 응답을 받아낸다. 잘리면 분량을 줄여 한 번 더 시도한다.

    **긴 산문에는 JSON을 쓰지 않는다.** 트리트먼트 본문처럼 여러 문단짜리 한국어를
    JSON 문자열 안에 담게 하면 파싱이 자주 깨진다. 실제로 아래로 실패했다.

        JSON parse failed treatment/narrative[intro,characterIntro]:
        Expecting ',' delimiter: line 14 column 815

    원인은 두 가지다. 문단을 나누려고 문자열 안에 실제 줄바꿈을 넣는 것, 그리고 대사를
    ASCII 큰따옴표로 감싸 문자열을 조기에 닫는 것이다. 상세도 '상세'는 대사 인용을
    요구하므로 이 실패가 사실상 보장된다. 지시로 막으려 해도 모델은 종종 어긴다.

    그래서 본문은 구분자를 쓰는 평문으로 받는다. 인용부호와 줄바꿈으로 깨질 수 없다.
    프로그램이 다시 읽어야 하는 구조적 산출물은 계속 JSON으로 받는다(_request_json).
    """
    attempts = [(0.6, ""), (0.4, _CONCISE_HINT)]
    last_error: DocumentError | None = None
    for index, (temperature, hint) in enumerate(attempts, start=1):
        system_with_hint = system_text + (f"\n\n[추가 지시] {hint}" if hint else "")
        text, truncated = caller(system_with_hint, user_text, max_tokens, temperature)
        if truncated:
            last_error = DocumentError("모델 출력이 길어 잘렸습니다.")
            logger.warning("Output truncated %s attempt=%s maxTokens=%s", log_label, index, max_tokens)
            continue
        if text and text.strip():
            return text
        last_error = DocumentError("모델이 빈 응답을 반환했습니다.")
        logger.warning("Empty response %s attempt=%s", log_label, index)
    raise last_error or DocumentError("본문을 생성하지 못했습니다.")


def _request_json(caller, system_text: str, user_text: str, max_tokens: int, log_label: str) -> dict:
    """JSON 하나를 받아낸다. 잘리거나 파싱이 실패하면 분량을 줄여 한 번 더 시도한다.

    caller는 gateway.text_caller가 만들어 준 함수다. 이 모듈은 Bedrock을 직접 부르지 않는다.
    """
    attempts = [(0.6, ""), (0.3, _CONCISE_HINT)]
    last_error: DocumentError | None = None
    for index, (temperature, hint) in enumerate(attempts, start=1):
        system_with_hint = system_text + (f"\n\n[추가 지시] {hint}" if hint else "")
        text, truncated = caller(system_with_hint, user_text, max_tokens, temperature)
        if truncated:
            # 잘린 JSON은 파싱해도 의미가 없다. 파싱을 시도하지 않고 바로 다시 만든다.
            last_error = DocumentError("모델 출력이 길어 잘렸습니다.")
            logger.warning("Output truncated %s attempt=%s maxTokens=%s", log_label, index, max_tokens)
            continue
        try:
            return extract_json(text)
        except DocumentError as error:
            last_error = error
            logger.warning("JSON parse failed %s attempt=%s: %s", log_label, index, error)
            continue
    raise last_error or DocumentError("산출물을 생성하지 못했습니다.")


def generate(
    caller,
    kind: str,
    project: dict,
    source_text: str,
    context: dict | None = None,
    options: dict | None = None,
    on_progress=None,
) -> dict:
    """산출물 본문(JSON)을 생성한다. 실패하면 DocumentError를 던진다.

    여러 번 호출해야 하는 산출물은 spec의 generator가 처리한다(트리트먼트).

    options는 생성 시점에 기획자가 고른 값이다. 종류마다 형태가 달라 스펙의 `options`
    선언과 normalize 함수가 짝을 이룬다. 지금은 트리트먼트만 옵션을 받는다.

    on_progress는 단계가 끝날 때마다 부르는 콜백이다. 트리트먼트는 모델 호출을 여러 번
    하므로 이것이 없으면 아직 진행 중인데도 stale 정리에 걸려 실패로 바뀐다.
    """
    spec = spec_for(kind)
    generator = spec.get("generator")
    if generator:
        payload = generator(caller, project, source_text, context or {}, options or {}, on_progress)
    else:
        # 옵션을 받지 않는 종류에 옵션이 오면 무시한다. 조용히 버리지 않고 로그로 남긴다.
        if options:
            logger.info("Options ignored kind=%s keys=%s", kind, sorted(options))
        payload = _request_json(
            caller,
            _system_text(kind),
            _project_block(project, source_text, context, MAX_SOURCE_CHARS),
            spec["maxTokens"],
            f"kind={kind}",
        )
    payload["_meta"] = {"kind": kind, "model": TEXT_MODEL, "label": spec["label"]}
    return payload


# --------------------------------------------------------------------------- #
# 트리트먼트: 설계 -> 화별 시놉시스 (2단계)
# --------------------------------------------------------------------------- #
# 한 번에 다 쓰게 하면 화수가 늘수록 출력이 잘린다. 먼저 시즌 구조와 화별 한 줄 개요를 잡고,
# 그 설계를 근거로 몇 화씩 나눠 상세 시놉시스를 쓴다. 계획을 먼저 세우므로 회차 간 흐름도 좋아진다.
_OUTLINE_SCHEMA = {
    "title": "작품 제목 (가제여도 무방)",
    "logline": "한 문장 로그라인",
    "overview": "기획 의도와 전체 개요 4~5문장",
    "toneAndManner": "연출 톤앤매너 2~3문장",
    "format": {"episodeCount": "총 화수 정수", "runtime": "회당 분량", "platform": "타겟 플랫폼"},
    "themes": ["작품이 다루는 주제"],
    "mainCharacters": [
        {"name": "이름", "role": "주인공 / 대립자 / 조력자 / 주변인", "summary": "한 줄 소개", "arc": "시즌 전체에서의 변화"}
    ],
    "structure": {
        "act1": "1막(도입)에서 벌어지는 일과 끝나는 지점",
        "act2": "2막(전개와 위기)의 흐름",
        "act3": "3막(절정과 해소)의 흐름",
    },
    "episodeOutlines": [
        {
            "number": "화 번호 정수",
            "title": "부제",
            "oneLine": "이 화에서 벌어지는 일 한 문장",
            "turningPoint": "이 화의 전환점",
        }
    ],
    "seasonEnding": "시즌 마지막 상태와 다음 시즌 가능성 2~3문장",
    "productionNotes": ["제작 시 고려할 점"],
}

_EPISODE_SCHEMA = {
    "episodes": [
        {
            "number": "화 번호 정수",
            "title": "부제",
            "synopsis": "5~7문장 시놉시스. 무슨 일이 일어나고 어떻게 끝나는지 구체적으로",
            "keyScenes": ["이 화의 핵심 장면"],
            "hook": "다음 화를 보게 만드는 마지막 지점",
            "characterFocus": ["이 화에서 중심이 되는 인물 이름"],
        }
    ]
}


def _episode_count(project: dict) -> int:
    try:
        count = int((project.get("direction") or {}).get("episodeCount") or 0)
    except (TypeError, ValueError):
        count = 0
    return count if 1 <= count <= 24 else int(DIRECTION_DEFAULTS["episodeCount"])


def _section_targets(pages: int) -> dict[str, int]:
    """페이지 예산을 섹션별 목표 글자 수로 나눈다.

    반올림 때문에 합이 예산과 어긋나므로 가장 두꺼운 섹션에 잔차를 넘긴다.
    """
    budget = max(1, pages) * CHARS_PER_A4_PAGE
    targets = {
        section["id"]: max(200, int(budget * section["weight"])) for section in _TREATMENT_SECTIONS
    }
    heaviest = max(_TREATMENT_SECTIONS, key=lambda section: section["weight"])["id"]
    targets[heaviest] += budget - sum(targets.values())
    return targets


# 본문 구분자. 줄 맨 앞에서만 인정해서 본문 안에 같은 글자가 나와도 오인하지 않는다.
_SECTION_MARK = "@@SECTION"
_BEATS_MARK = "@@BEATS"
_SECTION_LINE = re.compile(rf"^{_SECTION_MARK}\s*:\s*(\S+)\s*$", re.MULTILINE)


def _parse_narrative_text(text: str, wanted_ids: list[str]) -> tuple[dict, dict]:
    """구분자로 나뉜 평문에서 섹션별 본문과 핵심 장면을 뽑는다."""
    bodies: dict[str, str] = {}
    beats: dict[str, list] = {}

    marks = list(_SECTION_LINE.finditer(text))
    for index, match in enumerate(marks):
        section_id = match.group(1)
        if section_id not in wanted_ids:
            continue
        end = marks[index + 1].start() if index + 1 < len(marks) else len(text)
        chunk = text[match.end() : end]

        body_part, _, beats_part = chunk.partition(_BEATS_MARK)
        body = body_part.strip()
        if body:
            bodies[section_id] = body
        if beats_part.strip():
            lines = []
            for line in beats_part.splitlines():
                line = line.strip().lstrip("-*\u2022").strip()
                # 구분자 뒤의 콜론 한 줄(':' 만 남은 줄)은 버린다.
                if len(line) >= 2:
                    lines.append(line)
            if lines:
                beats[section_id] = lines
    return bodies, beats


def _generate_narrative(
    caller,
    project: dict,
    source_text: str,
    outline_for_prompt: str,
    options: dict,
    on_progress=None,
) -> tuple[dict, dict]:
    """트리트먼트 본문 5개 섹션을 만든다. (본문, 섹션별 핵심 장면)을 돌려준다.

    한 번에 다 쓰게 하면 10장 분량에서 출력이 잘린다. 무게가 비슷하게 묶은 배치로 나눈다.
    응답은 JSON이 아니라 구분자 평문으로 받는다(이유는 _request_text 참고).
    """
    detail_rule = _TREATMENT_DETAIL_RULES[options["detail"]]
    targets = _section_targets(options["pages"])
    by_id = {section["id"]: section for section in _TREATMENT_SECTIONS}

    narrative: dict[str, str] = {}
    beats: dict[str, list] = {}

    for batch in _TREATMENT_SECTION_BATCHES:
        sections = [by_id[section_id] for section_id in batch]
        batch_chars = sum(targets[section["id"]] for section in sections)

        wanted = "\n\n".join(
            f"{_SECTION_MARK}: {section['id']}   ({section['label']}, 약 {targets[section['id']]:,}자)\n"
            f"  {section['guide']}"
            for section in sections
        )
        example = "\n".join(
            [f"{_SECTION_MARK}: {sections[0]['id']}", "(본문 문단들. 문단 사이는 빈 줄로 나눕니다.)"]
            + ([f"{_BEATS_MARK}", "- 핵심 장면 한 줄", "- 핵심 장면 한 줄"]
               if options["detail"] != DOC_DETAIL_BRIEF else [])
        )
        system_text = (
            f"{BASE_RULES}\n\n"
            f"[이번 작업] 트리트먼트 본문 - {' / '.join(s['label'] for s in sections)}\n"
            "목표: 기획 회의에서 인쇄해 읽는 트리트먼트 본문을 쓴다. "
            "항목을 나열한 개요가 아니라 이야기가 이어지는 산문으로 쓴다.\n\n"
            f"[써야 하는 섹션 - 이 순서대로]\n{wanted}\n\n"
            "[출력 형식] JSON이 아니라 아래 형식의 평문으로 씁니다.\n"
            f"섹션은 반드시 '{_SECTION_MARK}: <섹션id>' 한 줄로 시작합니다. "
            f"섹션 id는 위에 적힌 것을 그대로 씁니다.\n"
            f"{example}\n\n"
            f"[상세도] {detail_rule['rule']}\n"
            f"[분량] 이 요청의 본문 합계는 약 {batch_chars:,}자입니다. "
            f"각 섹션의 목표 글자 수를 지키고, 합계 {int(batch_chars * 1.25):,}자를 넘기지 마세요. "
            "지정된 분량보다 크게 짧으면 문서를 쓸 수 없습니다.\n"
            "[주의] 대사는 한국어 인용부호 \u201c \u201d 를 씁니다. "
            "마크다운 제목(#)이나 굵게(**)를 쓰지 않습니다. 평문으로만 씁니다."
        )
        # BASE_RULES 6번이 "JSON 하나만 출력"이라고 지시한다. 여기서는 평문을 받으므로
        # 그 규칙을 명시적으로 덮어써야 한다. 덮어쓰지 않으면 모델이 JSON으로 감싼다.
        system_text += (
            f"\n\n[형식 재확인] 위 규칙 6번의 JSON 지시는 이 작업에 적용하지 않습니다. "
            f"'{_SECTION_MARK}:' 로 시작하는 평문만 출력하고, 코드펜스로 감싸지 않습니다."
        )
        if options["details"]:
            # 기획자가 적은 세부사항. 원본 자료보다 우선한다.
            system_text += f"\n\n[기획자가 요청한 세부사항 - 반드시 반영]\n{options['details']}"

        user_text = (
            f"[확정된 시즌 설계]\n{outline_for_prompt}\n\n"
            f"{_project_block(project, source_text, None, 10000)}"
        )
        # 토큰 예산. 한국어는 한 글자가 1토큰보다 많이 쪼개지고(음절당 1~2), 모델이
        # 목표 분량을 웃돌아 쓴다. 2배로 잡았더니 실제로 잘렸다.
        #   Output truncated treatment/narrative[mainEvents] maxTokens=4068  (목표 1,584자)
        # 넉넉하게 4배 + 여유를 준다. 잘리면 재시도로 한 번 더 부르므로 시간과 비용이 배가 된다.
        text = _request_text(
            caller,
            system_text,
            user_text,
            min(32000, batch_chars * 4 + 1200),
            f"treatment/narrative[{','.join(batch)}]",
        )
        bodies, batch_beats = _parse_narrative_text(text, list(batch))
        if not bodies and len(batch) == 1:
            # 섹션이 하나뿐인 배치에서는 모델이 구분자를 생략하는 경우가 있다.
            # 요청한 섹션이 하나이므로 본문 전체를 그 섹션으로 본다.
            body_part, _, beats_part = text.partition(_BEATS_MARK)
            fallback = body_part.strip()
            if fallback:
                bodies = {batch[0]: fallback}
                if beats_part.strip():
                    lines = [
                        line.strip().lstrip("-*\u2022").strip()
                        for line in beats_part.splitlines()
                    ]
                    lines = [line for line in lines if len(line) >= 2]
                    if lines:
                        batch_beats = {batch[0]: lines}
                logger.info("Narrative marker missing, used whole body batch=%s", batch)
        narrative.update(bodies)
        beats.update(batch_beats)

        missing_here = [section_id for section_id in batch if not narrative.get(section_id)]
        if missing_here:
            logger.warning(
                "Narrative section missing batch=%s missing=%s chars=%s",
                batch,
                missing_here,
                len(text),
            )
        if on_progress:
            on_progress()

    if not narrative:
        raise DocumentError("트리트먼트 본문을 만들지 못했습니다. 다시 시도해 주세요.")
    return narrative, beats


def _generate_treatment(
    caller,
    project: dict,
    source_text: str,
    context: dict,
    options: dict | None = None,
    on_progress=None,
) -> dict:
    episode_count = _episode_count(project)
    spec = spec_for("treatment")
    options = normalize_treatment_options(options)

    # 1단계: 시즌 설계
    outline_system = (
        f"{BASE_RULES}\n\n"
        f"[이번 작업] 트리트먼트 설계\n"
        f"목표: 시즌 전체 구조를 잡고 {episode_count}화 각각의 한 줄 개요까지 정한다.\n\n"
        f"[출력 형식] 아래 JSON 구조를 그대로 따릅니다.\n"
        f"{json.dumps(_OUTLINE_SCHEMA, ensure_ascii=False, indent=2)}\n\n"
        f"[분량] episodeOutlines는 정확히 {episode_count}개, number는 1부터 {episode_count}까지 빠짐없이 씁니다. "
        f"{spec['limits']}\n"
        f"format.episodeCount는 {episode_count}로 씁니다.\n"
        "반드시 JSON을 끝까지 닫아 주세요."
    )
    if options["details"]:
        # 세부사항은 설계 단계부터 반영해야 한다. 본문 단계에서만 넣으면
        # 이미 정해진 구조와 어긋나 본문이 설계를 배반한다.
        outline_system += f"\n\n[기획자가 요청한 세부사항 - 반드시 반영]\n{options['details']}"
    outline = _request_json(
        caller,
        outline_system,
        _project_block(project, source_text, context, MAX_SOURCE_CHARS),
        spec["maxTokens"] + 220 * episode_count,
        "treatment/outline",
    )

    outlines = outline.get("episodeOutlines")
    if not isinstance(outlines, list) or not outlines:
        raise DocumentError("화별 구성을 만들지 못했습니다. 다시 시도해 주세요.")
    if on_progress:
        on_progress()

    outline_for_prompt = json.dumps(outline, ensure_ascii=False, indent=2)[:12000]

    # 2단계: 본문 5개 섹션. 기획자가 인쇄해서 읽는 부분이라 먼저 만든다.
    narrative, narrative_beats = _generate_narrative(
        caller, project, source_text, outline_for_prompt, options, on_progress
    )

    # 3단계: 화별 상세 시놉시스. 설계를 근거로 몇 화씩 나눠 쓴다.
    episodes: list[dict] = []
    for start in range(1, episode_count + 1, EPISODE_BATCH_SIZE):
        end = min(start + EPISODE_BATCH_SIZE - 1, episode_count)
        batch_system = (
            f"{BASE_RULES}\n\n"
            f"[이번 작업] 트리트먼트의 화별 시놉시스\n"
            f"목표: 확정된 시즌 설계를 근거로 {start}화부터 {end}화까지의 시놉시스를 쓴다.\n\n"
            f"[출력 형식] 아래 JSON 구조를 그대로 따릅니다.\n"
            f"{json.dumps(_EPISODE_SCHEMA, ensure_ascii=False, indent=2)}\n\n"
            f"[분량] episodes는 {start}화부터 {end}화까지 정확히 {end - start + 1}개만 씁니다. "
            "다른 화는 쓰지 않습니다. keyScenes는 3~4개, characterFocus는 1~3명.\n"
            "설계의 부제와 전환점을 지키고, 앞 화의 결말에서 자연스럽게 이어지게 씁니다.\n"
            "반드시 JSON을 끝까지 닫아 주세요."
        )
        batch_user = (
            f"[확정된 시즌 설계]\n{outline_for_prompt}\n\n"
            f"{_project_block(project, source_text, None, 8000)}"
        )
        batch = _request_json(
            caller, batch_system, batch_user, 1400 * (end - start + 1) + 800, f"treatment/ep{start}-{end}"
        )
        items = batch.get("episodes")
        if not isinstance(items, list):
            raise DocumentError(f"{start}~{end}화 시놉시스를 만들지 못했습니다.")
        episodes.extend(item for item in items if isinstance(item, dict))
        if on_progress:
            on_progress()

    # 번호를 정리하고 설계에 없는 화가 섞여 들어오면 잘라낸다.
    episodes = _normalize_episodes(episodes, episode_count)
    if not episodes:
        raise DocumentError("화별 시놉시스를 만들지 못했습니다. 다시 시도해 주세요.")

    treatment = {key: value for key, value in outline.items() if key != "episodeOutlines"}
    treatment["narrative"] = narrative
    treatment["narrativeBeats"] = narrative_beats
    # 화면과 검증이 섹션 순서·라벨을 서버와 맞추도록 함께 내려준다.
    # 프론트에 순서를 하드코딩하면 섹션을 늘릴 때 조용히 빠진다.
    treatment["narrativeSections"] = [
        {"id": section["id"], "label": section["label"]}
        for section in _TREATMENT_SECTIONS
        if narrative.get(section["id"])
    ]
    treatment["episodes"] = episodes

    body_chars = sum(len(text) for text in narrative.values())
    treatment["pageEstimate"] = {
        "requestedPages": options["pages"],
        "detail": options["detail"],
        "bodyChars": body_chars,
        "charsPerPage": CHARS_PER_A4_PAGE,
        # 실측 페이지당 글자 수로 되짚은 값. 요청 장수와 크게 다르면 화면에서 알려 준다.
        "estimatedPages": round(body_chars / CHARS_PER_A4_PAGE, 1),
    }

    notes = []
    if len(episodes) < episode_count:
        # 일부만 만들어진 경우에도 결과는 쓸 수 있게 두고, 부족하다는 사실을 남긴다.
        notes.append(
            f"{episode_count}화 중 {len(episodes)}화까지만 생성되었습니다. 다시 생성하면 전체를 만듭니다."
        )
    empty = [
        section["label"]
        for section in _TREATMENT_SECTIONS
        if not narrative.get(section["id"])
    ]
    if empty:
        notes.append(f"본문에서 {', '.join(empty)} 섹션이 비었습니다. 다시 생성해 주세요.")
    if notes:
        treatment["incompleteNote"] = " ".join(notes)
    return treatment


def _normalize_episodes(episodes: list[dict], episode_count: int) -> list[dict]:
    """번호를 정수로 맞추고 중복을 제거한 뒤 순서대로 돌려준다."""
    by_number: dict[int, dict] = {}
    for index, episode in enumerate(episodes, start=1):
        try:
            number = int(episode.get("number") or index)
        except (TypeError, ValueError):
            number = index
        if not 1 <= number <= episode_count or number in by_number:
            continue
        episode["number"] = number
        by_number[number] = episode
    return [by_number[number] for number in sorted(by_number)]


KIND_SPECS["treatment"]["generator"] = _generate_treatment


# --------------------------------------------------------------------------- #
# 롱테이크: 컷 나누기
# --------------------------------------------------------------------------- #
_SHOT_PLAN_SCHEMA = {
    "title": "이 시퀀스를 한 줄로 요약한 제목",
    "subject": (
        "모든 컷에 공통으로 들어갈 인물 묘사. 영어 8~25 단어. 옷차림과 눈에 보이는 특징을 적는다. "
        "나이는 숫자 대신 'young woman', 'man in his forties'처럼 쓴다."
    ),
    "setting": "모든 컷에 공통으로 들어갈 장소·시간대·날씨. 영어 6~20 단어.",
    "lighting": "모든 컷에 공통으로 들어갈 조명. 광원의 위치와 성질. 영어 5~18 단어.",
    "styleClause": "모든 컷에 공통으로 들어갈 화면 look. 영어 4~12 단어.",
    "shots": [
        {
            "number": "컷 번호 정수",
            "summary": "이 컷에서 무엇이 보이는지 한국어 한 문장",
            "shotType": "숏 크기 하나. 아래 목록에서 고른다. 한 컷 안에서 구도가 바뀌면 화면이 뭉개진다.",
            "action": "이 컷에서 인물이 하는 하나의 연속된 동작. 영어 6~20 단어.",
            "cameraMove": "카메라 움직임 하나. 아래 목록에서 고른다.",
        }
    ],
}

# 공통 블록을 한 번만 받는 이유는 프롬프트 규칙으로 부탁하는 것보다 확실하기 때문이다.
# "매 컷 똑같이 반복하세요"라고 적어도 모델은 조금씩 바꿔 쓰고, 그러면 컷마다 인물이 달라진다.
# 인물·장소·조명·look을 한 번만 받아 서버가 모든 컷에 같은 문장으로 끼워 넣는다.
_SHOT_PLAN_RULES = """당신은 드라마 촬영 콘티를 컷으로 나누는 촬영감독입니다.
하나의 장면을 연속된 여러 컷으로 나눕니다. 각 컷은 {shot_seconds}초짜리 영상 클립으로 따로 생성되고,
나중에 순서대로 이어 붙여 하나의 영상이 됩니다.

규칙:
1. 정확히 {shot_count}개의 컷을 만듭니다. number는 1부터 {shot_count}까지 빠짐없이 씁니다.
2. subject, setting, lighting, styleClause는 **딱 한 번만** 씁니다. 서버가 이 문장을 모든 컷에
   그대로 복사해 넣습니다. 컷마다 다시 쓰지 않습니다.
3. 컷마다 다른 것은 shotType, action, cameraMove 셋뿐입니다. 앞 컷의 끝과 다음 컷의 시작이
   자연스럽게 이어지도록 동작을 배치합니다.
4. 한 컷에는 하나의 동작만 넣습니다. {shot_seconds}초 안에 사건을 두 개 넣으면 화면이 갑자기
   전환되며 뭉개집니다.
5. 동작은 작고 물리적으로 자연스럽게 유지합니다. 동작이 크면 모델이 보이지 않는 신체 부위를
   상상해 그리면서 팔다리가 늘어납니다.
6. cameraMove는 다음 중 하나를 그대로 씁니다: {camera_moves}
   shotType은 다음 중 하나를 그대로 씁니다: {shot_types}
7. 영어 항목에는 부정문을 쓰지 않습니다. 이 모델에는 부정 프롬프트가 없어서 원하지 않는 것을
   적으면 오히려 나타납니다. 일어나는 일만 적습니다.
8. 'beautiful', 'amazing' 같은 취향 표현 대신 눈에 보이는 것을 적습니다.
9. JSON 하나만 출력합니다."""


def plan_shots(
    caller,
    scene_prompt: str,
    shot_count: int,
    shot_seconds: int,
    style_prompt: str = "",
) -> dict:
    """장면을 여러 컷으로 나눈다.

    컷마다 따로 생성되기 때문에 묘사를 반복하지 않으면 인물이 컷마다 달라진다.
    그래서 프롬프트 규칙에서 반복 서술을 가장 강하게 요구한다.
    """
    if shot_count < 1:
        raise DocumentError("컷 수가 올바르지 않습니다.")

    system = (
        _SHOT_PLAN_RULES.format(
            shot_count=shot_count,
            shot_seconds=shot_seconds,
            camera_moves=", ".join(video_prompt.CAMERA_MOVES),
            shot_types=", ".join(video_prompt.SHOT_TYPES),
        )
        + "\n\n[출력 형식]\n"
        + json.dumps(_SHOT_PLAN_SCHEMA, ensure_ascii=False, indent=2)
        + "\n\n반드시 JSON을 끝까지 닫아 주세요."
    )
    parts = [f"[장면]\n{scene_prompt.strip()[:3000]}"]
    if style_prompt.strip():
        parts.append(f"[유지할 그림체 · 톤]\n{style_prompt.strip()[:600]}")
    parts.append(f"[컷 수] {shot_count}개, 컷당 {shot_seconds}초")

    payload = _request_json(
        caller,
        system,
        "\n\n".join(parts),
        # 공통 블록을 한 번만 받으므로 컷당 출력이 짧다. 컷당 130 토큰 + 공통 블록 여유.
        min(20000, 900 + 140 * shot_count),
        f"shots/{shot_count}",
    )
    shots = payload.get("shots")
    if not isinstance(shots, list) or not shots:
        raise DocumentError("컷을 나누지 못했습니다. 다시 시도해 주세요.")

    # 컷 전체가 공유하는 블록. 여기서 한 번 정리해 모든 컷에 같은 문장으로 넣는다.
    shared = {
        "subject": str(payload.get("subject") or "").strip(),
        "setting": str(payload.get("setting") or "").strip(),
        "lighting": str(payload.get("lighting") or "").strip(),
    }
    style_clause = str(payload.get("styleClause") or "").strip()

    cleaned = []
    for shot in shots:
        if not isinstance(shot, dict):
            continue
        action = str(shot.get("action") or "").strip()
        if not action:
            continue
        prompt, move = video_prompt.assemble_shot(
            {
                "shotType": str(shot.get("shotType") or "Medium shot").strip(),
                "action": action,
                "cameraMove": str(shot.get("cameraMove") or "").strip(),
                **shared,
            },
            style_clause,
        )
        cleaned.append(
            {
                "number": len(cleaned) + 1,
                "summary": str(shot.get("summary") or "").strip()[:300],
                "prompt": prompt,
                "cameraMove": move,
            }
        )
        if len(cleaned) >= shot_count:
            break
    if not cleaned:
        raise DocumentError("사용할 수 있는 컷 프롬프트가 없습니다.")
    return {"title": str(payload.get("title") or "").strip()[:200], "shots": cleaned}


# --------------------------------------------------------------------------- #
# 콘티 기획표: 컷마다 그림 한 장 + 설명
# --------------------------------------------------------------------------- #
# 상세도별로 요구하는 분량. 이것이 표에 들어가는 정보량을 결정한다.
_SB_DETAIL_RULES = {
    "brief": {
        "label": "간단",
        "context": "한국어 한 문장. 무엇이 보이는지만 적는다.",
        "extras": "dialogue와 audio는 빈 문자열로 둔다.",
        "tokens": 150,
    },
    "normal": {
        "label": "보통",
        "context": "한국어 두 문장. 무엇이 보이는지와 인물이 무엇을 하는지 적는다.",
        "extras": "dialogue에는 이 컷의 대사를 한 줄 적는다(없으면 빈 문자열). audio에는 주요 소리를 짧게 적는다.",
        "tokens": 220,
    },
    "full": {
        "label": "상세",
        "context": (
            "한국어 세 문장. 무엇이 보이는지, 인물이 무엇을 하는지, 이 컷이 이야기에서 하는 역할을 적는다."
        ),
        "extras": (
            "dialogue에는 인물명과 대사를 적는다(없으면 빈 문자열). audio에는 효과음과 음악 방향을 적는다. "
            "note에는 연출 의도나 주의점을 한국어 한 문장으로 적는다."
        ),
        "tokens": 320,
    },
}


def storyboard_details() -> list[dict]:
    """프론트가 상세도 선택 UI를 그릴 때 쓴다."""
    return [
        {"id": key, "label": value["label"], "hint": value["context"]}
        for key, value in _SB_DETAIL_RULES.items()
    ]


_SB_SCHEMA = {
    "title": "이 콘티를 한 줄로 요약한 제목(한국어)",
    "characters": (
        "모든 컷에 공통으로 들어갈 인물 묘사. 영어 10~30 단어. 옷차림과 눈에 보이는 특징. "
        "나이는 숫자 대신 'young woman' 처럼 쓴다. 인물이 여러 명이면 모두 적는다."
    ),
    "setting": "모든 컷에 공통으로 들어갈 장소와 시간대. 영어 6~20 단어.",
    "cuts": [
        {
            "number": "컷 번호 정수",
            "context": "이 컷의 장면 설명(한국어). 분량은 아래 지시를 따른다.",
            "scene": (
                "이 컷을 그릴 영어 묘사. 12~35 단어. 이 컷에서만 다른 것(인물의 동작, 표정, 화면에 보이는 대상)을 "
                "적는다. 인물 외형과 장소는 서버가 공통 블록으로 붙이므로 반복하지 않는다."
            ),
            "shot": "숏 크기 하나. wide / medium / close-up / over-the-shoulder 중에서 고른다.",
            "dialogue": "이 컷의 대사(한국어). 없으면 빈 문자열.",
            "audio": "이 컷의 소리(한국어). 효과음이나 음악. 없으면 빈 문자열.",
            "note": "연출 메모(한국어). 상세도가 '상세'가 아니면 빈 문자열.",
            "seconds": "이 컷의 예상 길이(초, 정수). 2~10 사이.",
        }
    ],
}

_SB_RULES = """당신은 웹툰 원작을 드라마로 옮기는 제작사의 촬영감독입니다.
주어진 이야기를 **연속된 {cut_count}개의 컷**으로 나눠 콘티 기획표를 만듭니다.

콘티는 그림 한 장이 아닙니다. 컷마다 그림과 설명이 짝을 이루고, 앞 컷에서 다음 컷으로
이야기가 이어지는 표입니다. 회의에서 이 표를 보고 촬영 순서를 논의합니다.

규칙:
1. 정확히 {cut_count}개의 컷을 만듭니다. number는 1부터 {cut_count}까지 빠짐없이 씁니다.
2. **컷들이 하나의 이야기로 이어져야 합니다.** 같은 장면을 각도만 바꿔 {cut_count}번 반복하지 않습니다.
   시작 - 전개 - 전환 - 마무리가 보이도록 시간 순서대로 배치합니다.
3. characters와 setting은 **딱 한 번만** 씁니다. 서버가 이 문장을 모든 컷 그림에 그대로 복사합니다.
   컷마다 인물 외형을 다시 쓰지 않습니다. 다시 쓰면 컷마다 다른 사람이 나옵니다.
4. scene에는 그 컷에서만 다른 것을 적습니다. 동작, 표정, 화면에 들어오는 대상, 카메라 위치.
5. context(장면 설명)의 분량: {context_rule}
6. {extras_rule}
7. shot은 컷마다 다양하게 섞습니다. 전부 medium으로 채우면 표가 단조로워 회의에 쓸모가 없습니다.
8. scene은 영어로, context/dialogue/audio/note는 한국어로 씁니다.
9. scene에 부정문을 쓰지 않습니다. 이미지 모델은 원하지 않는 것을 적으면 오히려 그립니다.
10. JSON 하나만 출력합니다."""


def plan_storyboard(
    caller,
    story: str,
    cut_count: int,
    detail: str = "normal",
    style_prompt: str = "",
    context: dict | None = None,
) -> dict:
    """이야기를 콘티 컷으로 나눈다.

    컷마다 이미지를 따로 생성하기 때문에 인물 묘사가 컷마다 흔들리면 다른 사람이 나온다.
    그래서 인물과 장소는 한 번만 받아 서버가 모든 컷에 같은 문장으로 넣는다(롱테이크와 같은 방식).

    반환값의 `scene`은 영어(이미지 모델용), `context`는 한국어(사람이 읽는 설명)로 분리된다.
    한 문장으로 둘을 겸하게 하면 그림도 설명도 어중간해진다.
    """
    if cut_count < 1:
        raise DocumentError("컷 수가 올바르지 않습니다.")
    rule = _SB_DETAIL_RULES.get(detail) or _SB_DETAIL_RULES["normal"]

    system = (
        _SB_RULES.format(
            cut_count=cut_count,
            context_rule=rule["context"],
            extras_rule=rule["extras"],
        )
        + "\n\n[출력 형식]\n"
        + json.dumps(_SB_SCHEMA, ensure_ascii=False, indent=2)
        + "\n\n반드시 JSON을 끝까지 닫아 주세요."
    )

    parts = [f"[이야기]\n{story.strip()[:MAX_SOURCE_CHARS]}"]
    if style_prompt.strip():
        parts.append(f"[그림체 · 톤]\n{style_prompt.strip()[:600]}")
    # 앞 단계 산출물이 있으면 인물과 구조를 그대로 쓴다. 없으면 이야기만으로 진행한다.
    for kind, payload in (context or {}).items():
        label = CONTEXT_LABELS.get(kind, kind)
        body = json.dumps(payload, ensure_ascii=False)[:MAX_CONTEXT_CHARS_PER_DOC]
        parts.append(f"[{label}]\n{body}")
    parts.append(f"[컷 수] {cut_count}개, 설명 상세도: {rule['label']}")

    payload = _request_json(
        caller,
        system,
        "\n\n".join(parts),
        min(20000, 1200 + rule["tokens"] * cut_count),
        f"storyboard/{cut_count}",
    )

    cuts_raw = payload.get("cuts")
    if not isinstance(cuts_raw, list) or not cuts_raw:
        raise DocumentError("컷을 나누지 못했습니다. 다시 시도해 주세요.")

    shared_characters = str(payload.get("characters") or "").strip()
    shared_setting = str(payload.get("setting") or "").strip()

    cuts = []
    for item in cuts_raw:
        if not isinstance(item, dict):
            continue
        scene = str(item.get("scene") or "").strip()
        context_text = str(item.get("context") or "").strip()
        if not scene and not context_text:
            continue
        try:
            seconds = int(item.get("seconds") or 4)
        except (TypeError, ValueError):
            seconds = 4
        cuts.append(
            {
                "number": len(cuts) + 1,
                "context": context_text[:600],
                "scene": scene[:600],
                "shot": str(item.get("shot") or "medium").strip()[:40],
                "dialogue": str(item.get("dialogue") or "").strip()[:400],
                "audio": str(item.get("audio") or "").strip()[:300],
                "note": str(item.get("note") or "").strip()[:300],
                "seconds": max(1, min(30, seconds)),
                "status": "pending",
                "error": "",
                "imageKey": "",
            }
        )
        if len(cuts) >= cut_count:
            break
    if not cuts:
        raise DocumentError("사용할 수 있는 컷이 없습니다.")

    return {
        "title": str(payload.get("title") or "").strip()[:200],
        "characters": shared_characters[:600],
        "setting": shared_setting[:400],
        "cuts": cuts,
    }


_MANUAL_SCHEMA = {
    "title": "이 콘티를 한 줄로 요약한 제목(한국어)",
    "characters": (
        "모든 컷에 공통으로 들어갈 인물 묘사. 영어 10~30 단어. 옷차림과 눈에 보이는 특징. "
        "나이는 숫자 대신 'young woman' 처럼 쓴다."
    ),
    "setting": "모든 컷에 공통으로 들어갈 장소와 시간대. 영어 6~20 단어.",
    "cuts": [
        {
            "number": "컷 번호 정수. 입력받은 번호를 그대로 쓴다.",
            "scene": (
                "이 컷을 그릴 영어 묘사. 12~35 단어. 기획자가 적은 내용만 옮긴다. "
                "인물 외형과 장소는 서버가 공통 블록으로 붙이므로 반복하지 않는다."
            ),
            "shot": "숏 크기 하나. wide / medium / close-up / over-the-shoulder 중에서 고른다.",
        }
    ],
}

_MANUAL_RULES = """당신은 웹툰 원작을 드라마로 옮기는 제작사의 촬영감독입니다.
기획자가 컷을 직접 적어 왔습니다. 그 컷을 그림으로 그릴 수 있게 영어 묘사로 옮기는 일만 합니다.

규칙:
1. **컷을 새로 만들거나 합치거나 나누지 않습니다.** 입력받은 컷 {cut_count}개를 그대로 옮깁니다.
   number도 입력값을 그대로 씁니다.
2. 기획자가 적지 않은 사건을 추가하지 않습니다. 적힌 내용을 그림으로 옮기는 것이 전부입니다.
3. characters와 setting은 딱 한 번만 씁니다. 서버가 모든 컷에 그대로 복사합니다.
   전체 컷을 읽고 일관된 인물·장소를 정합니다.
4. scene에는 그 컷에서만 다른 것을 적습니다. 동작, 표정, 화면에 들어오는 대상.
5. shot은 기획자가 적었으면 그것을 따르고, 없으면 내용에 맞게 고릅니다.
6. scene은 영어로 씁니다. 부정문을 쓰지 않습니다. 이미지 모델은 원하지 않는 것을 적으면 그립니다.
7. JSON 하나만 출력합니다."""


def prepare_manual_cuts(
    caller,
    cuts: list[dict],
    style_prompt: str = "",
    story: str = "",
) -> dict:
    """기획자가 직접 적은 컷을 그릴 수 있는 형태로 바꾼다.

    AI가 컷을 나누지 않는다. 적힌 컷을 그대로 두고 영어 묘사와 공통 블록만 만든다.
    컷을 마음대로 합치거나 늘리면 기획자가 의도한 구성이 무너지므로 규칙에서 가장 강하게 막는다.
    """
    if not cuts:
        raise DocumentError("컷을 하나 이상 적어 주세요.")

    system = (
        _MANUAL_RULES.format(cut_count=len(cuts))
        + "\n\n[출력 형식]\n"
        + json.dumps(_MANUAL_SCHEMA, ensure_ascii=False, indent=2)
        + "\n\n반드시 JSON을 끝까지 닫아 주세요."
    )

    listed = "\n".join(
        f"{cut['number']}. {cut.get('context') or ''}"
        + (f" [숏: {cut['shot']}]" if cut.get("shot") else "")
        for cut in cuts
    )
    parts = [f"[기획자가 적은 컷 {len(cuts)}개]\n{listed}"]
    if story.strip():
        parts.append(f"[작품 맥락]\n{story.strip()[:4000]}")
    if style_prompt.strip():
        parts.append(f"[그림체 · 톤]\n{style_prompt.strip()[:600]}")

    payload = _request_json(
        caller,
        system,
        "\n\n".join(parts),
        min(20000, 900 + 150 * len(cuts)),
        f"storyboard-manual/{len(cuts)}",
    )

    # 모델 응답을 번호로 색인해 입력 컷에 붙인다. 응답이 컷을 빠뜨려도 입력 구성을 유지한다.
    by_number: dict[int, dict] = {}
    for item in payload.get("cuts") or []:
        if not isinstance(item, dict):
            continue
        try:
            by_number[int(item.get("number"))] = item
        except (TypeError, ValueError):
            continue

    prepared = []
    for cut in cuts:
        drawn = by_number.get(cut["number"], {})
        scene = str(drawn.get("scene") or "").strip()
        prepared.append(
            {
                **cut,
                # 영어 묘사를 못 받았으면 기획자가 적은 문장을 그대로 쓴다. 품질은 떨어지지만
                # 그 컷만 비는 것보다 낫다.
                "scene": (scene or cut.get("context") or "")[:600],
                "shot": (cut.get("shot") or str(drawn.get("shot") or "medium")).strip()[:40],
                "status": "pending",
                "error": "",
                "imageKey": "",
            }
        )

    return {
        "title": str(payload.get("title") or "").strip()[:200],
        "characters": str(payload.get("characters") or "").strip()[:600],
        "setting": str(payload.get("setting") or "").strip()[:400],
        "cuts": prepared,
    }


def storyboard_scene_prompt(cut: dict, characters: str, setting: str) -> str:
    """컷 하나를 그릴 영어 장면 묘사. 공통 블록을 앞에 붙인다.

    순서가 중요하다. 인물을 먼저 고정하고 그 뒤에 이 컷의 동작을 붙인다.
    반대로 두면 모델이 동작에 끌려 인물 외형을 새로 만들어낸다.
    """
    shot_words = {
        "wide": "wide shot",
        "medium": "medium shot",
        "close-up": "close-up shot",
        "close up": "close-up shot",
        "closeup": "close-up shot",
        "over-the-shoulder": "over-the-shoulder shot",
        "over the shoulder": "over-the-shoulder shot",
    }
    shot = shot_words.get((cut.get("shot") or "").strip().lower(), "medium shot")
    parts = [shot]
    if characters:
        parts.append(characters.rstrip("."))
    if setting:
        parts.append(setting.rstrip("."))
    parts.append((cut.get("scene") or "").rstrip("."))
    return ", ".join(part for part in parts if part)[:2000]


def document_title(kind: str, payload: dict) -> str:
    """목록에 보여줄 한 줄 요약."""
    spec = spec_for(kind)
    if kind == "analysis":
        verdict = str(payload.get("verdict") or "").strip()
        score = payload.get("score")
        if verdict:
            return f"{verdict}" + (f" · 적합도 {score}" if isinstance(score, int) else "")
    elif kind == "adaptation":
        recommended = str(payload.get("recommended") or "").strip()
        if recommended:
            return f"추천 방향: {recommended}"
    elif kind == "logline":
        logline = str(payload.get("logline") or "").strip()
        if logline:
            return logline[:120]
    elif kind == "characters":
        characters = payload.get("characters")
        if isinstance(characters, list) and characters:
            names = [str(item.get("name") or "").strip() for item in characters if isinstance(item, dict)]
            names = [name for name in names if name]
            if names:
                return f"인물 {len(names)}명: " + ", ".join(names[:5])
    elif kind == "treatment":
        episodes = payload.get("episodes")
        count = len(episodes) if isinstance(episodes, list) else 0
        title = str(payload.get("title") or "").strip()
        estimate = payload.get("pageEstimate") or {}
        pages = estimate.get("estimatedPages")
        parts = []
        if title:
            parts.append(title)
        if isinstance(pages, (int, float)) and pages:
            parts.append(f"본문 약 {pages}장")
        if count:
            parts.append(f"{count}부작 시놉시스")
        if parts:
            return " · ".join(parts)
    return spec["label"]
