"""vpoc2 라우트 (/api/vpoc2). 인증은 앱에서 주입받는다 — g 를 직접 읽지 않는다.

작업은 만든 사람만 보고 고친다 (v1: 프로젝트와 무관한 개인 작업).
"""
from __future__ import annotations

import functools
import logging
from dataclasses import dataclass
from typing import Any, Callable

from flask import Blueprint, Response, abort, jsonify, request

from . import service, settings, store
from .errors import Vpoc2Error, from_client_error, public

log = logging.getLogger("vpoc2")


@dataclass
class Vpoc2Dependencies:
    require_auth: Callable[[Callable], Callable]
    current_username: Callable[[], str]


def _envelope(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Vpoc2Error as exc:
            err = exc
        except Exception as exc:  # noqa: BLE001 - 원문을 봉투에 담는다
            log.exception("vpoc2 unexpected")
            err = from_client_error(exc, f"vpoc2.{fn.__name__}")
            err.http = 500
        env = err.envelope()
        log.warning("vpoc2.error where=%s type=%s msg=%s", env["where"], env["type"], env["message"][:300])
        return jsonify({"ok": False, "error": public(env, settings.RAW_ERRORS)}), err.http

    return wrapper


def _body() -> dict[str, Any]:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return {}
    return data


def create_blueprint(deps: Vpoc2Dependencies) -> Blueprint:
    bp = Blueprint("vpoc2", __name__, url_prefix="/api/vpoc2")

    def route(rule: str, methods: list[str]):
        def deco(fn):
            wrapped = deps.require_auth(_envelope(fn))
            bp.add_url_rule(rule, endpoint=fn.__name__, view_func=wrapped, methods=methods)
            return fn

        return deco

    def me() -> str:
        name = deps.current_username()
        if not name:
            raise Vpoc2Error("Unauthorized", "로그인이 필요하다", http=401)
        return name

    def run_view(run_id: str) -> Response:
        return jsonify({"ok": True, "run": service.view(service.refresh(me(), run_id))})

    @bp.before_request
    def _enabled():
        if not settings.ENABLED:
            abort(404)

    @route("/meta", ["GET"])
    def meta():
        return jsonify({"ok": True, **service.meta(me())})

    @route("/runs", ["GET"])
    def list_runs():
        return jsonify({"ok": True, "runs": service.list_runs(me())})

    @route("/runs", ["POST"])
    def create_run():
        run = service.create(me(), (_body().get("command") or "").strip()[:2000])
        return jsonify({"ok": True, "run": service.view(run)})

    @route("/runs/<run_id>", ["GET"])
    def get_run(run_id: str):
        return run_view(run_id)

    @route("/runs/<run_id>/input", ["PUT"])
    def put_input(run_id: str):
        b = _body()
        service.save_input(me(), run_id, b.get("command", ""), b.get("characters") or [])
        return run_view(run_id)

    @route("/runs/<run_id>/interpretation", ["PUT"])
    def put_interpretation(run_id: str):
        data = _body().get("data")
        if not isinstance(data, dict):
            raise Vpoc2Error("BadRequest", "data 가 없다", http=400)
        service.save_interpretation(me(), run_id, data)
        return run_view(run_id)

    @route("/runs/<run_id>/prompts/<shot_id>", ["PUT"])
    def put_prompt(run_id: str, shot_id: str):
        service.edit_prompt(me(), run_id, shot_id, _body().get("promptEn", ""))
        return run_view(run_id)

    @route("/runs/<run_id>/characters/<char_id>/confirm", ["POST"])
    def confirm(run_id: str, char_id: str):
        b = _body()
        version = b.get("version")
        service.confirm_character(me(), run_id, char_id, bool(b.get("confirmed", True)),
                                  int(version) if version else None)
        return run_view(run_id)

    @route("/runs/<run_id>/run/<action>", ["POST"])
    def run_action(run_id: str, action: str):
        b = _body()
        user = me()
        if action == "interpret":
            service.run_interpret(user, run_id)
        elif action == "draw":
            service.run_draw(user, run_id, b.get("charId"), b.get("feedbackKo", ""))
        elif action == "prepare":
            service.run_prepare(user, run_id)
        elif action == "generate":
            service.generate(user, run_id, b.get("resolution", settings.DEFAULT_RESOLUTION),
                             float(b.get("confirmUsd", -1)))
        elif action == "retry":
            service.retry_shot(user, run_id, b.get("shotId", ""))
        elif action == "stitch":
            service.run_stitch(user, run_id)
        else:
            raise Vpoc2Error("BadAction", action, http=404)
        return run_view(run_id), 202 if action in {"interpret", "draw", "prepare", "stitch"} else 200

    @route("/runs/<run_id>/back", ["POST"])
    def go_back(run_id: str):
        service.back(me(), run_id, int(_body().get("toStep", 1)))
        return run_view(run_id)

    @route("/runs/<run_id>/review", ["PUT"])
    def put_review(run_id: str):
        service.save_review(me(), run_id, _body())
        return run_view(run_id)

    @route("/reviews", ["GET"])
    def reviews():
        return jsonify({"ok": True, **service.reviews_summary()})

    if settings.MOCK and settings.LOCAL_DIR:
        # 목 모드 전용: 로컬 파일을 presign URL 대신 내려 준다. 운영 · dev 배포에는 등록되지 않는다.
        @bp.route("/_local/<bucket>/<path:key>", methods=["GET"])
        def local_file(bucket: str, key: str):
            if bucket not in (store.MAIN, store.VIDEO):
                abort(404)
            got = store.get_store().get_bytes(bucket, key)
            if got is None:
                abort(404)
            ctype = "video/mp4" if key.endswith(".mp4") else "image/jpeg" if key.endswith(".jpg") else "application/json"
            return Response(got[0], mimetype=ctype)

    return bp
