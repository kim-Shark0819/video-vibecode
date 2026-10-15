"""그림체(아트 스타일) 프리셋과 모델 라우팅.

## 왜 이런 구조인가

Bedrock의 텍스트→이미지 모델(core / sd3_5 / ultra)은 `style_preset` 파라미터를 받지 않는다.
실제 호출로 확인했고, 오류 응답이 허용 필드를 그대로 알려준다.

    Invalid field 'style_preset' in request. Available fields for sd3_5 are:
    prompt, negative_prompt, mode, strength, seed, output_format, image, aspect_ratio

즉 "그림체를 고르는 파라미터가 있는 모델"은 없다. 그림체는 **프롬프트로** 만든다.
따라서 그림체 표현력은 곧 프롬프트 준수력이고, 그것이 좋은 모델이 그림체도 정확하다.

## 라우팅 규칙

1. 그림체 자체는 프리셋의 영어 프롬프트 조각과 부정 프롬프트로 만든다(모델 교체 없음).
2. 모델은 **용도**로 고른다. 성능이 1순위, 비용이 2순위라는 기준을 그대로 반영했다.
   - premium(ultra): 최종 키비주얼. 가장 비싸고 느리다(실측 약 11초).
   - standard(sd3.5 large): 기본값. 프롬프트 준수가 좋고 적당히 빠르다(약 7초).
   - draft(core): 러프 콘티를 여러 장 뽑을 때. 가장 저렴하고 빠르다(약 4~6초).
3. **원작 그림체를 그대로 살려야 하면** 프롬프트로는 불가능하다. 참조 이미지가 있으면
   Style Guide 모델로 보낸다. 참조 이미지의 화풍을 추출해 새 장면을 그린다.
"""

import json
import logging
import re

logger = logging.getLogger("toon-change-ai-pj-app.styles")

# 용도 -> 모델 티어. 카탈로그의 tier 값과 맞아야 한다.
TIER_DRAFT = "draft"
TIER_STANDARD = "standard"
TIER_PREMIUM = "premium"
TIERS = {
    TIER_DRAFT: {"label": "러프 (빠르고 저렴)", "note": "콘티를 여러 장 뽑을 때"},
    TIER_STANDARD: {"label": "표준 (기본)", "note": "프롬프트를 가장 정확히 따릅니다"},
    TIER_PREMIUM: {"label": "고품질 (느리고 비쌈)", "note": "최종 키비주얼, 포스터"},
}

PURPOSE_TIERS = {
    "storyboard": TIER_DRAFT,
    "scene": TIER_STANDARD,
    "keyart": TIER_PREMIUM,
    "videoFrame": TIER_STANDARD,
}

# 등급 비교용 순서. 프리셋이 최소 등급을 요구할 때 쓴다.
TIER_ORDER = [TIER_DRAFT, TIER_STANDARD, TIER_PREMIUM]

# 부정 프롬프트 공통 항목. 어느 그림체에서도 원하지 않는 것들이다.
BASE_NEGATIVE = "text, letters, korean text, watermark, signature, logo, extra limbs, deformed hands, extra fingers, low quality, jpeg artifacts"

