"""작업(run) 상태 기계. 화면의 단일 원본은 run.json 이다.

단계: 1 명령 → 2 해석 → 3 캐릭터 → 4 영상 → 5 리뷰
상태: empty · ready · stale(앞 단계가 바뀌어 다시 만들어야 함)
긴 일(해석 · 그리기 · 준비 · 합본)은 작업 스레드. 영상 제출 · 조회는 화면 조회(GET) 때 한다 (vpoc1 방식).
"""
from __future__ import annotations

import copy
import datetime as _dt
import secrets
import threading
import time
from typing import Any, Callable

from . import compose, ledger, settings, store
from .errors import Vpoc2Error, from_client_error, now_iso, public


def providers():
    """(llm, images, video) — 목 모드면 mock 모듈 하나가 셋을 다 한다."""
    if settings.MOCK:
        from . import mock

        return mock, mock, mock
    from . import images, llm, video

    return llm, images, video


def _take_tokens(llm_mod) -> tuple[int, int] | None:
    """llm 이 모아 둔 (입력, 출력) 토큰을 꺼낸다. 목 모드에는 없다(None)."""
    take = getattr(llm_mod, "take_tokens", None)
    return take() if take else None


# --- 저장 --------------------------------------------------------------------

def run_key(owner: str, run_id: str) -> str:
    return f"{store.run_prefix(owner, run_id)}/run.json"


def new_run_id() -> str:
    return "r" + _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d%H%M%S") + secrets.token_hex(2)


def _empty_run(owner: str, run_id: str) -> dict[str, Any]:
    return {
        "id": run_id, "owner": owner, "createdAt": now_iso(), "updatedAt": now_iso(),
        "step": 1, "maxStep": 1,
        "task": {"name": None, "status": "idle", "token": None, "startedAt": None, "finishedAt": None, "error": None},
        "input": {"command": "", "characters": [], "savedAt": None},
        "interpretation": {"status": "empty", "data": None, "violations": [], "costUsd": 0.0},
        "characters": {"status": "empty", "items": []},
        "video": {"status": "empty", "resolution": settings.DEFAULT_RESOLUTION, "prompts": [], "shots": [],
                  "final": {"status": "none", "key": None, "error": None}, "costUsd": 0.0},
        "review": None,
    }


def load(owner: str, run_id: str) -> tuple[dict[str, Any], str]:
    got = store.get_json(store.get_store(), run_key(owner, run_id))
    if got is None:
        raise Vpoc2Error("NotFound", f"작업 {run_id} 이 없다", http=404)
    return got


def update(owner: str, run_id: str, mutate: Callable[[dict[str, Any]], Any]) -> Any:
    """조건부 쓰기로 run.json 을 고친다. mutate 가 예외를 던지면 쓰지 않는다."""
    s = store.get_store()
    key = run_key(owner, run_id)
    for _ in range(5):
        run, etag = load(owner, run_id)
        result = mutate(run)
        run["updatedAt"] = now_iso()
        try:
            store.put_json(s, key, run, if_match=etag)
            return result
        except store.PreconditionFailed:
            continue
    raise Vpoc2Error("WriteConflict", "동시에 고치는 요청이 많다. 잠시 뒤 다시 시도한다", http=409)


def _index_upsert(owner: str, run: dict[str, Any]) -> None:
    data = (run["interpretation"].get("data") or {})
    entry = {"id": run["id"], "titleKo": data.get("titleKo") or run["input"]["command"][:30],
             "createdAt": run["createdAt"], "updatedAt": now_iso(), "step": run["step"],
             "videoStatus": run["video"]["status"],
             "score": (run.get("review") or {}).get("score")}

    def mutate(idx: dict[str, Any]) -> None:
        idx["runs"] = [r for r in idx["runs"] if r["id"] != run["id"]] + [entry]
        idx["runs"].sort(key=lambda r: r["createdAt"], reverse=True)

    store.update_json(store.get_store(), store.index_key(owner), {"runs": []}, mutate)


def list_runs(owner: str) -> list[dict[str, Any]]:
    return store.get_or_create_json(store.get_store(), store.index_key(owner), {"runs": []})[0]["runs"]


def create(owner: str, command: str = "") -> dict[str, Any]:
    run_id = new_run_id()
    run = _empty_run(owner, run_id)
    run["input"]["command"] = command
    store.put_json(store.get_store(), run_key(owner, run_id), run, if_none_match=True)
    _index_upsert(owner, run)
    return run


# --- 작업 스레드 --------------------------------------------------------------

