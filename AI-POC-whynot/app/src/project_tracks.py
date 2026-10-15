"""Whynot project-track adapter and route installer.

The shared graph_store project implementation remains untouched. This module adds the customer-owned
track/work-title projection, set-once selection, completed-video summary, HTTP handlers, and story
route guards on top of the existing Flask app.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import wraps
from typing import Callable

from flask import jsonify, request

from . import graph_store

TRACK_STORY = "story"
TRACK_VIDEO = "video"
TRACKS = (TRACK_STORY, TRACK_VIDEO)
TRACK_LABELS = {
    TRACK_STORY: "스토리 개발로 진행",
    TRACK_VIDEO: "영상 개발로 진행",
}
WORK_TITLE_MAX_CHARS = 120
logger = logging.getLogger("toon-change-ai-pj-app.project-tracks")

_STORY_PROJECT_ROUTE_ROOTS = frozenset({
    "assets",
    "bible",
    "direction",
    "documents",
    "outputs",
    "script-jobs",
    "source",
    "story",
    "story-text",
    "summary",
})
_PROJECT_NAMESPACE = uuid.UUID("d7ba8d68-1e12-49e9-a24f-72608f0f668e")


class ProjectTrackError(RuntimeError):
    pass


class ProjectTrackConflict(ProjectTrackError):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def normalize_track(value) -> str | None:
    track = str(value or "").strip()
    return track if track in TRACKS else None


def normalize_work_title(value) -> str:
    title = str(value or "").strip()
    if len(title) > WORK_TITLE_MAX_CHARS:
        raise ProjectTrackError(f"작품명은 {WORK_TITLE_MAX_CHARS}자 이하여야 합니다.")
    return title


def track_enabled(track: str, *, story_enabled: bool, video_enabled: bool) -> bool:
    return (track == TRACK_STORY and story_enabled) or (track == TRACK_VIDEO and video_enabled)


def track_command(project: dict, *, actor: str) -> dict:
    track = normalize_track(project.get("track"))
    if not track:
        raise ProjectTrackError("track command를 만들 project track이 없습니다.")
    project_id = str(project.get("id") or "")
    selected_at = str(project.get("trackSelectedAt") or "")
    if not project_id or not selected_at:
        raise ProjectTrackError("track command 정본 필드가 없습니다.")
    return {
        "id": f"project-track:{project_id}",
        "ts": selected_at,
        "userId": str(project.get("trackSelectedBy") or actor),
        "stage": "project",
        "action": "select_project_track",
        "target": {"projectId": project_id, "track": track},
        "text": TRACK_LABELS[track],
    }


def create_request_hash(payload: dict, *, track: str, work_title: str) -> str:
    basis = {
        "title": str(payload.get("title") or "").strip(),
        "story": str(payload.get("story") or "").strip(),
        "visibility": str(payload.get("visibility") or graph_store.VISIBILITY_PRIVATE).strip(),
        "inputType": str(payload.get("inputType") or graph_store.INPUT_WEBTOON).strip(),
        "direction": payload.get("direction") if isinstance(payload.get("direction"), dict) else {},
        "track": track,
        "workTitle": work_title,
    }
    return hashlib.sha256(canonical_json(basis).encode("utf-8")).hexdigest()


def suggested_track(project: dict) -> tuple[str | None, str]:
    """Return a read-only suggestion. It never mutates a project or command log."""
    if normalize_track(project.get("track")):
        return None, ""
    story_text = project.get("storyText") if isinstance(project.get("storyText"), dict) else {}
    story_evidence = bool(
        project.get("storyType")
        or int(story_text.get("charCount") or 0) > 0
        or project.get("storyAssets")
        or str(project.get("productionType") or "") == "storyboard"
    )
    video_evidence = bool(
        int(project.get("completedVideoCount") or 0) > 0
        or str(project.get("productionType") or "") == "video"
    )
    if story_evidence and not video_evidence:
        return TRACK_STORY, "기존 스토리 산출물이 있어 스토리 개발을 추천합니다."
    if video_evidence and not story_evidence:
        return TRACK_VIDEO, "기존 영상 작업이 있어 영상 개발을 추천합니다."
    if story_evidence and video_evidence:
        return None, "스토리와 영상 작업 근거가 모두 있어 직접 선택해야 합니다."
    return None, "기존 작업 근거가 없어 직접 선택해야 합니다."


def _metadata_view(row: dict | None) -> dict:
    row = row or {}
    return {
        "track": normalize_track(row.get("track")),
        "workTitle": str(row.get("workTitle") or ""),
        "trackSelectedAt": str(row.get("trackSelectedAt") or ""),
        "trackSelectedBy": str(row.get("trackSelectedBy") or ""),
        "completedVideoCount": max(0, int(row.get("completedVideoCount") or 0)),
        "latestVideoCompletedAt": row.get("latestVideoCompletedAt") or None,
        # vpoc1: 메인 목록의 영상 상태 배지. draft · generating · done.
        "vpocStatus": row.get("vpocStatus") or None,
        "vpocPlanId": row.get("vpocPlanId") or None,
    }


def enrich_project(project: dict, metadata: dict | None = None) -> dict:
    result = copy.deepcopy(project)
    result.update(_metadata_view(metadata))
    result["trackSelectionRequired"] = result["track"] is None
    suggestion, reason = suggested_track(result)
    result["suggestedTrack"] = suggestion
    result["trackSuggestionReason"] = reason
    return result


class GraphProjectTrackRepository:
    """Whynot-only Neptune adapter; graph_store.py stays unchanged."""

    def metadata_for(self, project_ids: list[str]) -> dict[str, dict]:
        ids = list(dict.fromkeys(str(value) for value in project_ids if str(value)))
        if not ids:
            return {}
        rows = graph_store.run(
            """
            MATCH (p:project)
            WHERE p.id IN $projectIds
            RETURN p.id AS projectId,
                   p.track AS track,
                   p.workTitle AS workTitle,
                   p.trackSelectedAt AS trackSelectedAt,
                   p.trackSelectedBy AS trackSelectedBy,
                   CASE WHEN p.vpocStatus = 'done' THEN 1 ELSE 0 END AS completedVideoCount,
                   p.vpocUpdatedAt AS latestVideoCompletedAt,
                   p.vpocStatus AS vpocStatus,
                   p.vpocPlanId AS vpocPlanId,
                   p.vpocThumbKey AS vpocThumbKey
            """,
            {"projectIds": ids},
        )
        return {str(row.get("projectId") or ""): _metadata_view(row) for row in rows}

    def enrich_many(self, projects: list[dict]) -> list[dict]:
        metadata = self.metadata_for([str(project.get("id") or "") for project in projects])
        return [enrich_project(project, metadata.get(str(project.get("id") or ""))) for project in projects]

    def enrich_buckets(self, buckets: dict) -> dict:
        shared = list(buckets.get("shared") or [])
        private = list(buckets.get("private") or [])
        all_projects = [*shared, *private]
        metadata = self.metadata_for([str(project.get("id") or "") for project in all_projects])

        def decorate(project: dict) -> dict:
            return enrich_project(project, metadata.get(str(project.get("id") or "")))

        return {
            "shared": [decorate(project) for project in shared],
            "private": [decorate(project) for project in private],
        }

    def get_project(self, project_id: str) -> dict | None:
        project = graph_store.get_project(project_id)
        if not project:
            return None
        metadata = self.metadata_for([project_id]).get(project_id)
        return enrich_project(project, metadata)

    def create_or_get(
        self,
        *,
        owner: str,
        create_key: str,
        create_hash: str,
        title: str,
        story: str,
        visibility: str,
        input_type: str,
        direction: dict,
        production_type: str,
        track: str,
        work_title: str,
        selected_at: str,
    ) -> tuple[dict, bool]:
        if visibility not in {graph_store.VISIBILITY_SHARED, graph_store.VISIBILITY_PRIVATE}:
            raise ProjectTrackError("visibility는 shared 또는 private만 허용됩니다.")
        if input_type not in graph_store.INPUT_TYPES:
            raise ProjectTrackError("진입점 타입이 올바르지 않습니다.")
        try:
            normalized_direction = graph_store.normalize_direction(direction or {})
        except graph_store.GraphError as error:
            raise ProjectTrackError(str(error)) from error
        direction_json = json.dumps(
            normalized_direction,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        project_id = uuid.uuid5(_PROJECT_NAMESPACE, f"{owner}:{create_key}").hex
        create_token = uuid.uuid4().hex
        rows = graph_store.run(
            """
            MATCH (u:user {username: $owner})
            MERGE (p:project {id: $projectId})
            ON CREATE SET p.title = $title,
                          p.story = $story,
                          p.visibility = $visibility,
                          p.inputType = $inputType,
                          p.directionJson = $directionJson,
                          p.sourceCharCount = 0,
                          p.summaryStory = $summaryStory,
                          p.productionType = $productionType,
                          p.track = $track,
                          p.workTitle = $workTitle,
                          p.trackSelectedAt = $selectedAt,
                          p.trackSelectedBy = $owner,
                          p.trackOwner = $owner,
                          p.trackCreateKey = $createKey,
                          p.trackCreateHash = $createHash,
                          p.trackCreateToken = $createToken,
                          p.createdAt = $selectedAt,
                          p.updatedAt = $selectedAt
            WITH u, p
            WHERE p.trackOwner = $owner
              AND p.trackCreateKey = $createKey
              AND p.trackCreateHash = $createHash
            MERGE (u)-[:owns]->(p)
            RETURN p.id AS projectId, p.trackCreateToken AS createToken
            """,
            {
                "owner": owner,
                "projectId": project_id,
                "createKey": create_key,
                "createHash": create_hash,
                "createToken": create_token,
                "title": title,
                "story": story,
                "visibility": visibility,
                "inputType": input_type,
                "directionJson": direction_json,
                "summaryStory": story[:300],
                "productionType": production_type,
                "track": track,
                "workTitle": work_title,
                "selectedAt": selected_at,
            },
        )
        if not rows:
            conflict = graph_store.run(
                "MATCH (p:project {id: $projectId}) RETURN p.trackCreateHash AS requestHash",
                {"projectId": project_id},
            )
            if conflict:
                raise ProjectTrackConflict("같은 프로젝트 생성 요청 ID가 다른 내용에 사용됐습니다.")
            raise graph_store.GraphError("프로젝트 소유자를 찾지 못했거나 프로젝트를 생성하지 못했습니다.")
        project = self.get_project(project_id)
        if not project:
            raise graph_store.GraphError("생성된 프로젝트를 읽지 못했습니다.")
        return project, rows[0].get("createToken") != create_token

    def select_once(
        self,
        project_id: str,
        *,
        owner: str,
        track: str,
        work_title: str,
        selected_at: str,
        selected_by: str,
        create_key: str = "",
        create_hash: str = "",
    ) -> dict | None:
        rows = graph_store.run(
            """
            MATCH (u:user {username: $owner})-[:owns]->(p:project {id: $projectId})
            WHERE coalesce(p.track, '') = ''
            SET p.track = $track,
                p.workTitle = $workTitle,
                p.trackSelectedAt = $selectedAt,
                p.trackSelectedBy = $selectedBy,
                p.trackCreateKey = $createKey,
                p.trackCreateHash = $createHash,
                p.updatedAt = $selectedAt
            RETURN p.id AS projectId
            """,
            {
                "owner": owner,
                "projectId": project_id,
                "track": track,
                "workTitle": work_title,
                "selectedAt": selected_at,
                "selectedBy": selected_by,
                "createKey": create_key,
                "createHash": create_hash,
            },
        )
        return self.get_project(project_id) if rows else None

    def update_work_title(
        self,
        project_id: str,
        *,
        owner: str,
        work_title: str,
        updated_at: str,
    ) -> dict | None:
        rows = graph_store.run(
            """
            MATCH (u:user {username: $owner})-[:owns]->(p:project {id: $projectId})
            SET p.workTitle = $workTitle, p.updatedAt = $updatedAt
            RETURN p.id AS projectId
            """,
            {
                "owner": owner,
                "projectId": project_id,
                "workTitle": work_title,
                "updatedAt": updated_at,
            },
        )
        return self.get_project(project_id) if rows else None


@dataclass
class ProjectTrackDependencies:
    require_auth: Callable
    response_error: Callable
    current_username: Callable[[], str]
    list_projects: Callable[[str], dict]
    project_payload: Callable[[dict], dict]
    can_read_project: Callable[[dict], bool]
    can_write_project: Callable[[dict], bool]
    command_store: object
    repository: object
    story_enabled: bool = True
    video_enabled: bool = False


def _story_project_rule(rule: str) -> bool:
    prefix = "/api/projects/<project_id>/"
    if not rule.startswith(prefix):
        return False
    first_segment = rule[len(prefix):].split("/", 1)[0]
    return first_segment in _STORY_PROJECT_ROUTE_ROOTS


def _availability_error(deps: ProjectTrackDependencies, track: str):
    if not deps.story_enabled and not deps.video_enabled:
        return deps.response_error(
            "스토리 개발과 영상 개발이 모두 꺼져 있어 프로젝트를 만들 수 없습니다.",
            503,
            "PROJECT_TRACKS_DISABLED",
        )
    if not track_enabled(track, story_enabled=deps.story_enabled, video_enabled=deps.video_enabled):
        label = "스토리 개발" if track == TRACK_STORY else "영상 개발"
        return deps.response_error(
            f"이 환경에서는 {label} 기능이 꺼져 있습니다.",
            403,
            "PROJECT_TRACK_DISABLED",
        )
    return None


def _append_track_command(deps: ProjectTrackDependencies, project: dict, actor: str):
    try:
        command, reused = deps.command_store.append_project_command(
            project,
            track_command(project, actor=actor),
        )
        return command, reused, None
    except Exception as error:
        logger.error("project track command append failed project=%s error=%s", project.get("id"), error)
        return None, False, deps.response_error(
            "프로젝트는 저장됐지만 트랙 선택 기록을 저장하지 못했습니다.",
            503,
            "TRACK_COMMAND_SAVE_FAILED",
            retryable=True,
            action="같은 선택으로 다시 시도해 주세요.",
        )


def install(app, deps: ProjectTrackDependencies) -> None:
    """Install Whynot project handlers after all shared server routes have been declared."""
    if app.extensions.get("whynot_project_tracks"):
        return
    missing_endpoints = [
        endpoint for endpoint in ("projects", "project_detail")
        if endpoint not in app.view_functions
    ]
    if missing_endpoints:
        raise ProjectTrackError(
            "project track installer에 필요한 endpoint가 없습니다: " + ", ".join(missing_endpoints)
        )
    app.extensions["whynot_project_tracks"] = True

    original_detail = app.view_functions["project_detail"]
    detail_inner = getattr(original_detail, "__wrapped__", original_detail)

    def collection_handler():
        actor = deps.current_username()
        if request.method in ("GET", "HEAD"):
            try:
                buckets = deps.repository.enrich_buckets(deps.list_projects(actor))
            except graph_store.GraphError as error:
                return deps.response_error(str(error), 503, "PROJECT_LIST_FAILED", retryable=True)
            return jsonify({
                "ok": True,
                "shared": [deps.project_payload(project) for project in buckets["shared"]],
                "private": [deps.project_payload(project) for project in buckets["private"]],
            })

        payload = request.get_json(silent=True) or {}
        if not deps.story_enabled and not deps.video_enabled:
            return _availability_error(deps, "")
        raw_track = str(payload.get("track") or "").strip()
        if not raw_track:
            return deps.response_error("프로젝트 개발 트랙을 선택해 주세요.", 400, "PROJECT_TRACK_REQUIRED")
        if raw_track not in TRACKS:
            return deps.response_error("프로젝트 트랙은 story 또는 video여야 합니다.", 400, "INVALID_PROJECT_TRACK")
        unavailable = _availability_error(deps, raw_track)
        if unavailable:
            return unavailable

        title = str(payload.get("title") or "").strip()
        story = str(payload.get("story") or "").strip()
        visibility = str(payload.get("visibility") or graph_store.VISIBILITY_PRIVATE).strip()
        input_type = str(payload.get("inputType") or graph_store.INPUT_WEBTOON).strip()
        direction = payload.get("direction") or {}
        if not isinstance(direction, dict):
            return deps.response_error("기획 방향 값이 올바르지 않습니다.", 400, "INVALID_DIRECTION")
        try:
            work_title = normalize_work_title(payload.get("workTitle"))
        except ProjectTrackError as error:
            return deps.response_error(str(error), 400, "INVALID_WORK_TITLE")
        if not 1 <= len(title) <= 120:
            return deps.response_error("프로젝트 제목은 1~120자여야 합니다.", 400, "INVALID_TITLE")
        if len(story) > 8000:
            return deps.response_error("스토리는 8000자 이하여야 합니다.", 400, "STORY_TOO_LONG")

        raw_create_key = str(payload.get("idempotencyKey") or "")
        create_key = raw_create_key.strip()
        if not create_key:
            return deps.response_error(
                "프로젝트 생성 요청 ID가 필요합니다.",
                400,
                "IDEMPOTENCY_KEY_REQUIRED",
            )
        if len(create_key) > 160:
            return deps.response_error(
                "프로젝트 생성 요청 ID는 160자 이하여야 합니다.",
                400,
                "INVALID_IDEMPOTENCY_KEY",
            )
        request_hash = create_request_hash(payload, track=raw_track, work_title=work_title)
        legacy_production_type = "storyboard" if raw_track == TRACK_STORY else "video"
        try:
            selected, reused = deps.repository.create_or_get(
                owner=actor,
                create_key=create_key,
                create_hash=request_hash,
                title=title,
                story=story,
                visibility=visibility,
                input_type=input_type,
                direction=direction,
                production_type=legacy_production_type,
                track=raw_track,
                work_title=work_title,
                selected_at=now_iso(),
            )
        except ProjectTrackConflict as error:
            return deps.response_error(str(error), 409, "IDEMPOTENCY_KEY_REUSED")
        except ProjectTrackError as error:
            return deps.response_error(str(error), 400, "PROJECT_CREATE_FAILED")
        except graph_store.GraphError as error:
            return deps.response_error(
                str(error),
                503,
                "PROJECT_CREATE_FAILED",
                retryable=True,
            )

        _command, _command_reused, command_error = _append_track_command(deps, selected, actor)
        if command_error:
            return command_error
        return jsonify({
            "ok": True,
            "item": deps.project_payload(selected),
            "reused": reused,
        }), (200 if reused else 201)

    collection_handler.__name__ = "projects"
    app.view_functions["projects"] = deps.require_auth(collection_handler)

    def detail_handler(project_id: str):
        project = deps.repository.get_project(project_id)
        if not project:
            return deps.response_error("프로젝트를 찾을 수 없습니다.", 404, "PROJECT_NOT_FOUND")
        if not deps.can_read_project(project):
            return deps.response_error("이 프로젝트에 접근할 권한이 없습니다.", 403, "PROJECT_FORBIDDEN")
        if request.method in ("GET", "HEAD"):
            return jsonify({"ok": True, "item": deps.project_payload(project)})
        if request.method == "DELETE":
            return detail_inner(project_id)
        if not deps.can_write_project(project):
            return deps.response_error("본인 프로젝트만 수정할 수 있습니다.", 403, "PROJECT_FORBIDDEN")

        payload = request.get_json(silent=True) or {}
        if "track" in payload:
            return deps.response_error(
                "프로젝트 트랙은 만든 뒤 변경할 수 없습니다. 새 프로젝트를 만들어 주세요.",
                409,
                "PROJECT_TRACK_IMMUTABLE",
            )
        has_work_title = "workTitle" in payload
        try:
            work_title = normalize_work_title(payload.get("workTitle")) if has_work_title else ""
        except ProjectTrackError as error:
            return deps.response_error(str(error), 400, "INVALID_WORK_TITLE")

        base_fields = {"title", "story", "visibility", "inputType", "productionType"}
        has_base_fields = any(key in payload for key in base_fields)
        if has_work_title and has_base_fields:
            return deps.response_error(
                "작품명과 프로젝트 기본 정보는 한 요청에서 함께 바꿀 수 없습니다.",
                400,
                "PROJECT_PATCH_SPLIT_REQUIRED",
            )
        if has_base_fields or not has_work_title:
            base_result = detail_inner(project_id)
            base_response = app.make_response(base_result)
            if base_response.status_code >= 400:
                return base_response
        if has_work_title:
            try:
                updated = deps.repository.update_work_title(
                    project_id,
                    owner=str(project.get("owner") or deps.current_username()),
                    work_title=work_title,
                    updated_at=now_iso(),
                )
            except graph_store.GraphError as error:
                return deps.response_error(str(error), 503, "PROJECT_UPDATE_FAILED", retryable=True)
            if not updated:
                return deps.response_error("프로젝트를 찾을 수 없습니다.", 404, "PROJECT_NOT_FOUND")
        else:
            updated = deps.repository.get_project(project_id)
        return jsonify({"ok": True, "item": deps.project_payload(updated)})

    detail_handler.__name__ = "project_detail"
    app.view_functions["project_detail"] = deps.require_auth(detail_handler)

    def select_track_handler(project_id: str):
        actor = deps.current_username()
        project = deps.repository.get_project(project_id)
        if not project:
            return deps.response_error("프로젝트를 찾을 수 없습니다.", 404, "PROJECT_NOT_FOUND")
        if str(project.get("owner") or "") != actor:
            return deps.response_error("프로젝트 소유자만 개발 트랙을 선택할 수 있습니다.", 403, "PROJECT_FORBIDDEN")
        payload = request.get_json(silent=True) or {}
        raw_track = str(payload.get("track") or "").strip()
        if not raw_track:
            return deps.response_error("프로젝트 개발 트랙을 선택해 주세요.", 400, "PROJECT_TRACK_REQUIRED")
        if raw_track not in TRACKS:
            return deps.response_error("프로젝트 트랙은 story 또는 video여야 합니다.", 400, "INVALID_PROJECT_TRACK")
        unavailable = _availability_error(deps, raw_track)
        if unavailable:
            return unavailable

        current = normalize_track(project.get("track"))
        if current and current != raw_track:
            return deps.response_error(
                "프로젝트 트랙은 만든 뒤 변경할 수 없습니다. 새 프로젝트를 만들어 주세요.",
                409,
                "PROJECT_TRACK_IMMUTABLE",
            )
        reused = current == raw_track
        if not current:
            try:
                selected = deps.repository.select_once(
                    project_id,
                    owner=str(project.get("owner") or actor),
                    track=raw_track,
                    work_title=str(project.get("workTitle") or ""),
                    selected_at=now_iso(),
                    selected_by=actor,
                )
            except graph_store.GraphError as error:
                return deps.response_error(str(error), 503, "PROJECT_TRACK_SAVE_FAILED", retryable=True)
            if not selected:
                selected = deps.repository.get_project(project_id)
                if not selected or normalize_track(selected.get("track")) != raw_track:
                    return deps.response_error(
                        "다른 요청이 프로젝트 트랙을 먼저 설정했습니다. 새로고침해 주세요.",
                        409,
                        "PROJECT_TRACK_IMMUTABLE",
                    )
                reused = True
            project = selected

        _command, command_reused, command_error = _append_track_command(deps, project, actor)
        if command_error:
            return command_error
        return jsonify({
            "ok": True,
            "item": deps.project_payload(project),
            "reused": reused or command_reused,
        })

    app.add_url_rule(
        "/api/projects/<project_id>/track",
        endpoint="project_track_select",
        view_func=deps.require_auth(select_track_handler),
        methods=["PUT"],
    )

    for rule in list(app.url_map.iter_rules()):
        if not _story_project_rule(rule.rule):
            continue
        endpoint = rule.endpoint
        current_view = app.view_functions.get(endpoint)
        if current_view and getattr(current_view, "_project_track_guarded", False):
            continue
        if current_view is None:
            raise ProjectTrackError(f"story track guard endpoint가 없습니다: {endpoint}")
        inner = getattr(current_view, "__wrapped__", None)
        if inner is None:
            raise ProjectTrackError(f"story track guard에 auth wrapper가 없습니다: {endpoint}")

        @wraps(inner)
        def story_guarded(*args, __inner=inner, **kwargs):
            project_id = str(kwargs.get("project_id") or "")
            project = deps.repository.get_project(project_id)
            if not project:
                return deps.response_error("프로젝트를 찾을 수 없습니다.", 404, "PROJECT_NOT_FOUND")
            allowed = deps.can_read_project(project) if request.method in ("GET", "HEAD") else deps.can_write_project(project)
            if not allowed:
                return deps.response_error("이 프로젝트에 접근할 권한이 없습니다.", 403, "PROJECT_FORBIDDEN")
            track = normalize_track(project.get("track"))
            if not track:
                return deps.response_error("프로젝트 개발 트랙을 먼저 선택해 주세요.", 409, "PROJECT_TRACK_REQUIRED")
            if track != TRACK_STORY:
                return deps.response_error(
                    "영상 개발 프로젝트에서는 스토리 개발 기능을 사용할 수 없습니다.",
                    403,
                    "PROJECT_TRACK_MISMATCH",
                )
            if not deps.story_enabled:
                return deps.response_error(
                    "이 환경에서는 스토리 개발 기능이 꺼져 있습니다.",
                    403,
                    "PROJECT_TRACK_DISABLED",
                )
            return __inner(*args, **kwargs)

        guarded_view = deps.require_auth(story_guarded)
        guarded_view._project_track_guarded = True
        app.view_functions[endpoint] = guarded_view