STYLE_PRESETS: list[dict] = [
    {
        "id": "webtoon-color",
        "lead": "A Korean webtoon illustration",
        "label": "웹툰 채색",
        "hint": "한국 웹툰 특유의 깔끔한 선과 부드러운 채색",
        "keywords": ["웹툰", "만화", "채색", "망화", "manhwa", "webtoon", "카툰"],
        "prompt": (
            "Korean webtoon illustration style, clean crisp linework, soft cel shading with gentle "
            "gradients, bright saturated palette, expressive large eyes, digital comic art"
        ),
        "negative": "photorealistic, 3d render, oil painting texture",
        "tier": TIER_STANDARD,
    },
    {
        "id": "webtoon-line",
        "lead": "A black and white manhwa line art panel",
        "label": "웹툰 선화",
        "hint": "채색 없이 선만. 콘티 검토에 적합",
        "keywords": ["선화", "라인", "흑백 만화", "펜선", "lineart", "line art"],
        "prompt": (
            "black and white manhwa line art, clean confident ink linework, screentone hatching for "
            "shadows, no colour fills, high contrast, comic panel drawing"
        ),
        "negative": "colour, painted shading, photorealistic, 3d render",
        "tier": TIER_STANDARD,
    },
    {
        "id": "storyboard-rough",
        "lead": "A quick black marker storyboard sketch on plain white paper",
        "label": "러프 콘티 (마커)",
        # 예전 hint가 "연필 스케치 느낌"이었는데 프롬프트는 펠트펜(마커)이고 명암을 3단계 쓴다.
        # 실제 연필 프리셋(storyboard-pencil)이 생겼으므로 이름을 사실과 맞췄다.
        # keywords에서 "연필"을 뺀 것도 같은 이유다. 두 프리셋이 같은 키워드를 가지면
        # match_by_keyword가 목록에서 먼저 나오는 이쪽을 항상 고른다.
        "hint": "마커 선 + 명암 3단계. 정보량이 조금 있는 러프",
        "keywords": ["콘티", "러프", "storyboard", "썸네일", "간단하게", "마커"],
        # 러프는 "덜 그리기"를 강하게 지시해야 한다. 완성된 연필 일러스트가 계속 나와
        # 매체(마커), 종이(흰 배경), 정보량(선 몇 개, 얼굴 생략)을 모두 못박았다.
        "prompt": (
            "loose felt-tip marker outlines, only three flat tonal values, large empty white areas, "
            "background reduced to a few guide lines, figures simplified without facial features, "
            "animation storyboard panel, hand drawn on paper"
        ),
        "negative": (
            "photograph, photorealistic, greyscale photo, detailed rendering, fine texture, "
            "smooth shading gradients, realistic face, intricate architecture, dark heavy image, "
            "vibrant colour, sharp focus"
        ),
        "tier": TIER_DRAFT,
        # 값싼 모델(Core)은 '러프'를 무시하고 완성된 잉크 일러스트를 그린다. 실측으로 확인했다.
        # 러프가 목적인데 완성된 그림이 나오면 그 비용은 그냥 버리는 것이라 하한을 둔다.
        "minTier": TIER_STANDARD,
        "minTierReason": "값싼 모델이 '러프' 지시를 무시해 표준 등급으로 올렸습니다.",
    },
    {
        "id": "storyboard-pencil",
        # lead 문구를 세 번 바꿔 보고 고른 값이다(각 1장 생성해 눈으로 비교).
        #   "A light graphite pencil storyboard sketch on plain white paper"  <- 채택
        #   "A crude unfinished pencil gesture drawing on white paper"        검은 면을 크게 칠했다
        # 'crude', 'gesture drawing' 처럼 거칠기를 강조하는 어휘는 SD3.5에서 고대비 잉크
        # 일러스트로 기울었다. 연필·흰 종이를 담백하게 선언하고 정보량은 prompt에서 제한한다.
        "lead": "A light graphite pencil storyboard sketch on plain white paper",
        "label": "연필 콘티 (형태만)",
        "hint": "연필 선으로 사람·물체의 형태만. 촬영 전 구도 검토용",
        "keywords": [
            "형태만", "형태", "윤곽", "윤곽선", "밑그림", "데생", "선만",
            "연필", "연필 콘티", "실루엣", "pencil", "pencil sketch",
        ],
        # 실제 촬영 콘티는 이 수준이다. 화면 안에 무엇이 어디에 있는지만 보이면 되고
        # 얼굴·질감·명암은 그리지 않는다. 그래서 "덜 그리기"를 매체·정보량·명암 세 축으로 못박는다.
        #
        # storyboard-rough와의 차이:
        #   rough  = 마커 선 + 평면 명암 3단계 (약간의 톤이 있다)
        #   pencil = 연필 윤곽선만, 명암 0단계 (톤이 아예 없다)
        # 둘을 한 프리셋으로 합치면 지시가 서로 상쇄되어 어중간한 그림이 나온다.
        # 정보량을 숫자로 못박는다. "형태만"이라는 말로는 모델이 완성된 그림을 그린다.
        # 실측 이력:
        #   1차 시도에 "construction and perspective guide lines left visible"를 넣었더니
        #   모델이 건축 투시도를 그렸다. 얼굴도 눈·머리카락까지 렌더링됐다.
        #   원인은 정보량을 제한하는 문구가 없고, 오히려 선을 더 그리라고 초대한 것이다.
        # 그래서 선 개수·얼굴 생략·빈 공간을 직접 지시한다.
        "prompt": (
            "drawn with only a dozen thin pencil strokes, extremely minimal, faces left completely "
            "blank with no eyes nose or mouth, bodies as plain outlined silhouettes, furniture and "
            "props as bare rectangles and ovals, background suggested by two or three lines at most, "
            "most of the paper left empty white, flat even line weight, no tone anywhere, "
            "quick thirty second thumbnail, single storyboard panel"
        ),
        "negative": (
            "colour, ink, felt-tip marker, brush stroke, photograph, photorealistic, greyscale photo, "
            "shading, hatching, cross-hatching, smooth gradients, rendered texture, grey tone, "
            "detailed facial features, eyes, hair detail, fabric folds, detailed background, "
            "architectural drawing, technical drawing, perspective grid, blueprint, draftsman, "
            "heavy black fill, grey fill, solid grey areas, filled surfaces, dark image, "
            "high contrast, painterly, polished, finished illustration"
        ),
        "tier": TIER_DRAFT,
        # 러프 콘티와 같은 이유다. 값싼 모델은 "형태만"을 무시하고 완성된 연필 일러스트를 그린다.
        "minTier": TIER_STANDARD,
        "minTierReason": "값싼 모델이 '형태만' 지시를 무시해 표준 등급으로 올렸습니다.",
    },
    {
        "id": "cinematic-real",
        "lead": "A photorealistic cinematic film still",
        "label": "실사 시네마틱",
        "hint": "촬영된 드라마 스틸 느낌",
        "keywords": ["실사", "시네마틱", "영화", "드라마", "촬영", "사진", "리얼", "photo", "실제"],
        "prompt": (
            "photorealistic cinematic film still, anamorphic lens, shallow depth of field, "
            "natural skin texture, motivated practical lighting, subtle film grain, 35mm"
        ),
        "negative": "illustration, cartoon, anime, painting, cgi look",
        "tier": TIER_PREMIUM,
    },
    {
        "id": "noir-mono",
        "lead": "A high contrast black and white film noir still",
        "label": "느와르 흑백",
        "hint": "강한 명암의 흑백",
        "keywords": ["느와르", "흑백", "모노", "누아르", "noir", "그림자"],
        "prompt": (
            "high contrast black and white noir cinematography, hard chiaroscuro lighting, deep "
            "shadows, venetian blind light shafts, smoky atmosphere, 1950s film noir"
        ),
        "negative": "colour, flat lighting, cheerful mood",
        "tier": TIER_STANDARD,
    },
    {
        "id": "anime-cel",
        "lead": "A Japanese anime cel animation frame",
        "label": "애니메이션 셀",
        "hint": "일본 애니메이션 작화",
        "keywords": ["애니", "애니메", "재패니메이션", "anime", "셀"],
        "prompt": (
            "Japanese anime cel animation style, crisp outlines, flat two-tone shading, "
            "detailed painted background, vivid key light, 90s theatrical anime look"
        ),
        "negative": "photorealistic, 3d render, western comic style",
        "tier": TIER_STANDARD,
    },
    {
        "id": "watercolor",
        "lead": "A soft watercolour painting",
        "label": "수채화",
        "hint": "번지는 물감 질감",
        "keywords": ["수채", "水彩", "물감", "파스텔화", "watercolor", "번지"],
        "prompt": (
            "delicate watercolour illustration, visible paper grain, soft wet-on-wet colour bleeds, "
            "loose ink outlines, muted harmonious palette, hand painted"
        ),
        "negative": "hard digital edges, photorealistic, 3d render, neon",
        "tier": TIER_STANDARD,
    },
    {
        "id": "neon-cyber",
        "lead": "A neon cyberpunk concept art painting",
        "label": "네온 사이버",
        "hint": "네온 조명의 근미래",
        "keywords": ["사이버", "네온", "미래", "sf", "cyberpunk", "홀로그램"],
        "prompt": (
            "neon-soaked cyberpunk concept art, magenta and cyan rim light, wet reflective streets, "
            "holographic signage, volumetric haze, high tech low life atmosphere"
        ),
        "negative": "daylight, pastoral, flat lighting",
        "tier": TIER_STANDARD,
    },
    {
        "id": "dark-fantasy",
        "lead": "A dark fantasy oil painting",
        "label": "다크 판타지",
        "hint": "어둡고 묵직한 판타지",
        "keywords": ["판타지", "다크", "중세", "무협", "fantasy", "어둡"],
        "prompt": (
            "dark fantasy painted illustration, heavy oil brushwork, desaturated earth palette, "
            "dramatic god rays, ornate costume detail, epic scale"
        ),
        "negative": "bright cheerful palette, cartoon, flat shading",
        "tier": TIER_PREMIUM,
    },
    {
        "id": "pastel-slice",
        "lead": "A warm pastel slice-of-life illustration",
        "label": "파스텔 일상",
        "hint": "밝고 따뜻한 일상물",
        "keywords": ["파스텔", "일상", "로맨스", "밝고", "따뜻", "청춘", "슬라이스"],
        "prompt": (
            "warm pastel slice-of-life illustration, soft diffused afternoon light, gentle pink and "
            "cream palette, light bloom, cozy intimate framing"
        ),
        "negative": "dark grim palette, harsh shadows, horror",
        "tier": TIER_STANDARD,
    },
    {
        "id": "ink-brush",
        "lead": "An east asian ink brush painting",
        "label": "수묵 붓터치",
        "hint": "동양화 붓 느낌",
        "keywords": ["수묵", "동양화", "붓", "먹", "한국화", "ink"],
        "prompt": (
            "east asian ink brush painting, sumi-e, expressive single-stroke brushwork, generous "
            "negative space, rice paper texture, restrained ink tones with one accent colour"
        ),
        "negative": "digital gradients, photorealistic, neon, 3d render",
        "tier": TIER_STANDARD,
    },
    {
        "id": "3d-render",
        "lead": "A stylised 3d animated film render",
        "label": "3D 렌더",
        "hint": "3D 애니메이션 룩",
        "keywords": ["3d", "쓰리디", "렌더", "픽사", "cg", "입체"],
        "prompt": (
            "stylised 3d animated film render, soft global illumination, subsurface skin shading, "
            "clean topology, cinematic depth of field, pixar-like character design"
        ),
        "negative": "2d line art, flat illustration, photorealistic documentary",
        "tier": TIER_STANDARD,
    },
]

