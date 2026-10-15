"""병합본이 Base 의 무엇도 잃지 않았는지 증명한다 (PRD A2 · A3).

## 왜 필요한가

이 저장소에는 테스트가 없다. 병합은 함수를 옮기고 파일을 합치는 작업이라
"화면이 뜬다"로는 무엇이 사라졌는지 알 수 없다. `git diff` 도 믿을 수 없다 —
대규모 재구성에서는 **이동과 모듈 추출이 삭제로 보인다**(실제로 병합 중
`server.py` 의 인증 가드 6개가 삭제처럼 보였으나 다른 모듈로 옮겨진 것이었다).

그래서 집합 포함 관계로 증명한다. 기준선(`baseline` 태그)의 집합이 현재 트리의
집합에 **포함**되면 삭제가 없다.

    파일 집합          baseline ⊆ HEAD
    Python 최상위 심볼  baseline ⊆ HEAD   (모듈 단위가 아니라 트리 전체 합집합)
    Flask 라우트        baseline ⊆ HEAD
    K8s 리소스(kind/name) baseline ⊆ HEAD

심볼을 트리 전체 합집합으로 보는 이유: 모듈 사이 이동은 정당한 리팩터링이고
막을 이유가 없다. 사라지는 것만 잡는다. 어느 모듈에서 어디로 갔는지도 함께 찍는다.

## 사용법

    python AI-POC/deploy/verify/verify_merge_nodelete.py
    python AI-POC/deploy/verify/verify_merge_nodelete.py --base baseline

종료 코드 0 통과 / 1 삭제 발견. Protected(GPU) 항목은 diff 가 0 이어야 하며
하나라도 다르면 실패로 다룬다.
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

ROUTE_RX = re.compile(r'@app\.(?:route|get|post|put|patch|delete)\(\s*"([^"]+)"')
K8S_RX = re.compile(r"^kind:\s*(\S+)", re.M)
K8S_NAME_RX = re.compile(r"^\s{2}name:\s*(\S+)", re.M)

# Protected: GPU 경로. Base 와 byte 단위로 같아야 한다 (PRD H3 · A3).
PROTECTED = [
    "app/src/wan_client.py",
    "app/src/video_prompt.py",
    "app/src/stitcher.py",
    "deploy/verify/verify-video-generation.ps1",
    "deploy/verify/verify-image-brief.ps1",
]

# 이동이 확인된 심볼만 예외로 둔다. 새로 추가할 때는 어디로 갔는지 반드시 적는다.
RELOCATED = {
    "COVER_IMAGE_MODEL": "app/src/model_registry.py (BUILTIN_IMAGE_MODEL_ID, 같은 env 를 읽는다)",
    "COVER_IMAGE_REGION": "app/src/model_registry.py (IMAGE_REGION, 같은 env 를 읽는다)",
}


def git(*args: str) -> str:
    r = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True)
    if r.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} 실패: {r.stderr.decode('utf-8', 'replace')}")
    return r.stdout.decode("utf-8", "replace")


def git_bytes(*args: str) -> bytes:
    r = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True)
    return r.stdout if r.returncode == 0 else b""


def top_level_symbols(source: str) -> set[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    out: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    out.add(target.id)
    return out


def collect_ref(base: str) -> tuple[set[str], dict[str, str], set[str], set[str]]:
    """기준선 커밋에서 집합을 모은다."""
    files = {p for p in git("ls-tree", "-r", "--name-only", base).split("\n") if p.strip()}
    symbols: dict[str, str] = {}
    routes: set[str] = set()
    for path in sorted(files):
        if not path.endswith(".py"):
            continue
        src = git_bytes("show", f"{base}:{path}").decode("utf-8", "replace")
        for sym in top_level_symbols(src):
            symbols.setdefault(sym, path)
        routes |= set(ROUTE_RX.findall(src))
    k8s = set()
    for path in sorted(p for p in files if p.startswith("k8s/")):
        k8s |= k8s_resources(git_bytes("show", f"{base}:{path}").decode("utf-8", "replace"))
    return files, symbols, routes, k8s


def k8s_resources(text: str) -> set[str]:
    out = set()
    for doc in text.split("\n---"):
        kinds = K8S_RX.findall(doc)
        names = K8S_NAME_RX.findall(doc)
        if kinds:
            out.add(f"{kinds[0]}/{names[0] if names else '?'}")
    return out


def collect_head() -> tuple[set[str], dict[str, str], set[str], set[str]]:
    files = {p for p in git("ls-files").split("\n") if p.strip()}
    symbols: dict[str, str] = {}
    routes: set[str] = set()
    for path in sorted(files):
        if not path.endswith(".py"):
            continue
        src = (REPO / path).read_text(encoding="utf-8", errors="replace")
        for sym in top_level_symbols(src):
            symbols.setdefault(sym, path)
        routes |= set(ROUTE_RX.findall(src))
    k8s = set()
    for path in sorted(p for p in files if p.startswith("k8s/")):
        k8s |= k8s_resources((REPO / path).read_text(encoding="utf-8", errors="replace"))
    return files, symbols, routes, k8s


def report(label: str, ref: set[str], now: set[str], allow: dict[str, str] | None = None) -> bool:
    missing = ref - now
    allow = allow or {}
    unexplained = {m for m in missing if m not in allow}
    mark = "PASS" if not unexplained else "FAIL"
    print(f"[{mark}] {label:<26} 기준선 {len(ref):>4} -> 현재 {len(now):>4}   신규 {len(now - ref):>4}")
    for m in sorted(missing):
        if m in allow:
            print(f"         이동 확인  {m}  ->  {allow[m]}")
    for m in sorted(unexplained):
        print(f"         **사라짐**  {m}")
    return not unexplained


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="baseline", help="기준선 (기본 baseline 태그)")
    args = ap.parse_args()

    print(f"저장소   : {REPO}")
    print(f"기준선   : {args.base}")
    print()

    ref_files, ref_syms, ref_routes, ref_k8s = collect_ref(args.base)
    files, syms, routes, k8s = collect_head()

    ok = True
    # 기준선 시점에 없던 메타 파일은 비교에서 제외한다
    ok &= report("파일 집합", ref_files, files)
    ok &= report("Python 최상위 심볼", set(ref_syms), set(syms), RELOCATED)
    ok &= report("Flask 라우트", ref_routes, routes)
    ok &= report("K8s 리소스", ref_k8s, k8s)

    print()
    print("Protected (GPU) — Base 와 diff 0 이어야 한다")
    for path in PROTECTED:
        diff = git("diff", "--stat", args.base, "--", path).strip()
        state = "diff 0" if not diff else "**변경됨**"
        print(f"  [{'PASS' if not diff else 'FAIL'}] {path:<46}{state}")
        if diff:
            ok = False

    print()
    print("=" * 70)
    print("결과: " + ("PASS — Base 에서 사라진 것 없음" if ok else "FAIL — 위 항목 확인 필요"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
