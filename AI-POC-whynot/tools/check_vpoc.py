"""vpoc1 CI 검사기 — `check:whynot` 이 부르는 유일한 검사기다.

구 검사기 13종은 검사 대상 코드와 함께 지웠다. 정적 검사 통과는 완료 증거가 아니다(R3).
이 검사기가 보는 것은 네 가지뿐이다.

  1. app/src 전체 compileall
  2. 앱 임포트 스모크 (필요한 환경변수는 더미)
  3. node 가 있으면 frontend/public 의 모든 .js 에 node --check (없으면 경고 후 건너뜀, E16)
  4. 잔존 검사 — 구 구현 식별자(PRD 부록 G)의 import·호출이 0건인지
  5. 조립기 자체 검사 5건 (길이 · 한글 · 이름 단어 경계 · 부정어 · 빈 조각) — 모델 호출 없음

실행: python -X utf8 AI-POC-whynot/tools/check_vpoc.py
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]      # AI-POC-whynot
REPO = ROOT.parent                               # 저장소 루트
APP_SRC = ROOT / "app" / "src"
PUBLIC = ROOT / "frontend" / "public"

failures: list[str] = []
warnings: list[str] = []


def fail(message: str) -> None:
    failures.append(message)
    print(f"[FAIL] {message}")


def warn(message: str) -> None:
    warnings.append(message)
    print(f"[WARN] {message}")


def ok(message: str) -> None:
    print(f"[ok  ] {message}")


# ---------------------------------------------------------------- 1. compileall


def check_compileall() -> None:
    result = subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "compileall", "-q", str(APP_SRC)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        fail("compileall 실패\n" + (result.stdout or "") + (result.stderr or ""))
    else:
        ok("compileall app/src")


# ---------------------------------------------------------------- 2. 임포트 스모크


def check_import() -> None:
    env = dict(os.environ)
    env.update(
        {
            "S3_BUCKET": "dummy-bucket",
            "S3_VIDEO_BUCKET": "dummy-video-bucket",
            "VPOC_VIDEO_BUCKET": "dummy-video-bucket",
            "AUTH_JWT_SECRET": "dummy-secret",
            "AWS_REGION": "ap-northeast-2",
            "AWS_DEFAULT_REGION": "ap-northeast-2",
            "AWS_ACCESS_KEY_ID": "dummy",
            "AWS_SECRET_ACCESS_KEY": "dummy",
            "NEPTUNE_ENDPOINT": "",
            "VIDEO_GEN_ENABLED": "true",
            "VPOC_ENABLED": "true",
            "PYTHONIOENCODING": "utf-8",
        }
    )
    code = (
        "import sys; sys.path.insert(0, r'%s');"
        "import src.server as s;"
        "rules=[str(r) for r in s.app.url_map.iter_rules()];"
        "need=['/api/vpoc/p/<pid>','/api/vpoc/smoke'];"
        "missing=[n for n in need if not any(r.startswith(n) for r in rules)];"
        "assert not missing, 'vpoc 라우트 누락: %%s' %% missing;"
        "print('routes', len(rules))" % str(ROOT / "app")
    )
    result = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", code], capture_output=True, text=True, env=env
    )
    if result.returncode != 0:
        fail("앱 임포트 스모크 실패\n" + (result.stdout or "") + (result.stderr or ""))
    else:
        ok("앱 임포트 스모크 — " + (result.stdout or "").strip().splitlines()[-1])


# ---------------------------------------------------------------- 3. node --check


def check_js() -> None:
    node = shutil.which("node")
    if not node:
        warn("node 가 없어 JS 구문 검사를 건너뜁니다 (E16)")
        return
    bad = []
    for path in sorted(PUBLIC.glob("*.js")):
        result = subprocess.run([node, "--check", str(path)], capture_output=True, text=True)
        if result.returncode != 0:
            bad.append(f"{path.name}: {(result.stderr or '').strip()[:400]}")
    if bad:
        fail("node --check 실패\n" + "\n".join(bad))
    else:
        ok(f"node --check ({len(list(PUBLIC.glob('*.js')))} files)")


# ---------------------------------------------------------------- 4. 잔존 검사

# PRD 부록 G. 작업 트리에 import·호출이 0건이어야 하는 구 구현 식별자.
RESIDUE_EVERYWHERE = [
    # 모듈
    "story_generate", "story_i2v", "story_clips", "story_people", "story_steps",
    "video_api", "video_purposes", "story_video", "scene_video", "bedrock_reel",
    "video_callers", "video_cuts", "video_graph", "video_jobs", "video_records",
    "video_spec", "video_store", "video_types", "wan_client", "story_assemble",
    "story_characters", "story_failures", "story_host", "story_results",
    "story_state", "story_text_input", "story_understand", "character_curator",
    # purpose
    "video.story_understanding", "video.keyframe_ref", "video.i2v_prompt", "video.qa",
    # 모델 · 설정
    "amazon.nova-reel", "REQUIRED_IMAGE_SIZE", "IMAGE_MODEL_BY_PROVIDER",
    # 함수 · 상태
    "build_first_frame", "keyframe_reuse_plan", "keyframe_cache_id", "display_orders",
    "PromptTooLong", "PROMPT_REVIEW_REQUIRED", "WAITING_FOR_SOURCE", "SUBMISSION_UNKNOWN",
    # 저장 · 경로
    "project-state.json", "/story-session",
]

# 새 코드에서만 검사한다(구 데이터·플랫폼 파일에는 남아 있어도 된다).
RESIDUE_NEW_CODE_ONLY = [
    "video_scene", "video_job", "video_rating",
    "textInputPreview", "textInputGeneration",
]

# 지워졌어야 하는 파일
MUST_NOT_EXIST = [
    "app/src/video_api.py", "app/src/video_jobs.py", "app/src/video_store.py",
    "app/src/video_graph.py", "app/src/video_records.py", "app/src/video_types.py",
    "app/src/video_callers.py", "app/src/video_cuts.py", "app/src/video_purposes.py",
    "app/src/video_spec.py", "app/src/wan_client.py", "app/src/story_generate.py",
    "app/src/story_i2v.py", "app/src/story_clips.py", "app/src/story_people.py",
    "app/src/story_steps.py", "app/src/story_understand.py", "app/src/story_video",
    "app/src/scene_video", "app/src/roles/video",
    "frontend/public/story-app.js", "frontend/public/scene.js",
    "frontend/public/scene-cuts.js", "frontend/public/production.js",
    "frontend/public/video-folder.js", "frontend/public/story-characters.js",
    "frontend/public/story-text-input.js",
    "docs/prd.md", "docs/prd2.md", "docs/text-input-extension",
    "docs/run-log/story-video-rebuild",
    "tools/check_story_e2e.py", "tools/check_story_i2v.py", "tools/check_story_clips.py",
    "tools/check_video_prd_frontend.py", "tools/check_story_contracts.py",
]

# 검사에서 제외한다. docs/vpoc1 은 PRD 본문이라 식별자가 당연히 나온다.
# review.md 2-b 의 잔존 항목(플랫폼 파일)도 여기 적는다 — 사유는 review.md 에 있다.
EXCLUDE_PARTS = {
    "docs/vpoc1",
    "tools/check_vpoc.py",
    ".kiro/steering/whynot-vpoc1.md",
    "CLAUDE.md",
    # 잔존: 플랫폼 파일. E20 에 따라 import 한 줄 이상의 리팩터링은 하지 않았다.
    "app/src/video_prompt.py",
    "app/src/documents.py",
    "app/src/prompt_layers.py",
    "app/src/graph_store.py",
    "frontend/public/app.js",
    "frontend/public/index.html",
    "frontend/public/styles.css",
    # 잔존: 영상 전용 인프라. 삭제는 Terraform·ArgoCD 절차가 따로 필요하다.
    "k8s-gpu",
    "video-worker",
    "terraform",
    "deploy/verify",
    "deploy/config.env.example",
    "deploy/config.prod.env",
    "docs/구조분석.md",
    "docs/AI-POC_history.md",
    "docs/run-log",
}

SCAN_SUFFIXES = {".py", ".js", ".html", ".css", ".yml", ".yaml", ".tmpl", ".json", ".sh", ".ps1"}


def _iter_files(base: Path):
    for path in base.rglob("*"):
        if not path.is_file() or path.suffix not in SCAN_SUFFIXES:
            continue
        rel = path.relative_to(REPO).as_posix()
        if "/__pycache__/" in rel or rel.endswith(".pyc"):
            continue
        if any(part in rel for part in EXCLUDE_PARTS):
            continue
        yield path, rel


def check_residue() -> None:
    for relative in MUST_NOT_EXIST:
        if (ROOT / relative).exists():
            fail(f"지워야 할 것이 남아 있습니다: AI-POC-whynot/{relative}")

    hits: list[str] = []
    for path, rel in _iter_files(ROOT):
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for needle in RESIDUE_EVERYWHERE:
            if needle in text:
                line_no = text[: text.index(needle)].count("\n") + 1
                hits.append(f"{rel}:{line_no} — 구 식별자 '{needle}'")

    new_code = [APP_SRC / "vpoc", PUBLIC / "vpoc.js", PUBLIC / "vpoc-smoke.js"]
    for base in new_code:
        paths = [base] if base.is_file() else list(base.rglob("*")) if base.exists() else []
        for path in paths:
            if not path.is_file() or path.suffix not in SCAN_SUFFIXES:
                continue
            text = path.read_text(encoding="utf-8")
            for needle in RESIDUE_NEW_CODE_ONLY:
                if needle in text:
                    hits.append(f"{path.relative_to(REPO).as_posix()} — 새 코드에 '{needle}'")

    if hits:
        fail("잔존 검사 실패\n" + "\n".join(f"  {item}" for item in sorted(set(hits))))
    else:
        ok(f"잔존 검사 — 구 식별자 {len(RESIDUE_EVERYWHERE)}종 0건")


# ---------------------------------------------------------------- 5. 조립기 자체 검사


def check_compose() -> None:
    sys.path.insert(0, str(ROOT / "app"))
    from src.vpoc import compose  # noqa: PLC0415

    characters = [
        {
            "id": "c1",
            "nameKo": "서원",
            "nameEn": "Seo Won",
            "appearanceEn": "A lean man with black hair and East Asian features, wearing a white-and-blue racing suit",
            "handleEn": "the black-haired racer",
        }
    ]
    base = {
        "id": "s1",
        "summaryKo": "서원이 걸어온다",
        "characters": ["c1"],
        "locationEn": "A floodlit racing pit lane at night",
        "actionEn": "The black-haired racer walks toward the camera and stops",
        "cameraEn": "Tracking shot at eye level",
        "cameraKo": "인물을 따라가기(트래킹)",
    }
    cases: list[tuple[str, dict, bool, str]] = []

    # (1) 정상 통과 + 조립 결과가 상한 안
    cases.append(("정상 샷", dict(base), True, ""))

    # (2) 길이 상한 — actionEn 260자 초과
    too_long = dict(base)
    too_long["actionEn"] = "The black-haired racer " + ("walks forward " * 30)
    cases.append(("길이 상한", too_long, False, "260자를 넘습니다"))

    # (3) 한글
    hangul = dict(base)
    hangul["actionEn"] = "The racer 걸어온다 toward the camera"
    cases.append(("한글", hangul, False, "한글이 있습니다"))

    # (4) 이름 단어 경계 — "Seo Won" 은 잡고 "won the race" 는 통과
    named = dict(base)
    named["actionEn"] = "Seo Won walks toward the camera and stops"
    cases.append(("이름 단어 경계(잡아야 함)", named, False, "캐릭터 이름"))
    won = dict(base)
    won["actionEn"] = "The black-haired racer who won the race walks toward the camera"
    cases.append(("이름 단어 경계(통과해야 함)", won, True, ""))

    # (5) 부정어와 빈 조각
    negated = dict(base)
    negated["actionEn"] = "The black-haired racer does not look at the camera"
    cases.append(("부정어", negated, False, "부정어가 있습니다"))
    empty = dict(base)
    empty["locationEn"] = ""
    cases.append(("빈 조각", empty, False, "비어 있습니다"))

    bad: list[str] = []
    for label, shot, should_pass, marker in cases:
        problems = compose.check_shot(shot, characters, "live_action")
        passed = not problems
        if passed != should_pass:
            bad.append(f"{label}: 기대 {'통과' if should_pass else '위반'} / 실제 {problems}")
        elif marker and not any(marker in item for item in problems):
            bad.append(f"{label}: 위반 사유에 '{marker}' 가 없습니다 → {problems}")

    prompt = compose.build_prompt(base, characters, "live_action")
    if not prompt.startswith(compose.STYLE["live_action"]):
        bad.append("조립 결과가 화풍 접두어로 시작하지 않습니다")
    if len(prompt) > compose.MAX_PROMPT_CHARS:
        bad.append(f"정상 샷 조립 결과가 상한을 넘습니다 ({len(prompt)}자)")

    if bad:
        fail("조립기 자체 검사 실패\n" + "\n".join(f"  {item}" for item in bad))
    else:
        ok(f"조립기 자체 검사 {len(cases)}건")


# ---------------------------------------------------------------- 6. 멱등 키


def check_client_token() -> None:
    """clientRequestToken 이 AWS 패턴을 만족하는지 본다.

    2026-09-23 dev 에서 4샷 전부 ValidationException 으로 죽었다. planId(`pl_20260922_de38`)와
    shotId(`shot_01`)의 밑줄을 그대로 넣어서다. AWS 패턴은 밑줄을 받지 않는다.
    """
    sys.path.insert(0, str(ROOT / "app"))
    from src.vpoc import video  # noqa: PLC0415

    cases = [
        ("pl_20260922_de38", "shot_01", 1),
        ("pl_20260923_a1b2", "s1", 2),
        ("sm_deadbeef", "s1", 1),
        ("pl.x/y", "shot 3", 10),
        ("__pl__", "__s__", 1),
    ]
    bad: list[str] = []
    seen: dict[str, tuple] = {}
    for plan_id, shot_id, attempt in cases:
        try:
            token = video.client_token(plan_id, shot_id, attempt)
        except Exception as exc:  # noqa: BLE001
            bad.append(f"{plan_id}/{shot_id}/{attempt}: 예외 {exc}")
            continue
        if not video.TOKEN_PATTERN.match(token):
            bad.append(f"{plan_id}/{shot_id}/{attempt} -> {token!r} 이 AWS 패턴을 어깁니다")
        if "_" in token:
            bad.append(f"{plan_id}/{shot_id}/{attempt} -> {token!r} 에 밑줄이 있습니다")
        if not 1 <= len(token) <= 256:
            bad.append(f"{plan_id}/{shot_id}/{attempt} -> 길이 {len(token)}")
        # 같은 입력은 항상 같은 값, 다른 입력은 다른 값이어야 한다(멱등성과 충돌 방지).
        if token in seen and seen[token] != (plan_id, shot_id, attempt):
            bad.append(f"{token!r} 가 {seen[token]} 와 충돌합니다")
        seen[token] = (plan_id, shot_id, attempt)

    first = video.client_token("pl_20260922_de38", "shot_01", 1)
    if first != video.client_token("pl_20260922_de38", "shot_01", 1):
        bad.append("같은 입력이 다른 키를 냅니다 (멱등성 깨짐)")
    if first == video.client_token("pl_20260922_de38", "shot_01", 2):
        bad.append("attempt 가 달라도 같은 키가 나옵니다")

    if bad:
        fail("멱등 키 검사 실패\n" + "\n".join(f"  {item}" for item in bad))
    else:
        ok(f"멱등 키 검사 {len(cases)}건 (예: {first})")


# ---------------------------------------------------------------- 7. 재생성(regen)


def _fake_state() -> dict:
    """새 필드가 전혀 없는 옛 계획 상태. R9 경로를 태운다."""
    return {
        "planId": "pl_20260923_test",
        "projectId": "prj_test",
        "owner": "tester",
        "currentStep": 4,
        "maxReachedStep": 4,
        "task": {"name": None, "status": "idle", "startedAt": None, "finishedAt": None, "error": None},
        "revisions": {"1": 0, "2": 0, "3": 0},
        "finalizedAt": "2026-09-23T05:20:11Z",
        "steps": {
            "1": {"status": "saved", "text": "t", "characters": [], "sceneFiles": []},
            "2": {
                "status": "ready",
                "story": {"style": "live_action", "settingEn": "A pit lane at night"},
                "characters": [
                    {
                        "id": "c1",
                        "nameKo": "서원",
                        "nameEn": "Seo Won",
                        "handleEn": "the black-haired racer",
                        "appearanceEn": "A lean man with black hair",
                    }
                ],
                "questions": [],
                "answers": {},
            },
            "3": {
                "status": "ready",
                "useKeyframes": False,
                "characters": [
                    {"id": "c1", "image": {"status": "ready", "source": "ai", "key": "k.jpg"}}
                ],
                "shots": [
                    {
                        "id": "s1",
                        "summaryKo": "첫 샷",
                        "cameraKo": "고정 샷",
                        "cameraEn": "Static camera",
                        "locationEn": "A pit lane at night",
                        "actionEn": "The black-haired racer stands still",
                        "characters": ["c1"],
                        "promptEn": "Cinematic live-action film footage. A pit lane at night.",
                        "useKeyframe": False,
                        "reviewKo": None,
                    },
                    {
                        "id": "s2",
                        "summaryKo": "둘째 샷",
                        "cameraKo": "트래킹",
                        "cameraEn": "Tracking shot",
                        "locationEn": "A pit lane at night",
                        "actionEn": "The black-haired racer walks forward",
                        "characters": ["c1"],
                        "promptEn": "Cinematic live-action film footage. Tracking shot.",
                        "useKeyframe": False,
                        "reviewKo": None,
                    },
                ],
                "cost": {"shots": 2, "totalUsd": 7.5},
            },
            "4": {
                "status": "done",
                "shots": [
                    {
                        "id": "s1",
                        "status": "completed",
                        "attempt": 1,
                        "mode": "text",
                        "submittedAt": "2026-09-23T05:10:02Z",
                        "completedAt": "2026-09-23T05:13:40Z",
                        "outputKey": "users/tester/a/s1.mp4",
                        "note": None,
                        "error": None,
                    },
                    {
                        "id": "s2",
                        "status": "completed",
                        "attempt": 1,
                        "mode": "text",
                        "submittedAt": "2026-09-23T05:10:03Z",
                        "completedAt": "2026-09-23T05:14:10Z",
                        "outputKey": "users/tester/a/s2.mp4",
                        "note": None,
                        "error": None,
                    },
                ],
                "final": {"status": "ready", "key": "users/tester/a/final.mp4", "error": None},
            },
        },
    }


# PlanView 의 ④ 샷이 반드시 들고 있어야 하는 기존 키(api-check.md).
LEGACY_SHOT4_KEYS = {
    "id", "status", "attempt", "mode", "submittedAt", "completedAt",
    "elapsedS", "videoUrl", "note", "error",
}
REGEN_SHOT4_KEYS = {"selectedAttempt", "versions", "revision", "regen"}

# ④ 자체에 더한 새 키. 생성 회차를 구분한다.
STEP4_KEYS = {"genId"}


def check_regen() -> None:
    import copy  # noqa: PLC0415
    import json as _json  # noqa: PLC0415

    sys.path.insert(0, str(ROOT / "app"))
    from src.vpoc import regen, settings  # noqa: PLC0415

    bad: list[str] = []

    # (a) 새 필드가 없는 옛 state 를 읽어도 기존 키가 그대로고 새 키가 규칙대로 채워진다 (R9)
    state = _fake_state()
    shot = state["steps"]["4"]["shots"][0]
    plan_shot = state["steps"]["3"]["shots"][0]
    versions = regen.versions_view(shot, plan_shot)
    if len(versions) != 1 or versions[0]["attempt"] != 1 or versions[0]["origin"] != "plan":
        bad.append(f"(a) 옛 state 의 versions 가 1개(plan)여야 합니다 → {versions}")
    if regen.revision_view(shot) is not None or regen.regen_view(shot) is not None:
        bad.append("(a) 옛 state 는 revision·regen 이 null 이어야 합니다")
    if regen.max_attempt(shot) != 1:
        bad.append(f"(a) max_attempt 가 1이어야 합니다 → {regen.max_attempt(shot)}")
    if "versions" in state["steps"]["4"]["shots"][0]:
        bad.append("(a) 읽기만 했는데 state 에 versions 가 쓰였습니다 (마이그레이션 금지)")

    # (b) regen 진행 중에도 샷 본체의 status·videoUrl·attempt 는 선택 버전 값이다 (G2)
    state = _fake_state()
    shot = state["steps"]["4"]["shots"][0]
    regen.seed_versions(shot, plan_shot)
    shot["regen"] = {
        "attempt": 2,
        "origin": "regen",
        "status": "submitted",
        "mode": "text",
        "promptEn": "Cinematic live-action film footage. New take.",
        "feedbackKo": "표정을 바꿔 주세요",
        "summaryKo": "새 버전",
        "submittedAt": "2026-09-23T06:00:00Z",
        "outputKey": "",
        "note": None,
        "error": None,
    }
    if shot["status"] != "completed" or shot["outputKey"] != "users/tester/a/s1.mp4":
        bad.append("(b) regen 이 샷 본체를 바꿨습니다")
    if int(shot.get("attempt") or 0) != 1:
        bad.append("(b) regen 이 샷 attempt 를 바꿨습니다")
    if regen.max_attempt(shot) != 2:
        bad.append("(b) max_attempt 가 regen attempt 를 세지 않습니다")
    targets = regen.poll_targets(shot)
    if len(targets) != 2 or targets[1] is not shot["regen"]:
        bad.append("(b) poll_targets 가 regen 을 대상에 넣지 않습니다")
    view = regen.regen_view(shot)
    if not view or view["status"] != "submitted" or view["attempt"] != 2:
        bad.append(f"(b) regen_view 가 잘못됐습니다 → {view}")

    # (c) select 뒤 다른 샷 데이터가 변하지 않는다
    state = _fake_state()
    other_before = _json.dumps(state["steps"]["4"]["shots"][1], sort_keys=True, ensure_ascii=False)
    step3_before = _json.dumps(state["steps"]["3"], sort_keys=True, ensure_ascii=False)
    shot = state["steps"]["4"]["shots"][0]
    regen.seed_versions(shot, state["steps"]["3"]["shots"][0])
    shot["versions"].append(
        {
            "attempt": 2,
            "origin": "regen",
            "feedbackKo": "표정을 바꿔 주세요",
            "summaryKo": "새 버전",
            "promptEn": "Cinematic live-action film footage. New take.",
            "mode": "text",
            "status": "completed",
            "outputKey": "users/tester/a/s1-v2.mp4",
            "submittedAt": "2026-09-23T06:00:00Z",
            "completedAt": "2026-09-23T06:03:24Z",
            "createdAt": "2026-09-23T06:00:00Z",
        }
    )
    shot["regen"] = None
    result = regen.apply_select(state, shot_id="s1", attempt=2)
    if result["attempt"] != 2 or result["previousAttempt"] != 1:
        bad.append(f"(c) apply_select 결과가 잘못됐습니다 → {result}")
    if shot["outputKey"] != "users/tester/a/s1-v2.mp4" or shot["selectedAttempt"] != 2:
        bad.append("(c) select 이 선택 버전을 샷에 반영하지 않았습니다")
    if not result["stitchStale"] or state["steps"]["4"]["final"]["status"] != "stale":
        bad.append("(c) 버전을 바꿨는데 합본이 stale 이 되지 않았습니다")
    other_after = _json.dumps(state["steps"]["4"]["shots"][1], sort_keys=True, ensure_ascii=False)
    step3_after = _json.dumps(state["steps"]["3"], sort_keys=True, ensure_ascii=False)
    if other_before != other_after:
        bad.append("(c) 다른 샷 데이터가 바뀌었습니다")
    if step3_before != step3_after:
        bad.append("(c) ③ 계획이 바뀌었습니다")

    # 되돌리기: 이전 버전 유지 = 같은 attempt 로 select
    state2 = copy.deepcopy(state)
    back = regen.apply_select(state2, shot_id="s1", attempt=1)
    if back["attempt"] != 1 or state2["steps"]["4"]["shots"][0]["selectedAttempt"] != 1:
        bad.append("(c) 이전 버전으로 되돌릴 수 없습니다")

    # (d) 스위치가 꺼지면 404
    original = settings.REGEN_ENABLED
    try:
        settings.REGEN_ENABLED = False
        if regen.enabled():
            bad.append("(d) 스위치를 껐는데 enabled() 가 True 입니다")
        try:
            regen.require_enabled("test")
            bad.append("(d) 스위치가 꺼졌는데 require_enabled 가 통과했습니다")
        except Exception as exc:  # noqa: BLE001
            if getattr(exc, "status", None) != 404:
                bad.append(f"(d) 404 가 아니라 {getattr(exc, 'status', '?')} 입니다")
    finally:
        settings.REGEN_ENABLED = original

    # 선행 조건: 진행 중이면 409, 의견이 비면 400, 너무 길면 400
    state3 = _fake_state()
    for feedback, want in (("", "FeedbackEmpty"), ("가" * 501, "FeedbackTooLong")):
        try:
            regen.check_revise_preconditions(state3, "s1", feedback)
            bad.append(f"선행 조건 검사가 {want} 를 잡지 못했습니다")
        except Exception as exc:  # noqa: BLE001
            if getattr(exc, "kind", "") != want:
                bad.append(f"{want} 대신 {getattr(exc, 'kind', '?')} 가 났습니다")
    state3["steps"]["4"]["shots"][0]["regen"] = {"status": "submitted", "attempt": 2}
    try:
        regen.check_revise_preconditions(state3, "s1", "표정을 바꿔 주세요")
        bad.append("진행 중인데 RegenRunning 이 나지 않았습니다")
    except Exception as exc:  # noqa: BLE001
        if getattr(exc, "kind", "") != "RegenRunning":
            bad.append(f"RegenRunning 대신 {getattr(exc, 'kind', '?')} 가 났습니다")

    if bad:
        fail("재생성 검사 실패\n" + "\n".join(f"  {item}" for item in bad))
    else:
        ok("재생성 검사 (a)옛 state (b)G2 격리 (c)select 영향 범위 (d)스위치 + 선행 조건")


def check_gen_id() -> None:
    """생성 회차(genId) 분리 — 2026-09-23 "옛 영상이 다시 붙는" 결함의 재발 방지.

    docs/vpoc1/fix-regen-reload.md 참고. ③을 고쳐 다시 승인했을 때 1차와 같은
    clientRequestToken 이 나와 Bedrock 이 1차 invocation 을 돌려줬다.
    """
    sys.path.insert(0, str(ROOT / "app"))
    from src.vpoc import regen, store, video  # noqa: PLC0415

    bad: list[str] = []
    owner, pid, plan = "admin", "prj1", "pl_20260922_0042"
    shot, attempt = "shot_01", 1
    g1, g2 = "g20260922233000", "g20260923003500"

    # (a) genId 가 다르면 토큰·jobs 키·출력 접두사가 모두 다르다
    t_old = video.client_token(plan, shot, attempt)
    t1 = video.client_token(plan, shot, attempt, g1)
    t2 = video.client_token(plan, shot, attempt, g2)
    if not (t_old != t1 != t2 and t1 != t2):
        bad.append(f"(a) 토큰이 회차별로 갈라지지 않습니다: {t_old} / {t1} / {t2}")
    for token in (t_old, t1, t2):
        if not video.TOKEN_PATTERN.match(token):
            bad.append(f"(a) 토큰 {token!r} 이 AWS 패턴을 어깁니다")
    if video.client_token(plan, shot, attempt, g1) != t1:
        bad.append("(a) 같은 회차인데 토큰이 달라집니다 (멱등성 깨짐)")

    j1 = store.job_key(owner, pid, plan, shot, attempt, g1)
    j2 = store.job_key(owner, pid, plan, shot, attempt, g2)
    j_old = store.job_key(owner, pid, plan, shot, attempt)
    if len({j1, j2, j_old}) != 3:
        bad.append(f"(a) jobs 키가 겹칩니다: {j_old} / {j1} / {j2}")
    if f"/jobs/{g1}/" not in j1 or f"/jobs/{g2}/" not in j2:
        bad.append("(a) jobs 키에 genId 가 들어가지 않습니다")
    if "/jobs/" not in j_old or j_old.count("/jobs/") != 1 or g1 in j_old:
        bad.append(f"(a) 옛 jobs 키 규칙이 바뀌었습니다: {j_old}")

    p1 = store.video_output_prefix(owner, pid, plan, shot, attempt, g1)
    p2 = store.video_output_prefix(owner, pid, plan, shot, attempt, g2)
    p_old = store.video_output_prefix(owner, pid, plan, shot, attempt)
    if len({p1, p2, p_old}) != 3:
        bad.append(f"(a) 출력 접두사가 겹칩니다: {p_old} / {p1} / {p2}")
    if not p1.endswith(f"/shots/{g1}/{shot}-{attempt}"):
        bad.append(f"(a) 출력 접두사에 genId 가 없습니다: {p1}")
    if not p_old.endswith(f"/shots/{shot}-{attempt}"):
        bad.append(f"(a) 옛 출력 접두사 규칙이 바뀌었습니다: {p_old}")

    if not store.new_gen_id().startswith("g") or len(store.new_gen_id()) != 15:
        bad.append(f"(a) new_gen_id 형식이 다릅니다: {store.new_gen_id()}")

    # mp4 찾기가 그 invocation 폴더만 보는지 (상위 접두사 나열 금지)
    source = (ROOT / "app" / "src" / "vpoc" / "video.py").read_text(encoding="utf-8")
    body = source[source.index("def find_output_mp4(") :]
    body = body[: body.index("\ndef ", 1)] if "\ndef " in body[1:] else body
    if "invocation_arn" not in body or "invocation_id(" not in body:
        bad.append("(a) find_output_mp4 가 invocation 폴더로 한정하지 않습니다")

    # (b) ③ 재실행 뒤 ④가 stale 이고 이전 영상 참조가 남지 않는다.
    #     사용자가 실제로 한 것: ④에서 ②로 되돌아가 화풍을 바꿨다 → mark_stale(2) → ③·④ stale.
    state = _fake_state()
    state["steps"]["4"]["genId"] = g1
    dropped = store.mark_stale(state, 2)
    step4 = state["steps"]["4"]
    if step4.get("status") != "stale":
        bad.append(f"(b) ④ 가 stale 이 아닙니다: {step4.get('status')}")
    if step4.get("shots"):
        bad.append(f"(b) ④ 에 이전 샷이 남아 있습니다: {len(step4['shots'])}개")
    if step4.get("genId"):
        bad.append(f"(b) genId 가 비워지지 않았습니다: {step4.get('genId')}")
    if (step4.get("final") or {}).get("key") or (step4.get("final") or {}).get("status") != "none":
        bad.append(f"(b) 합본 참조가 남아 있습니다: {step4.get('final')}")
    if state.get("finalizedAt"):
        bad.append("(b) finalizedAt 이 남아 있습니다 — 완료 처리가 이전 회차에 묶입니다")
    if dropped.get("videos") != 2:
        bad.append(f"(b) 버려질 영상 수가 틀립니다: {dropped}")
    # ③은 stale 이 되지만 내용은 남는다(사용자가 다시 실행해야 한다)
    if state["steps"]["3"].get("status") != "stale" or not state["steps"]["3"].get("shots"):
        bad.append("(b) ③ 내용이 지워졌습니다 — status 만 stale 이어야 합니다")

    # ③ 저장 내용이 바뀐 경우(mark_stale(3))도 ④만 비워진다
    only4 = _fake_state()
    only4["steps"]["4"]["genId"] = g1
    store.mark_stale(only4, 3)
    if only4["steps"]["4"].get("shots") or only4["steps"]["4"].get("genId"):
        bad.append("(b) mark_stale(3) 이 ④를 비우지 않았습니다")
    if only4["steps"]["3"].get("status") != "ready":
        bad.append("(b) mark_stale(3) 이 ③ 자신을 건드렸습니다")
    if only4.get("finalizedAt"):
        bad.append("(b) mark_stale(3) 뒤 finalizedAt 이 남아 있습니다")

    # (c) 두 번째 완료가 새 finals 기록을 만들고 project.json·Neptune 을 갱신한다
    k1 = store.finals_key(owner, pid, plan, "2026-09-22T23:57:14Z")
    k2 = store.finals_key(owner, pid, plan, "2026-09-23T00:40:02Z")
    if k1 == k2 or not k1.startswith(store.finals_prefix(owner, pid, plan)):
        bad.append(f"(c) finals 키가 회차별로 갈라지지 않습니다: {k1} / {k2}")
    rsource = (ROOT / "app" / "src" / "vpoc" / "routes.py").read_text(encoding="utf-8")
    fin = rsource[rsource.index("def finish_plan(") : rsource.index("def update_graph(")]
    if "finals_key" not in fin:
        bad.append("(c) finish_plan 이 finals 기록을 쓰지 않습니다")
    if '"genId": step4.get("genId")' not in fin:
        bad.append("(c) 완료 기록에 genId 가 없습니다")
    # final.json 이 이미 있을 때 조기 반환하면 project.json·Neptune 갱신에 도달하지 못한다
    tail = fin[fin.index("store.final_key(owner, pid, plan_id), record, create_only=True") :]
    guard = tail[: tail.index("store.project_key")]
    if "return True" in guard:
        bad.append("(c) final.json 이 이미 있으면 조기 반환합니다 — 확정본 갱신에 도달하지 못합니다")
    if "update_graph(pid, \"done\", plan_id, finals_key" not in fin:
        bad.append("(c) Neptune vpocFinalKey 가 finals 기록을 가리키지 않습니다")

    # (d) genId 없는 옛 state 를 읽어도 기존 키와 영상이 그대로다
    old = _fake_state()
    old_shot = old["steps"]["4"]["shots"][0]
    old_plan_shot = old["steps"]["3"]["shots"][0]
    versions = regen.versions_view(old_shot, old_plan_shot, "")
    if len(versions) != 1 or versions[0]["attempt"] != 1:
        bad.append(f"(d) 옛 state 의 versions 가 1개여야 합니다: {versions}")
    if old_shot.get("outputKey") != "users/tester/a/s1.mp4":
        bad.append("(d) 옛 state 의 outputKey 가 바뀌었습니다")
    if "versions" in old["steps"]["4"]["shots"][0]:
        bad.append("(d) 읽기만 했는데 state 에 versions 가 쓰였습니다")

    # (e) 재생성 versions 에 다른 genId 의 job 이 섞이지 않는다
    mixed = _fake_state()
    mixed["steps"]["4"]["genId"] = g2
    shot_m = mixed["steps"]["4"]["shots"][0]
    shot_m["versions"] = [
        {"attempt": 1, "genId": g1, "origin": "plan", "status": "completed",
         "outputKey": "old.mp4", "summaryKo": "1차"},
        {"attempt": 2, "genId": g1, "origin": "regen", "status": "completed",
         "outputKey": "old2.mp4", "summaryKo": "1차 재생성"},
        {"attempt": 1, "genId": g2, "origin": "plan", "status": "completed",
         "outputKey": "new.mp4", "summaryKo": "2차"},
    ]
    view = regen.versions_view(shot_m, mixed["steps"]["3"]["shots"][0], g2)
    if len(view) != 1 or view[0].get("genId") != g2:
        bad.append(f"(e) 다른 회차 버전이 섞였습니다: {[v.get('genId') for v in view]}")
    kept = regen.seed_versions(shot_m, mixed["steps"]["3"]["shots"][0], g2)
    if len(kept) != 1 or kept[0].get("outputKey") != "new.mp4":
        bad.append(f"(e) seed_versions 가 이전 회차를 버리지 않습니다: {kept}")

    if bad:
        fail("생성 회차 검사 실패\n" + "\n".join(f"  {item}" for item in bad))
    else:
        ok("생성 회차 검사 (a)키 분리 (b)③재실행 (c)finals·확정본 (d)옛 데이터 (e)회차 격리")


def check_no_budget_cap() -> None:
    """비용 상한 없음 — 2026-09-23 사용자 결정. 기록은 남기고 제출을 막지 않는다.

    (a) 누계가 매우 커도 제출이 거부되지 않는다
    (b) 제출하면 원장에 정확히 한 번 기록된다 (같은 키는 중복 계상하지 않는다)
    """
    sys.path.insert(0, str(ROOT / "app"))
    from src.vpoc import ledger, settings  # noqa: PLC0415

    bad: list[str] = []

    # 상한 검사 코드가 남아 있으면 실패시킨다 — 되살아나면 제출이 다시 막힌다.
    if hasattr(ledger, "ensure_room"):
        bad.append("ledger.ensure_room 이 남아 있습니다")
    if hasattr(settings, "BUDGET_USD"):
        bad.append("settings.BUDGET_USD 가 남아 있습니다")
    for name in ("routes.py", "regen.py", "ledger.py"):
        text = (ROOT / "app" / "src" / "vpoc" / name).read_text(encoding="utf-8")
        for needle in ("ensure_room(", "BudgetExceeded", "BUDGET_USD"):
            # ledger.py 의 모듈 docstring 에는 "제거했다" 설명이 남아 있어도 된다.
            if needle in text and not (name == "ledger.py" and needle in text.split('"""')[1]):
                bad.append(f"{name} 에 {needle} 이 남아 있습니다")

    # (a) 누계가 100,000 USD 여도 어떤 제출 경로도 거부되지 않는다
    fake = {"capUsd": None, "spentUsd": 100000.0, "entries": [{"key": "old", "usd": 100000.0}]}
    real_read = ledger.read
    real_write = ledger.STORE.write_json
    charged: list[dict] = []

    def fake_read():
        return json.loads(json.dumps(fake)), "etag"

    def fake_write(key, data, **kwargs):
        fake["entries"] = data["entries"]
        fake["spentUsd"] = data["spentUsd"]
        charged.append(data["entries"][-1])
        return "etag2"

    try:
        ledger.read = fake_read
        ledger.STORE.write_json = fake_write
        summary = ledger.summary()
        if summary.get("capUsd") is not None:
            bad.append(f"(a) summary 의 capUsd 가 null 이 아닙니다: {summary}")
        if abs(float(summary.get("spentUsd") or 0) - 100000.0) > 0.01:
            bad.append(f"(a) 누계가 그대로 보이지 않습니다: {summary}")

        # (b) 제출 1건이 정확히 한 번 기록된다
        token = "pl-x-g20260923-shot-01-1"
        out = ledger.charge_video(token, user="u", project_id="p", plan_id="pl")
        if len(charged) != 1:
            bad.append(f"(b) 기록이 1건이 아닙니다: {len(charged)}")
        elif abs(float(charged[0]["usd"]) - settings.shot_usd()) > 0.001:
            bad.append(f"(b) 기록 금액이 틀립니다: {charged[0]['usd']}")
        elif charged[0]["key"] != token:
            bad.append(f"(b) 기록 키가 틀립니다: {charged[0]['key']}")
        if out.get("capUsd") is not None:
            bad.append("(b) charge 응답의 capUsd 가 null 이 아닙니다")

        # 같은 키를 다시 계상하지 않는다
        before = len(charged)
        ledger.charge_video(token, user="u", project_id="p", plan_id="pl")
        if len(charged) != before:
            bad.append("(b) 같은 키가 두 번 계상됐습니다")
    finally:
        ledger.read = real_read
        ledger.STORE.write_json = real_write

    # 화면이 상한을 그리지 않는다
    js = (ROOT / "frontend" / "public" / "vpoc.js").read_text(encoding="utf-8")
    # 값을 읽어 쓰는 곳이 없어야 한다. 주석과 픽스처의 `capUsd: null` 은 괜찮다.
    if re.search(r"\.capUsd\b", js):
        bad.append("vpoc.js 가 budget.capUsd 값을 읽어 씁니다 (상한을 표시하게 됩니다)")
    if "상한 " in js:
        bad.append("vpoc.js 에 '상한' 문구가 남아 있습니다")
    # 설계자 2026-10-07: 영상 만들기 화면에는 금액을 보이지 않는다(admin 모델 관리에서만 본다).
    # 예전에는 "누적 비용"·"현재 누적" 문구가 있어야 했다 — 그 요구를 반대로 바꿨다.
    for name in ("vpoc.js", "vpoc2.js"):
        text = (ROOT / "frontend" / "public" / name).read_text(encoding="utf-8")
        # 주석은 빼고 문자열 리터럴 안의 금액 표시만 본다.
        code = re.sub(r"/\*.*?\*/|//[^\n]*", "", text, flags=re.S)
        for needle in (" USD", "누적 비용", "현재 누적"):
            if re.search(r"[\"'][^\"'\n]*" + re.escape(needle), code):
                bad.append(f"{name} 가 영상 화면에 금액을 보입니다: {needle.strip()!r}")

    if bad:
        fail("비용 상한 없음 검사 실패\n" + "\n".join(f"  {item}" for item in bad))
    else:
        ok("비용 상한 없음 검사 (a)누계 100,000 에도 제출 가능 (b)원장 1건 정확히 기록")