_PRESETS_BY_ID = {preset["id"]: preset for preset in STYLE_PRESETS}
DEFAULT_PRESET_ID = "webtoon-color"


def describe() -> dict:
    """프론트가 그림체 선택 UI를 그릴 때 쓰는 메타데이터."""
    return {
        "presets": [
            {"id": p["id"], "label": p["label"], "hint": p["hint"], "tier": p["tier"]}
            for p in STYLE_PRESETS
        ],
        "tiers": [{"id": key, **value} for key, value in TIERS.items()],
        "purposes": [
            {"id": "storyboard", "label": "콘티 (러프)"},
            {"id": "scene", "label": "장면 이미지"},
            {"id": "keyart", "label": "키비주얼 · 포스터"},
        ],
    }


def get_preset(preset_id: str | None) -> dict:
    return _PRESETS_BY_ID.get(preset_id or "", _PRESETS_BY_ID[DEFAULT_PRESET_ID])


def match_by_keyword(style_text: str) -> dict | None:
    """사용자가 쓴 문장에서 그림체를 규칙으로 찾는다.

    LLM 라우팅이 실패해도 그림체 선택이 멈추지 않게 하는 안전망이다.
    """
    text = (style_text or "").lower()
    if not text.strip():
        return None
    best: tuple[int, dict] | None = None
    for preset in STYLE_PRESETS:
        hits = sum(1 for keyword in preset["keywords"] if keyword.lower() in text)
        if hits and (best is None or hits > best[0]):
            best = (hits, preset)
    return best[1] if best else None


