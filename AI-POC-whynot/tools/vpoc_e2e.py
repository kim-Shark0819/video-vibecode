"""vpoc1 E2E — F1 예시를 공개 API 로만 처음부터 끝까지 돌린다 (Gate 2).

정적 검사가 아니다. **실제로 유료 호출을 한다** (약 15.2 USD: Claude 2회 + SD3.5 2장 + Luma 4건).

두 가지 방법으로 돈다.

  1. dev API 로그인 — 환경변수 `VPOC_E2E_USER` · `VPOC_E2E_PASS` 가 있으면 이 방법을 쓴다.
     `VPOC_E2E_BASE` 로 주소를 바꿀 수 있다(기본 https://dev-whynot.meta-clouds.com).
  2. 앱 파드 안 — 위 환경변수가 없으면 앱 모듈을 직접 import 해서 test client 로 돈다.
     파드 안에서만 된다(IRSA·Neptune 접근 필요).

     kubectl -n whynot exec -i <앱 파드> -- python - < AI-POC-whynot/tools/vpoc_e2e.py

결과 표는 표준 출력으로 나온다. `docs/vpoc1/run-log.md` 의 샷별 표에 그대로 옮긴다.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.getenv("VPOC_E2E_BASE", "https://dev-whynot.meta-clouds.com").rstrip("/")
USER = os.getenv("VPOC_E2E_USER", "")
PASS = os.getenv("VPOC_E2E_PASS", "")
TIMEOUT_S = int(os.getenv("VPOC_E2E_TIMEOUT_S", "2400"))

F1_TEXT = (
    "두 남자는 F1 레이서이고, 둘은 원수지간이다. 하지만, 둘의 갈등 사이에서 연애 감정이 "
    "피어나게 된다. 캐릭터 중 하얀머리의 서양인 얼굴, 검은색과 노란색이 섞인 레이스 유니폼을 "
    "입고 있는 캐릭터의 이름은 Roman Sinclair. 검은 머리의 동양인 얼굴, 흰색과 파란색이 섞인 "
    "레이스 유니폼을 입고 있는 캐릭터의 이름은 Seo Won. 이 둘은 갈등 끝에 서로의 마음을 확인한다."
)


# ---------------------------------------------------------------- 전송 계층


class HttpClient:
    """dev API 로 로그인해서 쓴다."""

    def __init__(self) -> None:
        self.token = ""

    def login(self) -> None:
        payload = self.call("POST", "/api/auth/login", {"username": USER, "password": PASS})
        self.token = payload.get("token") or payload.get("accessToken") or ""
        if not self.token:
            raise SystemExit(f"로그인 응답에 토큰이 없습니다: {list(payload)}")

    def call(self, method: str, path: str, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(BASE + path, data=data, method=method)
        if data:
            request.add_header("Content-Type", "application/json")
        if self.token:
            request.add_header("Authorization", "Bearer " + self.token)
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                return json.loads(raw)
            except ValueError:
                return {"ok": False, "error": {"type": "HTTPError", "message": raw[:2000],
                                               "httpStatus": exc.code}}

    def check_url(self, url: str) -> tuple[int, str]:
        request = urllib.request.Request(url, method="GET")
        request.add_header("Range", "bytes=0-0")
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.status, response.headers.get("Content-Type", "")
        except Exception as exc:  # noqa: BLE001
            return 0, f"{type(exc).__name__}: {exc}"


class PodClient:
    """앱 파드 안에서 Flask test client 로 쓴다. 인증은 관리자 토큰을 직접 만든다."""

    def __init__(self) -> None:
        sys.path.insert(0, "/app")
        from src import server  # noqa: PLC0415

        self.server = server
        self.client = server.app.test_client()
        self.token = server.issue_token(
            {"username": server.ADMIN_USERNAME, "role": server.ROLE_ADMIN,
             "displayName": server.ADMIN_USERNAME}
        ) if hasattr(server, "issue_token") else ""

    def login(self) -> None:
        if self.token:
            return
        response = self.client.post(
            "/api/auth/login",
            json={"username": self.server.ADMIN_USERNAME, "password": self.server.ADMIN_PASSWORD},
        )
        payload = response.get_json() or {}
        self.token = payload.get("token") or ""
        if not self.token:
            raise SystemExit(f"파드 내 로그인 실패: {response.status_code} {payload}")

    def call(self, method: str, path: str, body=None):
        headers = {"Authorization": "Bearer " + self.token}
        response = self.client.open(path, method=method, json=body, headers=headers)
        return response.get_json() or {"ok": False, "error": {"type": "NonJson",
                                                              "message": response.data[:500].decode("utf-8", "replace"),
                                                              "httpStatus": response.status_code}}

    def check_url(self, url: str) -> tuple[int, str]:
        request = urllib.request.Request(url, method="GET")
        request.add_header("Range", "bytes=0-0")
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.status, response.headers.get("Content-Type", "")
        except Exception as exc:  # noqa: BLE001
            return 0, f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------- 흐름


def need(payload: dict, where: str) -> dict:
    if not payload or payload.get("ok") is not True:
        print(json.dumps(payload, ensure_ascii=False, indent=2)[:3000])
        raise SystemExit(f"{where} 실패")
    return payload


def wait_task(client, pid: str, plan_id: str, label: str, limit_s: int = 600) -> dict:
    started = time.time()
    while True:
        view = need(client.call("GET", f"/api/vpoc/p/{pid}/plans/{plan_id}"), f"{label} 조회")
        task = view.get("task") or {}
        if task.get("status") in (None, "idle", "done"):
            print(f"  {label} 완료 · {int(time.time() - started)}초")
            return view
        if task.get("status") == "error":
            print(json.dumps(task.get("error"), ensure_ascii=False, indent=2))
            raise SystemExit(f"{label} 오류")
        if time.time() - started > limit_s:
            raise SystemExit(f"{label} 이 {limit_s}초를 넘겼습니다")
        time.sleep(3)


def main() -> int:
    client = HttpClient() if (USER and PASS) else PodClient()
    client.login()
    print(f"전송 계층: {type(client).__name__}")

    project = need(
        client.call("POST", "/api/projects", {"title": "vpoc1 E2E F1", "visibility": "private"}),
        "프로젝트 생성",
    )
    pid = (project.get("item") or {}).get("id") or project.get("id")
    if not pid:
        raise SystemExit(f"프로젝트 id 를 찾을 수 없습니다: {list(project)}")
    print(f"프로젝트 {pid}")
    client.call("PUT", f"/api/projects/{pid}/track", {"track": "video"})

    plan_id = need(client.call("POST", f"/api/vpoc/p/{pid}/plans"), "계획 생성")["planId"]
    print(f"계획 {plan_id}")

    need(
        client.call(
            "PUT", f"/api/vpoc/p/{pid}/plans/{plan_id}/steps/1",
            {"text": F1_TEXT, "characters": [], "sceneFiles": []},
        ),
        "① 저장",
    )
    need(client.call("POST", f"/api/vpoc/p/{pid}/plans/{plan_id}/run/analyze"), "analyze 시작")
    view = wait_task(client, pid, plan_id, "analyze")

    step2 = view["steps"]["2"]
    print(f"  캐릭터 {len(step2.get('characters') or [])}명 · 질문 {len(step2.get('questions') or [])}개")
    need(
        client.call("PUT", f"/api/vpoc/p/{pid}/plans/{plan_id}/steps/2",
                    {"story": {}, "characters": [], "answers": {}, "extraKo": ""}),
        "② 저장",
    )
    need(client.call("POST", f"/api/vpoc/p/{pid}/plans/{plan_id}/run/prepare"), "prepare 시작")
    view = wait_task(client, pid, plan_id, "prepare", limit_s=900)

    step3 = view["steps"]["3"]
    shots = step3.get("shots") or []
    review = [shot["id"] for shot in shots if shot.get("reviewKo")]
    print(f"  샷 {len(shots)}개 · 검토 필요 {len(review)}개 · 비용 {step3.get('cost')}")
    for shot in shots:
        print(f"    {shot['id']} promptChars={shot.get('promptChars')}")
    if review:
        raise SystemExit(f"검토 필요 샷이 있어 승인할 수 없습니다: {review}")

    view = need(
        client.call("PUT", f"/api/vpoc/p/{pid}/plans/{plan_id}/steps/3",
                    {"keepShotIds": [shot["id"] for shot in shots], "shots": [],
                     "useKeyframes": False}),
        "③ 저장",
    )
    total = (view["steps"]["3"].get("cost") or {}).get("totalUsd")
    print(f"  승인 금액 {total} USD")
    need(
        client.call("POST", f"/api/vpoc/p/{pid}/plans/{plan_id}/run/generate",
                    {"confirmUsd": total}),
        "generate",
    )

    started = time.time()
    while True:
        view = need(client.call("GET", f"/api/vpoc/p/{pid}/plans/{plan_id}"), "④ 조회")
        step4 = view["steps"]["4"]
        counts: dict[str, int] = {}
        for shot in step4.get("shots") or []:
            counts[shot["status"]] = counts.get(shot["status"], 0) + 1
        print(f"  {int(time.time() - started)}초 · {counts}")
        if step4.get("status") in ("done", "partial"):
            break
        if time.time() - started > TIMEOUT_S:
            raise SystemExit(f"영상 생성이 {TIMEOUT_S}초를 넘겼습니다")
        time.sleep(15)

    print("\n| 샷 | 상태 | 모드 | 소요 | 재생 확인 |")
    print("|---|---|---|---|---|")
    failed = 0
    for shot in step4.get("shots") or []:
        if shot.get("videoUrl"):
            status, content_type = client.check_url(shot["videoUrl"])
        else:
            status, content_type = 0, "url 없음"
        if shot["status"] != "completed" or status not in (200, 206):
            failed += 1
        print(
            f"| {shot['id']} | {shot['status']} | {shot.get('mode')} | "
            f"{shot.get('elapsedS')}초 | HTTP {status} {content_type} |"
        )
        if shot.get("error"):
            print(f"  오류: {json.dumps(shot['error'], ensure_ascii=False)}")

    view = need(client.call("GET", f"/api/vpoc/p/{pid}"), "프로젝트 보기")
    print(
        f"\n프로젝트 보기: 영상 {len(view.get('videos') or [])}건 · "
        f"캐릭터 {(view.get('characters') or {}).get('status')} "
        f"{len(((view.get('characters') or {}).get('items')) or [])}명 · "
        f"입력 {len(view.get('inputs') or [])}건"
    )
    print(f"원장: {view.get('budget') or (need(client.call('GET', f'/api/vpoc/p/{pid}/plans/{plan_id}'), 'budget')).get('budget')}")

    if failed:
        print(f"\nGate 2 미통과 — 실패 샷 {failed}개")
        return 1
    print("\nGate 2 통과")
    return 0


if __name__ == "__main__":
    sys.exit(main())
