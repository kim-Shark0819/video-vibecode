"""배포 없이 산출물 프롬프트를 실제 Bedrock에 걸어 품질과 JSON 파싱을 확인한다.

프롬프트나 스키마를 바꿨을 때 이미지를 빌드하기 전에 먼저 돌린다.
결과는 .build/sample-{kind}.json 으로 남는다.

사용법 (자격증명은 세션 환경변수로 주입한 뒤):
    python AI-POC/deploy/verify/try-documents.py analysis adaptation logline characters

종류 뒤에 콜론으로 옵션을 붙일 수 있다.
    treatment:6                              6부작 (예전 문법 그대로)
    treatment:episodes=6,pages=10,detail=full  A4 10장 · 상세
세부사항 자유 입력은 쉼표가 섞이므로 환경변수로 받는다.
    $env:DOC_TRY_DETAILS = "결말은 열린 결말로"
"""

import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))          # toon-change-ai-pj
_WORKSPACE_ROOT = os.path.abspath(os.path.join(_PROJECT_ROOT, ".."))
sys.path.insert(0, os.path.join(_PROJECT_ROOT, "app"))

import boto3
from botocore.config import Config

from src import documents

SOURCE = """
[1화] 향 도둑
2035년 서울. 기억은 향으로 저장된다. 사람들은 결혼식, 첫사랑, 어머니의 손맛 같은 순간을
작은 유리병에 담아 보관한다. 기억 거래는 반합법이다. 국가 기억관리국이 유통을 관리하지만
암시장은 그보다 크다.

유하(19)는 무면허 기억 브로커다. 낮에는 편의점에서 일하고 밤에는 남의 기억을 훔쳐 판다.
유하에게는 일곱 살 이전의 기억이 없다. 통째로 비어 있다.

[2화] 의뢰
남자가 찾아온다. 정겸(34). 기억관리국 수사관이다. 그는 유하를 잡으러 온 게 아니라
의뢰를 하러 왔다. 죽은 어머니의 마지막 기억을 사고 싶다고 한다. 그 기억은 이미
암시장에 팔렸고, 유하가 그걸 훔친 당사자다.

[3화] 얼굴
유하가 훔친 기억을 다시 열어본다. 정겸의 어머니가 마지막으로 본 얼굴은 일곱 살의 유하다.
어머니는 유하의 손을 잡고 있었다. 유하는 자기가 왜 그 자리에 있었는지 모른다.

[4화] 거래
유하와 정겸은 거래를 한다. 유하는 정겸에게 기억을 돌려주고, 정겸은 유하의 잃어버린
일곱 살을 찾아준다. 둘은 기억관리국 지하 창고로 향한다. 거기에 폐기 처분된 기억들이 있다.

[5화] 규칙
기억을 두 번 이상 옮기면 원본이 훼손된다. 유하가 훔친 기억은 이미 세 번 옮겨졌다.
정겸의 어머니 얼굴이 점점 흐려진다. 유하는 자기 기억을 포기하고 정겸의 기억을 복원하기로 한다.
"""


# 진입점 2 상황: 짧은 소재 메모만 있는 상태
LOGLINE_SOURCE = """
로그라인: 기억을 향으로 저장하는 세계에서, 자기 기억을 잃은 소녀가 남의 기억을 훔쳐 판다.

메모
- 배경은 2035년 서울, 기억 거래가 반합법
- 주인공은 19세 무면허 브로커
- 훔친 기억 속에 자기 얼굴이 있다는 것이 1막 후반 훅
- 상대역은 기억관리국 수사관
"""

DIRECTION = {
    "targetPlatform": "넷플릭스",
    "genres": ["SF", "스릴러", "멜로"],
    "episodeCount": 6,
    "tone": "서늘하고 절제된 톤, 감정은 폭발하는 지점만 크게",
    "audience": "20~30대",
    "freeDirection": "향 설정은 반드시 살릴 것. 신체 훼손 묘사는 피한다.",
}


def fixture_for(kind: str, episode_count: int | None = None) -> tuple[dict, str]:
    """종류에 맞는 진입점 상황을 준다. logline은 자료가 짧은 상태를 재현한다."""
    direction = dict(DIRECTION)
    if episode_count:
        direction["episodeCount"] = episode_count
    if kind == "logline":
        return (
            {
                "title": "향 도둑 (가제)",
                "story": "기억을 향으로 저장하는 세계의 기억 브로커 이야기.",
                "inputType": "logline",
                "direction": direction,
            },
            LOGLINE_SOURCE,
        )
    return (
        {
            "title": "향 도둑",
            "story": "기억을 향으로 저장하는 세계에서, 자기 기억을 잃은 소녀가 남의 기억을 훔쳐 판다.",
            "inputType": "webtoon",
            "direction": direction,
        },
        SOURCE,
    )


def load_context(kind: str) -> dict:
    """앞 단계 결과를 맥락으로 넣어본다. 이전 실행이 남긴 .build/sample-*.json을 재사용한다."""
    spec = documents.KIND_SPECS[kind]
    context = {}
    for source_kind in spec.get("contextKinds", ()):
        path = os.path.join(_WORKSPACE_ROOT, ".build", f"sample-{source_kind}.json")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as handle:
                context[source_kind] = json.load(handle)
    return context