def apply_min_tier(preset: dict, tier: str, reason: str) -> dict:
    """프리셋이 최소 등급을 요구하면 올린다.

    값싼 모델이 그 그림체의 핵심 지시를 무시하는 경우가 있어, 결과가 쓸모없어지는 것을 막는다.
    올렸다는 사실과 이유를 함께 돌려주어 사용자가 왜 비용이 더 들었는지 알 수 있게 한다.
    """
    min_tier = preset.get("minTier")
    if not min_tier or tier not in TIER_ORDER or min_tier not in TIER_ORDER:
        return {"tier": tier, "reason": reason, "tierRaised": False}
    if TIER_ORDER.index(tier) >= TIER_ORDER.index(min_tier):
        return {"tier": tier, "reason": reason, "tierRaised": False}
    note = preset.get("minTierReason") or "이 그림체는 더 높은 등급이 필요합니다."
    return {
        "tier": min_tier,
        "reason": f"{reason} {note}".strip(),
        "tierRaised": True,
    }


_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)

_ROUTER_INSTRUCTION = """당신은 웹툰 IP를 드라마로 옮기는 제작사의 아트 디렉터입니다.
기획자가 한국어로 원하는 그림체를 적었습니다. 아래 프리셋 중 가장 가까운 것을 고르고,
프리셋만으로 담기지 않는 요구는 영어 프롬프트 조각으로 따로 적어 주세요.

프리셋 목록:
{presets}

품질 등급(용도에 따라 고릅니다):
- draft: 러프 콘티를 여러 장 뽑을 때. 가장 빠르고 저렴합니다.
- standard: 기본값. 프롬프트를 가장 정확히 따릅니다.
- premium: 최종 키비주얼이나 포스터. 느리고 비쌉니다.

규칙:
1. presetId는 반드시 위 목록의 id 중 하나여야 합니다.
2. tier는 기획자가 명시적으로 품질이나 속도를 요구한 경우에만 기본값에서 바꿉니다.
   기본값은 용도에 따라 정해져 있고 이미 알려 드립니다.
3. extraPrompt에는 프리셋에 없는 구체적 요구만 영어로 짧게 적습니다(색감, 시대, 참고 작품,
   카메라, 질감 등). 없으면 빈 문자열로 둡니다. 그림체와 무관한 장면 설명은 넣지 않습니다.
4. avoid에는 기획자가 명확히 원하지 않는다고 말한 것만 영어로 적습니다. 없으면 빈 문자열.
5. reason은 왜 그 프리셋을 골랐는지 한국어 한 문장.
6. JSON 하나만 출력합니다. 설명이나 코드펜스를 붙이지 않습니다.

출력 형식:
{{"presetId": "...", "tier": "draft|standard|premium", "extraPrompt": "...", "avoid": "...", "reason": "..."}}"""


