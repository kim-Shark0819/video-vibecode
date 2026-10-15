"""컨텍스트 팩 — 모든 역할의 입출력 컨테이너.

## 왜 이것이 필요한가

지시서 1절: "역할 간 인터페이스는 JSON 컨텍스트 팩. 역할끼리 서로 부르지 않고 라우터만
부른다. 대화 히스토리를 공유하지 않는다."

역할이 서로를 부르면 호출 그래프가 코드에 흩어지고, 어떤 순서로 무엇이 돌았는지
사후에 알 수 없다. 팩 하나를 라우터가 들고 다니면서 역할에 넘기면
  - 호출 순서는 라우터 코드 한 곳에만 있다
  - 무엇이 근거였는지가 팩에 남는다 (S3 에 저장하면 그대로 재현 가능)
  - trace 로 역할별 토큰·비용·캐시 히트를 한 곳에서 집계할 수 있다

## 대화 히스토리를 공유하지 않는 이유

역할마다 system 프롬프트가 다르고 목적이 다르다. 히스토리를 넘기면 앞 역할의 지시가
뒤 역할의 system 을 밀어낸다(모델은 뒤에 온 지시를 더 강하게 따르는 경향이 있다 —
prompt_layers.compose_system 이 우선순위 문장을 넣는 것도 같은 이유).
또 히스토리가 쌓이면 입력 토큰이 회차마다 누적되어 비용이 제곱으로 늘어난다.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

# 스토리 타입 (원문이 무엇인가)
TYPE_SCRIPT = "script"
TYPE_TREATMENT = "treatment"
TYPE_LOGLINE = "logline"
STORY_TYPES = (TYPE_SCRIPT, TYPE_TREATMENT, TYPE_LOGLINE)

STORY_TYPE_LABELS = {
    TYPE_SCRIPT: "대본",
    TYPE_TREATMENT: "트리트먼트",
    TYPE_LOGLINE: "로그라인",
}

# 결과물 종류
OUT_LOGLINE = "logline"
OUT_STORYBOARD = "storyboard"
OUT_SCRIPT = "script"
OUTPUT_KINDS = (OUT_LOGLINE, OUT_STORYBOARD, OUT_SCRIPT)

OUTPUT_KIND_LABELS = {
    OUT_LOGLINE: "로그라인",
    OUT_STORYBOARD: "콘티",
    OUT_SCRIPT: "대본",
}

# 회차 분량
LENGTH_SHORT = "short"
LENGTH_NORMAL = "normal"
LENGTH_LONG = "long"
LENGTHS = (LENGTH_SHORT, LENGTH_NORMAL, LENGTH_LONG)
LENGTH_LABELS = {
    LENGTH_SHORT: "짧게",
    LENGTH_NORMAL: "보통",
    LENGTH_LONG: "길게",
}

# 한 번에 만들 수 있는 회차 수 상한. 지시서 3절이 "1회 최대 12화는 설정값"이라고 정했다.
# 회차당 1콜이고 회차당 16,000 출력 토큰이라, 12화면 한 번에 약 19만 토큰이다.
MAX_EPISODES_PER_JOB = 12

# 첨부 이미지 상한. 지시서 3절이 10장으로 정했다.
# 참고: 영상 경로의 상한은 20장이다(Bedrock Converse 의 요청당 이미지 상한).
# 여기서 10장인 이유는 다르다 — 장당 1콜로 **병렬 분석**하므로 Converse 상한과 무관하고,
# 10장을 넘으면 바이블 입력이 커져 오히려 품질이 떨어진다.
MAX_STORY_IMAGES = 10

# 요약 스토리 상한. 화면 입력칸과 같아야 한다.
SUMMARY_MAX_CHARS = 300


def content_hash(*parts: Any) -> str:
    """캐시·재생성 판단용 해시.

    지시서 9절 수용 기준: "원문이 안 바뀌면 바이블을 재생성하지 않는다."
    그 판단의 근거가 이 값이다. 원문·타입·분석 결과를 합쳐 해시하고,
    바이블에 source_hash 로 저장해 다음 요청과 비교한다.

    dict 는 정렬해서 직렬화한다. 키 순서가 바뀌면 내용이 같은데 해시가 달라져
    바이블이 불필요하게 재생성된다.
    """
    hasher = hashlib.sha256()
    for part in parts:
        if part is None:
            hasher.update(b"\x00")
        elif isinstance(part, (dict, list)):
            hasher.update(json.dumps(part, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        else:
            hasher.update(str(part).encode("utf-8"))
        hasher.update(b"\x1f")  # 구분자. 없으면 ("ab","c") 와 ("a","bc") 가 같아진다
    return hasher.hexdigest()


def image_cache_key(sha256: str, model: str) -> str:
    """이미지 분석 캐시 키.

    지시서 1절이 "sha256+model 로 캐시"라고 정했다. 모델을 넣는 이유는 모델이 바뀌면
    분석 결과가 달라지기 때문이다. sha256 만으로 캐시하면 모델을 올려도 옛 분석이
    계속 쓰이고, 그 사실을 알아차릴 방법이 없다.
    """
    return content_hash(sha256, model)[:32]


def new_pack(
    project_id: str,
    story_type: str,
    output_kind: str,
    summary_story: str = "",
    story_text_ref: str = "",
    options: dict | None = None,
) -> dict:
    """빈 컨텍스트 팩. 지시서 1절의 구조를 그대로 따른다."""
    return {
        "project_id": project_id,
        "story_type": story_type,
        "output_kind": output_kind,
        "summary_story": (summary_story or "")[:SUMMARY_MAX_CHARS],
        # 원문은 길다. 팩에 본문을 넣지 않고 S3 키만 넣는다.
        # 팩 자체가 S3 에 저장되므로 본문을 넣으면 같은 텍스트가 두 번 저장된다.
        "story_text_ref": story_text_ref,
        "image_analyses": [],
        "bible": {},
        "options": options or {},
        "trace": [],
    }


def stamp(pack: dict, story_text: str) -> dict:
    """결과물에 남길 "이 결과물은 어느 스토리로 만들었는가".

    ## 왜 필요한가

    프로젝트는 고유한 스토리 하나를 갖고, 결과물은 **그 스토리로 만든 것만** 보관해야
    한다. 그런데 지금까지 결과물 정점에 남는 출처는 `contextPackRef` 하나 — S3 키였다.
    그래서 스토리를 고친 뒤에도 옛 결과물이 같은 프로젝트에 그대로 남고, 그것이 지금
    스토리로 만든 것인지 아닌지를 판별할 방법이 없었다.

    S3 의 팩을 따라가면 `bible.source_hash` 가 있긴 하다. 하지만 그것으로는 부족하다.
      - 팩을 읽으려면 S3 왕복이 필요하다. 목록 조회에서 결과물마다 할 수 없다.
      - `set_story_text` 는 저장할 때마다 **새 키**를 만들고 호출자가 옛 객체를 지운다.
        스토리를 한 번 고치면 옛 팩의 `story_text_ref` 는 없는 객체를 가리킨다.
      - 바이블 정점은 제자리 덮어쓰기라 옛 세대의 해시가 남지 않는다.

    그래서 생성 시점의 지문을 결과물 정점에 직접 박는다. 그래프만 읽어서 판별할 수 있다.

    ## 각 값의 역할

      storyHash      원문 + 타입 + 이미지 분석의 지문(바이블의 source_hash 와 같은 값).
                     권위 있는 신원. 같은 값이면 같은 입력으로 만든 것이다.
      storyTextRef   생성 당시 원문의 S3 키. 원문이 교체되면 프로젝트의 키가 바뀌므로,
                     **S3 를 읽지 않고** 그래프의 두 문자열만 비교해 수정 여부를 안다.
      storyType      생성 당시 스토리 타입. 원문은 그대로인데 타입만 바꾼 경우를 잡는다.
      storySnapshot  생성 당시 요약 스토리(≤300자). 사람이 화면에서 바로 읽는 값이다.
      storyCharCount 생성 당시 원문 글자 수. "12,400자 → 15,900자" 처럼 보여 준다.

    `pack` 과 원문만 받는 순수 함수다. 요청 컨텍스트에 의존하지 않으므로 백그라운드
    스레드(대본 아웃라인)에서도 같은 함수를 쓴다.
    """
    bible = pack.get("bible") or {}
    return {
        "storyHash": str(bible.get("source_hash") or "")[:64],
        "storyTextRef": str(pack.get("story_text_ref") or "")[:400],
        "storyType": str(pack.get("story_type") or "")[:20],
        "storySnapshot": str(pack.get("summary_story") or "")[:SUMMARY_MAX_CHARS],
        # 원문 글자 수는 프로젝트 속성이 아니라 실제로 모델에 넣은 텍스트에서 센다.
        # S3 읽기가 실패해 옛 인라인 필드로 폴백한 경우 두 값이 다를 수 있고,
        # 남겨야 하는 것은 "무엇을 근거로 만들었는가" 쪽이다.
        "storyCharCount": len(story_text or ""),
    }


def stamp_drift(saved: dict, project: dict) -> str:
    """결과물의 스탬프와 지금 프로젝트의 스토리를 비교한다. 다르면 이유를 돌려준다.

    S3 를 읽지 않는다. 그래프에 있는 값만 쓴다 — 목록 조회에서 결과물 수만큼
    S3 왕복이 생기면 화면이 느려진다.

    빈 문자열이면 "어긋나지 않았다"가 아니라 **"판단하지 않았다"** 다. 이 코드 이전에
    만들어진 결과물은 스탬프가 없으므로, 없는 것을 근거로 경고를 띄우면 전부 수정된
    것처럼 보인다.

    타입만 검사에서 빠뜨리지 않는 이유: 원문을 그대로 두고 타입만 대본→트리트먼트로
    바꾸면 바이블 입력이 달라져 결과물의 근거가 바뀐다. 그런데 원문 키는 그대로다.

    이미지 첨부 변경은 여기서 잡지 못한다. 그것을 알려면 storyHash 를 다시 계산해야
    하고, 계산에는 이미지 분석 결과가 필요하다. 다음 생성 때 자연히 드러난다.
    """
    if not isinstance(saved, dict):
        return ""
    ref = str(saved.get("storyTextRef") or "")
    story_type = str(saved.get("storyType") or "")

    current_ref = str((project.get("storyText") or {}).get("key") or "")
    current_type = str(project.get("storyType") or "")

    if ref and current_ref and ref != current_ref:
        return "story_text"
    if story_type and current_type and story_type != current_type:
        return "story_type"
    return ""


def add_trace(
    pack: dict,
    role: str,
    model: str = "",
    tokens_in: int = 0,
    tokens_out: int = 0,
    cache_hit: bool = False,
    ok: bool = True,
    note: str = "",
) -> dict:
    """역할 한 건의 실행 기록.

    지시서 9절이 수용 기준으로 "trace 로 확인"을 세 번 요구한다.
      - 이미지 없는 요청은 비전 분석가를 호출하지 않는다
      - 같은 이미지 재첨부는 캐시 히트
      - 역할별 토큰·비용이 기록된다
    그 확인이 가능하려면 **호출하지 않은 것도 기록**해야 한다. 그래서 cache_hit=True 인
    항목도 남긴다(호출은 안 했지만 그 역할을 지나갔다는 사실이 필요하다).

    cost 는 넣지 않는다. 이 코드베이스는 토큰만 기록하고 단가를 갖고 있지 않다
    (graph_store.record_usage 에 cost 필드가 없다). 단가 표를 여기에 박으면 AWS 가격이
    바뀔 때 조용히 틀린 값이 쌓인다. 비용은 토큰 × 단가로 조회 시점에 계산해야 한다.
    """
    pack.setdefault("trace", []).append(
        {
            "role": role,
            "model": model,
            "tokens_in": int(tokens_in or 0),
            "tokens_out": int(tokens_out or 0),
            "cache_hit": bool(cache_hit),
            "ok": bool(ok),
            "note": note[:200],
        }
    )
    return pack


def is_call(row: dict) -> bool:
    """이 trace 행이 **실제 모델 호출**인지.

    캐시 히트가 아니어도 호출이 아닌 행이 있다. 라우터는 "이미지가 없어서 부르지
    않았다"는 사실도 기록하는데(analyze_images 주석), 그 행에는 model 이 비어 있다.
    model 이 비었으면 호출이 아니다. 이 구분이 없으면 이미지 없는 요청의 호출 수가
    실제보다 한 건 많게 보인다.
    """
    return bool(row.get("model")) and not row.get("cache_hit")


def trace_totals(pack: dict) -> dict:
    """trace 합계. 화면과 manifest 가 같은 함수를 쓴다."""
    rows = pack.get("trace") or []
    return {
        "calls": sum(1 for row in rows if is_call(row)),
        "cacheHits": sum(1 for row in rows if row.get("cache_hit")),
        "skipped": sum(1 for row in rows if not row.get("model") and not row.get("cache_hit")),
        "tokensIn": sum(int(row.get("tokens_in") or 0) for row in rows),
        "tokensOut": sum(int(row.get("tokens_out") or 0) for row in rows),
        "failed": sum(1 for row in rows if not row.get("ok")),
        "byRole": _by_role(rows),
    }


def _by_role(rows: list[dict]) -> list[dict]:
    grouped: dict[str, dict] = {}
    for row in rows:
        entry = grouped.setdefault(
            row.get("role") or "(unknown)",
            {"role": row.get("role") or "(unknown)", "calls": 0, "cacheHits": 0,
             "tokensIn": 0, "tokensOut": 0, "model": row.get("model") or ""},
        )
        if row.get("cache_hit"):
            entry["cacheHits"] += 1
        elif is_call(row):
            entry["calls"] += 1
        # model 이 비어 있고 캐시도 아닌 행은 "지나갔지만 부르지 않았다"는 기록이다.
        # 어느 쪽으로도 세지 않는다.
        entry["tokensIn"] += int(row.get("tokens_in") or 0)
        entry["tokensOut"] += int(row.get("tokens_out") or 0)
    return sorted(grouped.values(), key=lambda item: -(item["tokensIn"] + item["tokensOut"]))


# --------------------------------------------------------------------------- #
# 역할 입력 조립
# --------------------------------------------------------------------------- #
def bible_input(story_text: str, story_type: str, analyses: list[dict], max_chars: int = 120000) -> str:
    """바이블 작성자에게 넘길 user 메시지.

    긴 원문을 어떻게 다루나 (지시서 6절): "컨텍스트 안이면 통째로, 넘으면 회차/장 단위
    요약을 바이블에 캐시." 여기서는 통째로 넣되 상한을 둔다. 상한을 넘는 원문은
    앞부분과 뒷부분을 함께 남긴다 — 앞만 자르면 결말이 사라지고, 뒤만 자르면 인물 소개가
    사라진다. 둘 다 바이블에 필요하다.
    """
    label = STORY_TYPE_LABELS.get(story_type, story_type)
    text = (story_text or "").strip()
    if len(text) > max_chars:
        head = text[: int(max_chars * 0.7)]
        tail = text[-int(max_chars * 0.3):]
        text = f"{head}\n\n[...중간 생략: 원문이 길어 앞 70%와 뒤 30%만 넣었습니다...]\n\n{tail}"

    parts = [f"[원문 타입] {label}", "", "[원문]", text]

    if analyses:
        parts += ["", "[첨부 이미지 분석]"]
        for index, item in enumerate(analyses, start=1):
            parts.append(f"--- 이미지 {index} ({item.get('image_kind') or 'other'}) ---")
            for name in ("setting", "mood_lighting", "art_style"):
                value = str(item.get(name) or "").strip()
                if value:
                    parts.append(f"{name}: {value}")
            for character in item.get("characters") or []:
                descriptor = str(character.get("descriptor") or "").strip()
                if not descriptor:
                    continue
                who = str(character.get("name_if_visible") or "").strip()
                parts.append(f"인물: {f'{who} — ' if who else ''}{descriptor}")
            text_in_image = str(item.get("text_in_image") or "").strip()
            if text_in_image:
                # 말풍선 전사는 사건 정보다. 눈에 띄게 표시해 바이블이 놓치지 않게 한다.
                parts.append(f"이미지 속 텍스트(대사·효과음): {text_in_image[:800]}")
    return "\n".join(parts)


def bible_digest(bible: dict, max_chars: int = 9000) -> str:
    """결과물 작성자에게 넘길 바이블 요약.

    바이블 전체를 JSON 으로 넣지 않는다. 회차마다 넣으면 12화면 같은 내용을 12번
    보내게 되고, 입력 토큰이 회차 수만큼 곱해진다. 사람이 읽는 형태로 압축한다.
    """
    parts: list[str] = []
    if bible.get("title"):
        parts.append(f"[제목] {bible['title']}")
    meta = [f"장르: {bible.get('genre', '')}", f"톤: {bible.get('tone', '')}"]
    parts.append("[기본] " + " / ".join(value for value in meta if value.split(": ", 1)[-1]))
    if bible.get("logline"):
        parts.append(f"[로그라인] {bible['logline']}")

    characters = bible.get("characters") or []
    if characters:
        parts.append("[인물]")
        for character in characters[:12]:
            bits = [character.get("name", "")]
            for key in ("role", "age", "occupation"):
                value = str(character.get(key) or "").strip()
                if value:
                    bits.append(value)
            head = " / ".join(bit for bit in bits if bit)
            detail = []
            for key, label in (("appearance", "외형"), ("personality", "성격"),
                               ("speech", "말투"), ("goal", "목표"), ("conflict", "갈등")):
                value = str(character.get(key) or "").strip()
                if value:
                    detail.append(f"{label}={value}")
            parts.append(f"- {head}" + (f"\n    {'; '.join(detail)}" if detail else ""))

    relationships = bible.get("relationships") or []
    if relationships:
        parts.append("[관계]")
        for item in relationships[:12]:
            parts.append(f"- {item.get('from', '')} → {item.get('to', '')}: {item.get('relation', '')}")

    settings = bible.get("settings") or []
    if settings:
        parts.append("[장소]")
        for item in settings[:8]:
            parts.append(f"- {item.get('name', '')}: {item.get('description', '')} ({item.get('mood', '')})")

    beats = bible.get("plot_beats") or []
    if beats:
        parts.append("[사건 순서]")
        for item in beats[:14]:
            parts.append(f"{item.get('no', '?')}. {item.get('beat', '')}")

    if bible.get("visual_style"):
        parts.append(f"[영상 방향] {bible['visual_style']}")

    joined = "\n".join(parts)
    return joined[:max_chars]


def story_excerpt(story_text: str, max_chars: int = 6000) -> str:
    """결과물 작성자에게 함께 넘길 원문 발췌.

    바이블만으로는 말투와 문체가 사라진다. 원문 일부를 함께 넘겨 대사 톤을 맞추게 한다.
    앞부분을 쓰는 이유: 대본·트리트먼트는 앞에 인물 소개와 톤이 집중되어 있다.
    """
    text = (story_text or "").strip()
    if not text:
        return ""
    return text[:max_chars]