def make_caller(client, model_id: str):
    """documents 모듈이 기대하는 호출자를 만든다.

    `documents._request_json`은 caller(system, user, max_tokens, temperature) 를 부르고
    (본문, 잘렸는지) 튜플을 받는다. 예전에는 이 스크립트가 boto3 클라이언트를 그 자리에
    그대로 넘겨서 TypeError가 났다.

    운영 경로(gateway.text_caller)와 달리 사용량을 기록하지 않는다. 기록에는 Neptune이
    필요하고, 이 스크립트는 로컬에서 프롬프트만 확인하는 용도다.
    """

    def call(system_text: str, user_text: str, max_tokens: int, temperature: float):
        response = client.converse(
            modelId=model_id,
            messages=[{"role": "user", "content": [{"text": user_text}]}],
            system=[{"text": system_text}],
            inferenceConfig={"maxTokens": max_tokens, "temperature": temperature},
        )
        parts = response.get("output", {}).get("message", {}).get("content", [])
        text = "".join(part.get("text", "") for part in parts).strip()
        stop = response.get("stopReason")
        usage = response.get("usage") or {}
        print(f"    call in={usage.get('inputTokens')} out={usage.get('outputTokens')} stop={stop}")
        return text, stop == "max_tokens"

    return call


def parse_arg(spec_arg: str) -> tuple[str, int | None, dict]:
    """'treatment:episodes=6,pages=10,detail=full' 을 해석한다.

    'treatment:12' 처럼 숫자만 쓰던 예전 문법도 그대로 받는다.
    """
    kind, _, rest = spec_arg.partition(":")
    if not rest:
        return kind, None, {}
    if rest.isdigit():
        return kind, int(rest), {}

    episode_count = None
    options: dict = {}
    for chunk in rest.split(","):
        key, _, value = chunk.partition("=")
        key = key.strip()
        value = value.strip()
        if key == "episodes":
            episode_count = int(value) if value.isdigit() else None
        elif key in {"pages", "detail"}:
            options[key] = int(value) if key == "pages" and value.isdigit() else value
        else:
            raise SystemExit(f"알 수 없는 옵션: {key}")
    return kind, episode_count, options


def report_treatment(payload: dict) -> None:
    """분량 옵션이 실제로 반영됐는지 본다. 이것이 이 스크립트를 돌리는 주된 이유다."""
    narrative = payload.get("narrative") or {}
    sections = payload.get("narrativeSections") or []
    estimate = payload.get("pageEstimate") or {}
    if not narrative:
        print("  [경고] narrative 가 비어 있습니다.")
        return
    print("  본문 섹션별 글자 수:")
    for section in sections:
        text = narrative.get(section["id"]) or ""
        beats = (payload.get("narrativeBeats") or {}).get(section["id"]) or []
        print(f"    {section['label']:<12} {len(text):>6,}자  핵심장면 {len(beats)}개")
    requested = estimate.get("requestedPages")
    print(
        f"  합계 {estimate.get('bodyChars', 0):,}자 = A4 약 {estimate.get('estimatedPages')}장"
        f" (요청 {requested}장, 페이지당 {estimate.get('charsPerPage')}자 기준)"
    )
    if requested:
        ratio = (estimate.get("estimatedPages") or 0) / requested
        verdict = "OK" if 0.7 <= ratio <= 1.4 else "분량이 목표와 많이 다릅니다"
        print(f"  달성률 {ratio:.0%} - {verdict}")


def main() -> int:
    kinds = sys.argv[1:] or ["analysis"]

    # 서버와 같은 조건으로 부른다. 기본 read_timeout(60s)로는 생성 중 요청이 끊기고 재시도된다.
    client = boto3.client(
        "bedrock-runtime",
        region_name="ap-northeast-2",
        config=Config(connect_timeout=10, read_timeout=240, retries={"max_attempts": 1, "mode": "standard"}),
    )
    caller = make_caller(client, documents.TEXT_MODEL)
    free_details = os.getenv("DOC_TRY_DETAILS", "").strip()
    failures = 0
    for spec_arg in kinds:
        kind, episode_count, options = parse_arg(spec_arg)
        if kind not in documents.KIND_SPECS:
            print(f"알 수 없는 종류: {kind}")
            failures += 1
            continue
        if free_details and documents.KIND_SPECS[kind].get("options"):
            options["details"] = free_details
        print(f"\n{'=' * 70}\n{kind} ({documents.KIND_SPECS[kind]['label']})"
              f"{f' / {episode_count}부작' if episode_count else ''}"
              f"{f' / {options}' if options else ''}\n{'=' * 70}")
        project, source = fixture_for(kind, episode_count)
        context = load_context(kind)
        if context:
            print(f"맥락: {list(context)}")
        started = time.time()
        try:
            payload = documents.generate(
                caller, kind, project, source, context=context, options=options or None
            )
        except documents.DocumentError as error:
            print(f"FAILED: {error}")
            failures += 1
            continue
        elapsed = time.time() - started
        body = json.dumps(payload, ensure_ascii=False, indent=2)
        print(f"소요 {elapsed:.1f}s / {len(body):,} bytes")
        print(f"제목: {documents.document_title(kind, payload)}")
        print(f"키: {[k for k in payload if k != '_meta']}")
        if kind == "treatment":
            report_treatment(payload)
        else:
            print(body[:2500])
        out_dir = os.path.join(_WORKSPACE_ROOT, ".build")
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, f"sample-{kind}.json"), "w", encoding="utf-8") as handle:
            handle.write(body)
        print(f"저장: .build/sample-{kind}.json")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