def _preset_lines() -> str:
    return "\n".join(f"- {p['id']}: {p['label']} — {p['hint']}" for p in STYLE_PRESETS)


def route(
    style_text: str,
    purpose: str = "scene",
    has_reference: bool = False,
    caller=None,
) -> dict:
    """그림체 요청을 실제 생성 파라미터로 바꾼다.

    caller가 있으면 LLM으로 판단하고, 없거나 실패하면 키워드 규칙으로 되돌린다.
    반환값은 사용자에게 그대로 보여줄 수 있어야 한다. 어떤 판단이 있었는지 감출 이유가 없다.
    """
    default_tier = PURPOSE_TIERS.get(purpose, TIER_STANDARD)
    decision = {
        "presetId": None,
        "tier": default_tier,
        "extraPrompt": "",
        "avoid": "",
        "reason": "",
        "source": "default",
    }

    if caller and (style_text or "").strip():
        try:
            system = _ROUTER_INSTRUCTION.format(presets=_preset_lines())
            user = (
                f"[용도] {purpose} (기본 품질 등급: {default_tier})\n"
                f"[기획자가 원하는 그림체]\n{style_text.strip()[:1500]}"
            )
            text, _ = caller(system, user, 500, 0.2)
            match = _JSON_BLOCK.search(text or "")
            if match:
                parsed = json.loads(match.group(0))
                preset_id = str(parsed.get("presetId") or "").strip()
                if preset_id in _PRESETS_BY_ID:
                    decision["presetId"] = preset_id
                    decision["source"] = "ai"
                tier = str(parsed.get("tier") or "").strip()
                if tier in TIERS:
                    decision["tier"] = tier
                decision["extraPrompt"] = str(parsed.get("extraPrompt") or "").strip()[:400]
                decision["avoid"] = str(parsed.get("avoid") or "").strip()[:200]
                decision["reason"] = str(parsed.get("reason") or "").strip()[:200]
        except Exception as error:  # noqa: BLE001 - 판단 실패가 생성을 막아서는 안 된다
            logger.warning("스타일 라우팅에 실패해 규칙으로 처리합니다: %s", error)

    if not decision["presetId"]:
        matched = match_by_keyword(style_text)
        if matched:
            decision["presetId"] = matched["id"]
            decision["source"] = "keyword"
            decision["reason"] = decision["reason"] or f"'{matched['label']}' 키워드로 판단했습니다."
        else:
            decision["presetId"] = DEFAULT_PRESET_ID
            decision["reason"] = decision["reason"] or "그림체 지시가 없어 기본 웹툰 채색으로 진행합니다."

    preset = get_preset(decision["presetId"])
    decision.update(apply_min_tier(preset, decision["tier"], decision["reason"]))
    decision["presetLabel"] = preset["label"]
    # 참조 이미지가 있으면 프롬프트로 못 만드는 원작 화풍을 그대로 따라간다.
    decision["useStyleReference"] = bool(has_reference)
    decision["lead"] = preset["lead"]
    decision["stylePrompt"] = preset["prompt"]
    if decision["extraPrompt"]:
        decision["stylePrompt"] += f", {decision['extraPrompt']}"
    decision["negativePrompt"] = ", ".join(
        part for part in [preset["negative"], decision["avoid"], BASE_NEGATIVE] if part
    )
    return decision