def check_planview_keys() -> None:
    """PlanView ④ 샷의 기존 키가 그대로고 새 키만 더해졌는지 본다."""
    source = (ROOT / "app" / "src" / "vpoc" / "routes.py").read_text(encoding="utf-8")
    start = source.index("def _shot4_view(")
    body = source[start : source.index("def plan_view(", start)]
    missing = [key for key in LEGACY_SHOT4_KEYS if f'"{key}":' not in body]
    added = [key for key in REGEN_SHOT4_KEYS if f'"{key}":' not in body]
    gen = source[source.index("def mutator(inner: dict):", source.index('if action == "generate"')) :]
    gen = gen[: gen.index("store.mutate_state")]
    step4_missing = [key for key in STEP4_KEYS if f'"{key}":' not in gen]
    if missing:
        fail(f"PlanView ④ 샷에서 기존 키가 사라졌습니다: {missing}")
    elif added:
        fail(f"PlanView ④ 샷에 새 키가 없습니다: {added}")
    elif step4_missing:
        fail(f"generate 가 ④ 에 새 키를 세우지 않습니다: {step4_missing}")
    else:
        ok(
            f"PlanView ④ 샷 키 — 기존 {len(LEGACY_SHOT4_KEYS)}개 유지"
            f" + 새 {len(REGEN_SHOT4_KEYS)}개, ④ genId 확인"
        )


# ---------------------------------------------------------------- main


def main() -> int:
    print("== vpoc1 check ==")
    check_compileall()
    check_import()
    check_js()
    check_residue()
    check_compose()
    check_client_token()
    check_regen()
    check_gen_id()
    check_no_budget_cap()
    check_planview_keys()
    print()
    if warnings:
        print(f"경고 {len(warnings)}건")
    if failures:
        print(f"실패 {len(failures)}건")
        return 1
    print("전부 통과")
    return 0


if __name__ == "__main__":
    sys.exit(main())