def _task_running(run: dict[str, Any]) -> bool:
    t = run["task"]
    if t["status"] != "running":
        return False
    started = _dt.datetime.strptime(t["startedAt"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.timezone.utc)
    return (_dt.datetime.now(_dt.timezone.utc) - started).total_seconds() < settings.TASK_STALE_SECONDS


def _cancel_task(run: dict[str, Any]) -> None:
    if run["task"]["status"] == "running":
        run["task"].update({"status": "idle", "token": None, "finishedAt": now_iso(),
                            "error": None, "name": run["task"]["name"]})


def start_task(owner: str, run_id: str, name: str, precheck: Callable[[dict[str, Any]], None],
               work: Callable[[dict[str, Any]], Callable[[dict[str, Any]], None]]) -> None:
    """precheck(run) 로 선행 조건을 보고 running 으로 표시한 뒤 스레드에서 work(run 사본) 을 돌린다.
    work 는 결과를 run 에 적용하는 함수를 돌려준다. 그 사이 사용자가 되돌리거나 고치면(token 이 바뀌면) 결과를 버린다."""
    token = secrets.token_hex(8)

    def mark(run: dict[str, Any]) -> dict[str, Any]:
        if _task_running(run):
            raise Vpoc2Error("TaskRunning", f"'{run['task']['name']}' 작업이 진행 중이다", http=409)
        precheck(run)
        run["task"] = {"name": name, "status": "running", "token": token, "startedAt": now_iso(),
                       "finishedAt": None, "error": None}
        return copy.deepcopy(run)

    snapshot = update(owner, run_id, mark)

    def body() -> None:
        try:
            apply = work(snapshot)
            error = None
        except Vpoc2Error as exc:
            apply, error = None, exc.envelope()
        except Exception as exc:  # noqa: BLE001 - 원문을 남긴다
            apply, error = None, from_client_error(exc, f"task.{name}").envelope()

        def finish(run: dict[str, Any]) -> None:
            if run["task"].get("token") != token:
                return  # 취소됨 (되돌리기 · 수정)
            if apply is not None:
                apply(run)
            run["task"].update({"status": "error" if error else "done", "token": None,
                                "finishedAt": now_iso(), "error": error})

        try:
            update(owner, run_id, finish)
            _index_upsert(owner, load(owner, run_id)[0])
        except Exception:  # noqa: BLE001
            import logging

            logging.getLogger("vpoc2").exception("vpoc2.task finish failed name=%s run=%s", name, run_id)

    threading.Thread(target=body, name=f"vpoc2-{name}-{run_id}", daemon=True).start()


# --- 무효화 -------------------------------------------------------------------

def _stale_from(run: dict[str, Any], step: int) -> None:
    """step 이후 단계를 stale 로 표시한다 (데이터는 남긴다)."""
    if step <= 2 and run["interpretation"]["status"] == "ready":
        run["interpretation"]["status"] = "stale"
    if step <= 3 and run["characters"]["status"] != "empty":
        run["characters"]["status"] = "stale"
        for it in run["characters"]["items"]:
            it["confirmed"] = False
    if step <= 4 and run["video"]["status"] != "empty":
        run["video"]["status"] = "stale"
        run["video"]["final"]["status"] = "stale" if run["video"]["final"]["key"] else "none"
    run["maxStep"] = min(run["maxStep"], step)
    run["step"] = min(run["step"], step)


# --- 1 명령 -------------------------------------------------------------------

def save_input(owner: str, run_id: str, command: str, characters: list[dict[str, Any]]) -> None:
    command = (command or "").strip()
    if len(command) > 2000:
        raise Vpoc2Error("CommandTooLong", f"명령은 2,000자까지 ({len(command)}자)", http=400)
    chars = [{"name": (c.get("name") or "").strip()[:40], "description": (c.get("description") or "").strip()[:300]}
             for c in (characters or [])[:2]]

    def mutate(run: dict[str, Any]) -> None:
        changed = run["input"]["command"] != command or run["input"]["characters"] != chars
        run["input"].update({"command": command, "characters": chars, "savedAt": now_iso()})
        if changed:
            _cancel_task(run)
            _stale_from(run, 2)
        run["step"] = 1

    update(owner, run_id, mutate)


# --- 2 해석 -------------------------------------------------------------------

def run_interpret(owner: str, run_id: str) -> None:
    llm, _images, _video = providers()

    def precheck(run: dict[str, Any]) -> None:
        if not run["input"]["command"]:
            raise Vpoc2Error("PreconditionFailed", "명령을 먼저 입력한다", http=409)

    def work(snap: dict[str, Any]):
        _take_tokens(llm)
        data, usd, violations = llm.interpret(snap["input"]["command"], snap["input"]["characters"])
        tokens = _take_tokens(llm)
        if usd:
            ledger.charge(f"interpret-{run_id}-{secrets.token_hex(3)}", user=owner, run_id=run_id, kind="text",
                          model=settings.TEXT_MODEL, units=1, usd=usd, tokens=tokens)

        def apply(run: dict[str, Any]) -> None:
            # 새 해석이면 뒤 단계는 새로 시작한다 (이전 리뷰는 reviews.json 에 남아 있다)
            run["video"] = _empty_run(owner, run_id)["video"]
            run["review"] = None
            run["interpretation"] = {"status": "ready", "data": data, "violations": violations,
                                     "costUsd": round(usd, 4)}
            run["characters"] = {"status": "empty", "items": [
                {"id": c["id"], "image": None, "history": [], "feedbackKo": "", "confirmed": False,
                 "appearanceShortEn": None} for c in data.get("characters", [])]}
            run["step"] = 2
            run["maxStep"] = 2

        return apply

    start_task(owner, run_id, "interpret", precheck, work)


def save_interpretation(owner: str, run_id: str, data: dict[str, Any]) -> None:
    """사용자가 ②에서 고친 해석. 검사 결과를 다시 계산한다."""
    def mutate(run: dict[str, Any]) -> None:
        if run["interpretation"]["status"] == "empty":
            raise Vpoc2Error("PreconditionFailed", "해석이 아직 없다", http=409)
        old = run["interpretation"]["data"] or {}
        def looks(chars: list[dict[str, Any]]) -> list[tuple]:
            return [(c.get("id"), c.get("kind"), c.get("speciesEn"), c.get("mustKeepEn"), c.get("appearanceEn"),
                     c.get("handleEn")) for c in chars]

        chars_changed = looks(old.get("characters", [])) != looks(data.get("characters", []))
        _cancel_task(run)
        run["interpretation"]["data"] = data
        run["interpretation"]["violations"] = compose.check_interpretation(data)
        run["interpretation"]["status"] = "ready"
        if chars_changed:
            _stale_from(run, 3)
        else:
            _stale_from(run, 4)
        run["step"] = 2

    update(owner, run_id, mutate)


# --- 3 캐릭터 -----------------------------------------------------------------

def run_draw(owner: str, run_id: str, char_id: str | None, feedback: str = "") -> None:
    """char_id 가 없으면 아직 이미지가 없는 캐릭터 전부를 그린다."""
    llm, images, _video = providers()
    feedback = (feedback or "").strip()[:300]

    def precheck(run: dict[str, Any]) -> None:
        interp = run["interpretation"]
        if interp["status"] != "ready":
            raise Vpoc2Error("PreconditionFailed", "해석을 먼저 확정한다", http=409)
        if interp["violations"]:
            raise Vpoc2Error("PreconditionFailed", "해석에 '검토 필요' 항목이 있다: " + "; ".join(interp["violations"][:5]),
                             http=409)
        if char_id and char_id not in {c["id"] for c in run["characters"]["items"]}:
            raise Vpoc2Error("NotFound", f"캐릭터 {char_id} 가 없다", http=404)

    def work(snap: dict[str, Any]):
        data = snap["interpretation"]["data"]
        by_id = {c["id"]: c for c in data.get("characters", [])}
        targets = [it for it in snap["characters"]["items"]
                   if (it["id"] == char_id) or (char_id is None and (not it.get("image") or snap["characters"]["status"] == "stale"))]
        results = {}
        revised: dict[str, dict[str, Any]] = {}
        for it in targets:
            c = by_id[it["id"]]
            if feedback and it["id"] == char_id:
                # 한국어 의견을 SD3.5 프롬프트에 그대로 넣지 않는다 (모델이 한국어를 거의 못 읽는다).
                # Claude 가 캐릭터의 영어 외형을 의견대로 고친다 → 영상 프롬프트에도 같이 반영된다.
                _take_tokens(llm)
                c, usd_text = llm.revise_character(c, feedback)
                tokens = _take_tokens(llm)
                revised[c["id"]] = c
                if usd_text:
                    ledger.charge(f"revise-{run_id}-{it['id']}-{secrets.token_hex(3)}", user=owner, run_id=run_id,
                                  kind="text", model=settings.TEXT_MODEL, units=1, usd=usd_text, tokens=tokens)
            prompt, negative = compose.character_image_prompt(data.get("style", "live_action"), c,
                                                             data.get("settingEn", ""))
            jpeg, seed, calls = images.generate(prompt, negative)
            n = len(it.get("history") or []) + 1
            key = f"{store.run_prefix(owner, run_id)}/characters/{it['id']}-{n}.jpg"
            store.get_store().put_bytes(store.MAIN, key, jpeg, "image/jpeg")
            usd = settings.USD_PER_IMAGE * calls if not settings.MOCK else 0.0
            if usd:
                ledger.charge(f"img-{run_id}-{it['id']}-{n}", user=owner, run_id=run_id, kind="image",
                              model=settings.IMAGE_MODEL, units=calls, usd=usd)
            results[it["id"]] = {"key": key, "seed": seed, "prompt": prompt, "at": now_iso(),
                                 "feedbackKo": feedback if it["id"] == char_id else ""}

        def apply(run: dict[str, Any]) -> None:
            chars = (run["interpretation"]["data"] or {}).get("characters", [])
            for i, c in enumerate(chars):
                if c.get("id") in revised:
                    chars[i] = revised[c["id"]]
            for it in run["characters"]["items"]:
                if it["id"] in results:
                    it["image"] = results[it["id"]]
                    it.setdefault("history", []).append(results[it["id"]])
                    it["confirmed"] = False
                    it["appearanceShortEn"] = None
                    if it["id"] == char_id:
                        it["feedbackKo"] = feedback
            run["characters"]["status"] = "ready"
            _stale_from(run, 4)
            run["step"] = 3
            run["maxStep"] = max(run["maxStep"], 3)

        return apply

    start_task(owner, run_id, "draw", precheck, work)


def confirm_character(owner: str, run_id: str, char_id: str, confirmed: bool, version: int | None = None) -> None:
    """확정 / 확정 취소. version 을 주면 그 이력 이미지를 현재 이미지로 고른 뒤 확정한다."""
    def mutate(run: dict[str, Any]) -> None:
        items = {it["id"]: it for it in run["characters"]["items"]}
        it = items.get(char_id)
        if it is None:
            raise Vpoc2Error("NotFound", f"캐릭터 {char_id} 가 없다", http=404)
        if version is not None:
            hist = it.get("history") or []
            if not 1 <= version <= len(hist):
                raise Vpoc2Error("NotFound", f"{char_id} 의 이미지 버전 {version} 이 없다", http=404)
            it["image"] = hist[version - 1]
        if confirmed and not it.get("image"):
            raise Vpoc2Error("PreconditionFailed", "이미지를 먼저 그린다", http=409)
        if it["confirmed"] != confirmed or version is not None:
            it["confirmed"] = confirmed
            it["appearanceShortEn"] = None
            _stale_from(run, 4)
            run["step"] = 3

    update(owner, run_id, mutate)


# --- 4 영상 -------------------------------------------------------------------

def run_prepare(owner: str, run_id: str) -> None:
    """확정 이미지에서 영상용 외형 문장을 만들고 샷 프롬프트 3개를 조립한다 (영상 제출 전)."""
    llm, _images, _video = providers()

    def precheck(run: dict[str, Any]) -> None:
        items = run["characters"]["items"]
        if run["interpretation"]["status"] != "ready":
            raise Vpoc2Error("PreconditionFailed", "해석을 먼저 확정한다", http=409)
        if not all(it["confirmed"] for it in items):
            raise Vpoc2Error("PreconditionFailed", "모든 캐릭터를 확정해야 영상을 만든다", http=409)

    def work(snap: dict[str, Any]):
        data = snap["interpretation"]["data"]
        chars = {c["id"]: c for c in data.get("characters", [])}
        appearances: dict[str, str] = {}
        issues: dict[str, list[str]] = {}
        total = 0.0
        s = store.get_store()
        _take_tokens(llm)
        for it in snap["characters"]["items"]:
            if it.get("appearanceShortEn"):
                appearances[it["id"]] = it["appearanceShortEn"]
                continue
            got = s.get_bytes(store.MAIN, it["image"]["key"])
            if got is None:
                raise Vpoc2Error("ImageMissing", it["image"]["key"], where="prepare", http=500)
            line, usd, violations = llm.appearance_from_image(got[0], chars[it["id"]])
            total += usd
            appearances[it["id"]] = line
            if violations:
                issues[it["id"]] = violations
        tokens = _take_tokens(llm)
        if total:
            ledger.charge(f"appearance-{run_id}-{secrets.token_hex(3)}", user=owner, run_id=run_id, kind="text",
                          model=settings.TEXT_MODEL, units=len(chars), usd=total, tokens=tokens)
        char_list = list(chars.values())
        prompts = []
        for shot in data["shots"]:
            text = compose.assemble(data, shot, appearances)
            v = compose.check_prompt(text, char_list)
            for cid in shot.get("characters") or []:
                v += [f"{cid}: {x}" for x in issues.get(cid, [])]
            prompts.append({"shotId": shot["id"], "promptEn": text, "promptChars": len(text), "reviewKo": v})

        def apply(run: dict[str, Any]) -> None:
            for it in run["characters"]["items"]:
                it["appearanceShortEn"] = appearances.get(it["id"])
            run["video"].update({"status": "prepared", "prompts": prompts, "shots": [],
                                 "final": {"status": "none", "key": None, "error": None}})
            run["step"] = 4
            run["maxStep"] = max(run["maxStep"], 4)

        return apply

    start_task(owner, run_id, "prepare", precheck, work)


def edit_prompt(owner: str, run_id: str, shot_id: str, prompt: str) -> None:
    """고급: 제출 전 영어 프롬프트 직접 수정. 같은 검사를 다시 한다."""
    def mutate(run: dict[str, Any]) -> None:
        if run["video"]["status"] != "prepared":
            raise Vpoc2Error("PreconditionFailed", "영상 시작 전에만 고칠 수 있다", http=409)
        chars = (run["interpretation"]["data"] or {}).get("characters", [])
        for p in run["video"]["prompts"]:
            if p["shotId"] == shot_id:
                p.update({"promptEn": prompt.strip(), "promptChars": len(prompt.strip()),
                          "reviewKo": compose.check_prompt(prompt, chars), "edited": True})
                return
        raise Vpoc2Error("NotFound", f"샷 {shot_id} 이 없다", http=404)

    update(owner, run_id, mutate)


def generate(owner: str, run_id: str, resolution: str, confirm_usd: float) -> dict[str, Any]:
    if resolution not in (settings.DEFAULT_RESOLUTION, settings.HQ_RESOLUTION):
        raise Vpoc2Error("BadResolution", f"{resolution} — 540p 또는 720p", http=400)
    expected = settings.video_usd(resolution)
    if round(float(confirm_usd), 2) != expected:
        raise Vpoc2Error("CostMismatch", f"확인 금액 {confirm_usd} ≠ 서버 계산 {expected}", http=409)

    def precheck(run: dict[str, Any]) -> None:
        if _task_running(run):
            raise Vpoc2Error("TaskRunning", "다른 작업이 진행 중이다", http=409)
        if run["video"]["status"] != "prepared":
            raise Vpoc2Error("PreconditionFailed", "영상 준비(프롬프트 조립)를 먼저 한다", http=409)
        bad = [p["shotId"] for p in run["video"]["prompts"] if p["reviewKo"]]
        if bad:
            raise Vpoc2Error("PreconditionFailed", f"'검토 필요' 샷이 있다: {', '.join(bad)}", http=409)

    run, _ = load(owner, run_id)
    precheck(run)
    if resolution == settings.HQ_RESOLUTION:
        ledger.reserve_hq(owner, run_id)

    def mutate(run: dict[str, Any]) -> None:
        precheck(run)
        run["video"]["resolution"] = resolution
        run["video"]["status"] = "running"
        run["video"]["startedAt"] = now_iso()
        run["video"]["shots"] = [
            {"id": p["shotId"], "promptEn": p["promptEn"], "status": "queued", "attempt": 1, "arn": None,
             "submittedAt": None, "completedAt": None, "lastPolledAt": 0, "outputKey": None, "error": None,
             "note": None} for p in run["video"]["prompts"]]
        run["video"]["final"] = {"status": "none", "key": None, "error": None}

    update(owner, run_id, mutate)
    return {"queued": settings.SHOTS, "usd": expected}


def retry_shot(owner: str, run_id: str, shot_id: str) -> None:
    def mutate(run: dict[str, Any]) -> None:
        for sh in run["video"]["shots"]:
            if sh["id"] == shot_id:
                if sh["status"] != "failed":
                    raise Vpoc2Error("PreconditionFailed", "실패한 샷만 다시 만든다", http=409)
                sh.update({"status": "queued", "attempt": sh["attempt"] + 1, "arn": None, "submittedAt": None,
                           "completedAt": None, "outputKey": None, "error": None, "note": None, "lastPolledAt": 0})
                run["video"]["status"] = "running"
                run["video"]["final"] = {"status": "none", "key": None, "error": None}
                return
        raise Vpoc2Error("NotFound", f"샷 {shot_id} 이 없다", http=404)

    update(owner, run_id, mutate)


def _refresh_video(owner: str, run_id: str, run: dict[str, Any]) -> bool:
    """조회 때 샷을 제출 · 확인한다. run 을 제자리에서 고치고, 바뀌었으면 True."""
    _llm, _images, video = providers()
    vid = run["video"]
    if vid["status"] not in ("running", "partial"):
        return False
    changed = False
    now = time.time()
    inflight = sum(1 for sh in vid["shots"] if sh["status"] == "submitted")
    for sh in vid["shots"]:
        due = settings.MOCK or now - sh.get("lastPolledAt", 0) >= settings.POLL_MIN_SECONDS
        if sh["status"] == "submitted" and due:
            try:
                st = video.poll(sh["arn"])
            except Vpoc2Error as exc:
                sh["note"] = f"조회 실패: {exc.message}"
                continue
            sh["lastPolledAt"] = now
            changed = True
            if st["status"] == "Completed":
                prefix = _out_prefix(owner, run_id, sh)
                key = video.find_output(prefix)
                if key:
                    sh.update({"status": "completed", "completedAt": now_iso(), "outputKey": key, "note": None})
                else:
                    sh["note"] = "완료됐지만 출력 파일을 아직 찾지 못했다"
                inflight -= 1
            elif st["status"] == "Failed":
                sh.update({"status": "failed", "completedAt": now_iso(), "error": {
                    "where": "luma.get_async_invoke", "type": "AsyncInvokeFailed",
                    "message": st.get("failureMessage") or "", "requestId": "", "httpStatus": 200, "at": now_iso()}})
                inflight -= 1
    for sh in vid["shots"]:
        if sh["status"] != "queued" or inflight >= settings.MAX_INFLIGHT:
            continue
        token = _token(run_id, sh)
        try:
            arn = video.submit(sh["promptEn"], resolution=vid["resolution"], token=token,
                               out_prefix=_out_prefix(owner, run_id, sh))
        except Vpoc2Error as exc:
            sh.update({"status": "failed", "error": exc.envelope()})
            changed = True
            continue
        except Exception as exc:  # noqa: BLE001 - 스로틀은 대기열로 (과금 없음)
            if type(exc).__name__ == "Throttled":
                sh["note"] = "동시 제출 한도로 대기 중"
                changed = True
                continue
            sh.update({"status": "failed", "error": from_client_error(exc, "luma.start_async_invoke").envelope()})
            changed = True
            continue
        if not settings.MOCK:
            ledger.charge(token, user=owner, run_id=run_id, kind="video", model=settings.VIDEO_MODEL,
                          units=settings.SHOT_SECONDS, usd=settings.shot_usd(vid["resolution"]),
                          resolution=vid["resolution"])
        sh.update({"status": "submitted", "arn": arn, "submittedAt": now_iso(), "lastPolledAt": now, "note": None})
        inflight += 1
        changed = True
    for sh in vid["shots"]:
        if sh["status"] == "queued" and not sh.get("note"):
            sh["note"] = "동시 제출 한도로 대기 중"
            changed = True
    statuses = {sh["status"] for sh in vid["shots"]}
    new_status = "done" if statuses == {"completed"} else ("partial" if "failed" in statuses and not (
        statuses & {"queued", "submitted"}) else "running")
    if new_status != vid["status"]:
        vid["status"] = new_status
        changed = True
    # 제출된 시도 수 × 단가 (실패 작업도 계상 — PRD 6.6)
    vid["costUsd"] = round(sum(settings.shot_usd(vid["resolution"]) * (sh["attempt"] - (sh["status"] == "queued"))
                               for sh in vid["shots"]), 2)
    return changed


def _token(run_id: str, sh: dict[str, Any]) -> str:
    from . import video as real_video

    return real_video.client_token(run_id, sh["id"], sh["attempt"])


def _out_prefix(owner: str, run_id: str, sh: dict[str, Any]) -> str:
    return f"{store.run_prefix(owner, run_id)}/shots/{sh['id']}-{sh['attempt']}/"


def run_stitch(owner: str, run_id: str) -> None:
    from . import stitch

    def precheck(run: dict[str, Any]) -> None:
        if run["video"]["status"] != "done":
            raise Vpoc2Error("PreconditionFailed", "샷 3개가 모두 완료돼야 합친다", http=409)

    def work(snap: dict[str, Any]):
        keys = [sh["outputKey"] for sh in snap["video"]["shots"]]
        data = stitch.concat(keys)
        # 주 버킷은 ListBucket 권한을 확인하지 않았다 → 나열하지 않고 시각으로 이름을 짓는다
        stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d%H%M%S")
        key = f"{store.run_prefix(owner, run_id)}/final-{stamp}.mp4"
        store.get_store().put_bytes(store.MAIN, key, data, "video/mp4")

        def apply(run: dict[str, Any]) -> None:
            if [sh["outputKey"] for sh in run["video"]["shots"]] != keys:
                return
            run["video"]["final"] = {"status": "ready", "key": key, "error": None, "at": now_iso()}
            run["maxStep"] = max(run["maxStep"], 5)  # ④ 에서 완성본을 보고 [리뷰 남기기] 로 넘어간다

        return apply

    start_task(owner, run_id, "stitch", precheck, work)


def refresh(owner: str, run_id: str) -> dict[str, Any]:
    """GET 때 부른다. 멈춘 작업 표시 · 영상 진행 · 합본 시작."""
    run, _ = load(owner, run_id)
    need_write = False
    t = run["task"]
    if t["status"] == "running" and not _task_running(run):
        need_write = True
    if run["video"]["status"] in ("running", "partial") and any(
            sh["status"] in ("queued", "submitted") for sh in run["video"]["shots"]):
        need_write = True
    if need_write:
        def mutate(r: dict[str, Any]) -> None:
            if r["task"]["status"] == "running" and not _task_running(r):
                r["task"].update({"status": "error", "token": None, "finishedAt": now_iso(), "error": {
                    "where": f"task.{r['task']['name']}", "type": "TaskStale",
                    "message": "중단됨 (서버 재시작 추정). 다시 실행한다", "requestId": "", "httpStatus": 500,
                    "at": now_iso()}})
            _refresh_video(owner, run_id, r)

        try:
            update(owner, run_id, mutate)
        except Vpoc2Error:
            pass
        run, _ = load(owner, run_id)
    # 합본 자동 시작. 합본이 실패했으면 자동으로 다시 돌리지 않는다 (반복 실패는 결함) — [다시 합치기] 로만
    stitch_failed = run["task"].get("name") == "stitch" and run["task"]["status"] == "error"
    if run["video"]["status"] == "done" and run["video"]["final"]["status"] in ("none", "stale") \
            and not _task_running(run) and not stitch_failed:
        try:
            run_stitch(owner, run_id)
        except Vpoc2Error:
            pass
        run, _ = load(owner, run_id)
    return run


# --- 되돌리기 -----------------------------------------------------------------

def back(owner: str, run_id: str, to_step: int) -> None:
    def mutate(run: dict[str, Any]) -> None:
        if not 1 <= to_step <= run["maxStep"]:
            raise Vpoc2Error("PreconditionFailed", f"{to_step} 단계로 갈 수 없다 (최대 {run['maxStep']})", http=409)
        if _task_running(run) and to_step < run["step"]:
            _cancel_task(run)
        run["step"] = to_step

    update(owner, run_id, mutate)


# --- 5 리뷰 -------------------------------------------------------------------

REVIEW_MARKS = {"yes", "partial", "no"}


def save_review(owner: str, run_id: str, body: dict[str, Any]) -> dict[str, Any]:
    score = body.get("score")
    if not isinstance(score, int) or not 1 <= score <= 5:
        raise Vpoc2Error("BadScore", "점수는 1~5 정수", http=400)
    marks = body.get("elements") or {}
    good = (body.get("goodKo") or "").strip()[:1000]
    bad = (body.get("badKo") or "").strip()[:1000]
    author = (body.get("author") or owner).strip()[:40]

    def mutate(run: dict[str, Any]) -> dict[str, Any]:
        if run["video"]["final"]["status"] != "ready":
            raise Vpoc2Error("PreconditionFailed", "15초 영상이 완성된 뒤 리뷰한다", http=409)
        elements = (run["interpretation"]["data"] or {}).get("elements", [])
        ids = [e["id"] for e in elements]
        clean = {eid: marks.get(eid) for eid in ids}
        missing = [eid for eid, m in clean.items() if m not in REVIEW_MARKS]
        if missing:
            raise Vpoc2Error("ReviewIncomplete", f"요소별 반영 여부를 모두 고른다: {', '.join(missing)}", http=400)
        yes = sum(1 for m in clean.values() if m == "yes")
        partial = sum(1 for m in clean.values() if m == "partial")
        review = {"score": score, "elements": clean, "goodKo": good, "badKo": bad, "author": author,
                  "savedAt": now_iso(), "reflectionRate": round(yes / len(ids), 3) if ids else None,
                  "yes": yes, "partial": partial, "no": len(ids) - yes - partial}
        run["review"] = review
        run["step"] = 5
        return review

    review = update(owner, run_id, mutate)
    run, _ = load(owner, run_id)
    data = run["interpretation"]["data"] or {}
    entry = {"runId": run_id, "owner": owner, "titleKo": data.get("titleKo"), "command": run["input"]["command"],
             "resolution": run["video"]["resolution"], "elements": data.get("elements", []),
             "prompts": [sh["promptEn"] for sh in run["video"]["shots"]], **review}

    def add(all_reviews: dict[str, Any]) -> None:
        all_reviews["items"] = [r for r in all_reviews["items"]
                                if not (r["runId"] == run_id and r["owner"] == owner)] + [entry]

    store.update_json(store.get_store(), store.REVIEWS_KEY, {"items": []}, add)
    _index_upsert(owner, run)
    return review


def reviews_summary() -> dict[str, Any]:
    items = store.get_or_create_json(store.get_store(), store.REVIEWS_KEY, {"items": []})[0]["items"]
    rates = [r["reflectionRate"] for r in items if r.get("reflectionRate") is not None]
    return {
        "count": len(items),
        "avgScore": round(sum(r["score"] for r in items) / len(items), 2) if items else None,
        "avgReflectionRate": round(sum(rates) / len(rates), 3) if rates else None,
        "items": sorted(items, key=lambda r: r["savedAt"], reverse=True)[:100],
    }


# --- 화면용 보기 ---------------------------------------------------------------

def view(run: dict[str, Any]) -> dict[str, Any]:
    s = store.get_store()
    out = copy.deepcopy(run)
    out["task"].pop("token", None)
    out["task"]["error"] = public(out["task"].get("error"), settings.RAW_ERRORS)
    for it in out["characters"]["items"]:
        if it.get("image"):
            it["image"]["url"] = s.presign(store.MAIN, it["image"]["key"], "image/jpeg")
        for i, h in enumerate(it.get("history") or [], 1):
            h["url"] = s.presign(store.MAIN, h["key"], "image/jpeg")
            h["version"] = i
    for sh in out["video"]["shots"]:
        sh["videoUrl"] = s.presign(store.VIDEO, sh["outputKey"], "video/mp4") if sh.get("outputKey") else None
        sh["error"] = public(sh.get("error"), settings.RAW_ERRORS)
        sh.pop("arn", None)
        if sh.get("submittedAt"):
            end = sh.get("completedAt") or now_iso()
            fmt = "%Y-%m-%dT%H:%M:%SZ"
            sh["elapsedS"] = int((_dt.datetime.strptime(end, fmt) - _dt.datetime.strptime(sh["submittedAt"], fmt))
                                 .total_seconds())
    final = out["video"]["final"]
    final["url"] = s.presign(store.MAIN, final["key"], "video/mp4") if final.get("key") else None
    out["cost"] = {
        "shotUsd": {r: settings.shot_usd(r) for r in settings.USD_PER_SEC},
        "videoUsd": {r: settings.video_usd(r) for r in settings.USD_PER_SEC},
    }
    return out


def meta(owner: str) -> dict[str, Any]:
    month = now_iso()[:7]
    used = ledger.hq_used(month)
    return {
        "mock": settings.MOCK,
        "shots": settings.SHOTS, "shotSeconds": settings.SHOT_SECONDS,
        "defaultResolution": settings.DEFAULT_RESOLUTION, "hqResolution": settings.HQ_RESOLUTION,
        "hq": {"month": month, "limit": settings.HQ_MONTHLY_LIMIT, "used": used,
               "remaining": max(0, settings.HQ_MONTHLY_LIMIT - used)},
        "videoUsd": {r: settings.video_usd(r) for r in settings.USD_PER_SEC},
        "spentUsd": ledger.read().get("spentUsd", 0.0),
        "models": {"text": settings.TEXT_MODEL, "image": settings.IMAGE_MODEL, "video": settings.VIDEO_MODEL},
    }