def _lower_first(text: str) -> str:
    """문장 중간에 끼워 넣을 조각의 첫 글자를 내린다.

    고유명사는 두 번째 글자가 대문자인 경우(예: "McKay", "iPhone")를 제외해 보호한다.
    video_prompt에 같은 이름의 함수가 있지만, 그 모듈은 영상 프롬프트 전용이라
    이미지 경로가 그것을 임포트하면 두 모듈이 서로를 부르게 된다. 짧아서 따로 둔다.
    """
    if not text:
        return text
    if len(text) > 1 and text[1].isupper():
        return text
    return text[0].lower() + text[1:]


def compose_prompt(scene_prompt: str, decision: dict) -> str:
    """장면 설명과 그림체를 하나의 프롬프트로 합친다.

    **매체를 문장 맨 앞에 둔다.** 확산 모델은 앞쪽 토큰에 훨씬 크게 반응한다.
    장면을 먼저 쓰고 그림체를 뒤에 붙였을 때는 러프 콘티를 요청해도 사실적인 사진이 나왔다.
    "A rough pencil storyboard sketch of ..." 형태로 매체를 선언하면 의도대로 따라온다.
    """
    scene = (scene_prompt or "").strip().rstrip(".")
    lead = (decision.get("lead") or "").strip()
    detail = (decision.get("stylePrompt") or "").strip()

    if not scene:
        return f"{lead}. {detail}"[:9000] if lead else detail[:9000]
    if not lead:
        return f"{scene}. {detail}"[:9000]
    # "... on plain white paper of A young woman stands ..." 처럼 대문자로 이어지면
    # 문장 경계가 흐려져 매체 선언과 장면이 서로 다른 문장으로 읽힌다.
    # 매체를 앞에 두는 효과를 살리려면 하나의 문장이어야 한다.
    return f"{lead} of {_lower_first(scene)}. {detail}"[:9000]
