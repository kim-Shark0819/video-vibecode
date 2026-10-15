"""vpoc1 BytePlus ModelArk 영상 경로 — ② 영상 모델 고르기(설계자 2026-10-07, dev 만).

video.py 의 Luma 경로와 같은 모양(submit → poll → 출력 mp4)을 돌려준다. routes 는 작업 식별자의
접두사(`ark/tasks/`)로 어느 경로인지 가른다.

보안(설계자 2026-10-07): BytePlus 쪽에 이미지를 남기지 않는다.
- 첫 프레임은 **AI 가 만든 캐릭터 이미지만**, URL 이 아니라 요청 본문의 base64 로 보낸다.
  업로드 원본은 routes 가 애초에 키프레임으로 고르지 않는다(source == "ai" 만).
- 결과 영상 URL 은 24시간 뒤 만료된다. 완료를 본 즉시 우리 영상 버킷으로 옮긴다.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request

from . import settings
from .errors import VpocError, now_iso
from .store import STORE, video_output_prefix

log = logging.getLogger("vpoc")

ARN_PREFIX = "ark/tasks/"
# 한 번에 만들기(A안)는 샷 문장을 모두 합치므로 샷 하나보다 길다. BytePlus 상한은 확인하지 못했다 —
# 거절되면 오류 원문이 ④ 에 보이고, 그때 이 값을 고친다.
MAX_PROMPT_CHARS = 4000

# 화면 표시 순서. Luma 는 video.py 가 맡고 여기서는 이름만 같이 둔다.
LABELS = {
    "seedance-2-0": "Seedance 2.0",
    "seedance-2-5": "Seedance 2.5",
}

# Luma(GetAsyncInvoke) 상태 이름으로 맞춘다 — routes 의 조회 분기를 그대로 쓰기 위해서다.
_STATUS = {
    "queued": "InProgress",
    "running": "InProgress",
    "succeeded": "Completed",
    "failed": "Failed",
    "cancelled": "Failed",
    "expired": "Failed",
}


def is_ark(model: str) -> bool:
    return model in settings.ARK_MODEL_IDS


def is_ark_job(invocation_arn: str) -> bool:
    return str(invocation_arn or "").startswith(ARN_PREFIX)


def model_id(model: str) -> str:
    return str(settings.ARK_MODEL_IDS.get(model) or "").strip()


def unavailable_reason(model: str) -> str:
    """고를 수 없으면 그 이유(화면에 그대로 보인다). 고를 수 있으면 빈 문자열."""
    if not model_id(model):
        return "모델 ID 미설정"
    if not settings.ARK_API_KEY:
        return "API 키 미설정"
    return ""


DEFAULT_MODEL = "luma-ray2"


def choices() -> list[dict]:
    """② 화면의 모델 목록. 고를 수 없는 모델도 이유와 함께 보인다."""
    luma_usd = settings.shot_usd(DEFAULT_MODEL)
    items = [
        {
            "id": DEFAULT_MODEL,
            "label": "Luma Ray2",
            "available": True,
            "reason": "",
            "perShotUsd": luma_usd,
            "priceKnown": True,
        }
    ]
    for key, label in LABELS.items():
        reason = unavailable_reason(key)
        items.append(
            {
                "id": key,
                "label": label,
                "available": not reason,
                "reason": reason,
                "perShotUsd": settings.shot_usd(key),
                "priceKnown": settings.ark_usd_per_sec(key) is not None,
            }
        )
    return items


def selected(state: dict) -> str:
    """이 계획이 쓸 모델. 고르기가 꺼져 있거나 고를 수 없는 값이면 Luma."""
    if not settings.MODEL_PICKER_ENABLED:
        return DEFAULT_MODEL
    model = str((state.get("steps", {}).get("2") or {}).get("videoModel") or DEFAULT_MODEL)
    if model == DEFAULT_MODEL or (is_ark(model) and not unavailable_reason(model)):
        return model
    return DEFAULT_MODEL


def video_options(model: str) -> dict:
    """③ 영상 설정에서 고를 수 있는 화질 · 샷 길이(초)."""
    low, high = settings.VIDEO_DURATION_RANGE.get(model, (settings.DURATION_S, settings.DURATION_S))
    return {
        "model": model,
        "resolutions": list(settings.VIDEO_RESOLUTIONS.get(model) or [settings.RESOLUTION]),
        "defaultResolution": settings.default_resolution(model),
        "minDurationS": low,
        "maxDurationS": high,
        "durationFixed": low == high,
        # Seedance 는 샷의 캐릭터 이미지를 참고 이미지로 함께 보낸다(캐릭터 그대로 유지).
        "referenceImages": is_ark(model),
    }


def reference_prompt(
    prompt: str, names: list[str | None], *, style_refs: int = 0, style_lock: bool = False
) -> str:
    """참고 이미지가 있을 때 Seedance 에 보낼 최종 프롬프트.

    설계자 2026-10-07: 사용자가 올린 이미지(있으면) 또는 ③에서 승인한 AI 이미지 속 캐릭터가
    **그대로** 나와야 한다. 이미지 순서대로 "Image N" 이 누구인지 밝히고, 얼굴·머리·의상·체형을
    바꾸지 말라고 앞에 못박는다. 장면 묘사(prompt)는 그 뒤에 그대로 붙인다.

    설계자 2026-10-08: 첨부 이미지의 **화풍**도 그대로. 캐릭터 이미지 뒤에 화풍 참고 이미지
    style_refs 장이 붙는다(인물이 아니라 그림체 기준). style_lock 이면 고정 화풍 문장 대신
    "참고 이미지와 같은 화풍"을 지시한다 — 호출부가 prompt 에서 고정 화풍 문장을 미리 뗀다.

    설계자 2026-10-13: names 는 이미지 한 장마다 하나다. 같은 인물이 이어서 여러 장이면
    "Images 1-2 are X" 로 묶고, None 은 화풍 참고 이미지다(style_refs 를 쓰지 않는 새 호출 방식).
    """
    names = list(names) + [None] * style_refs
    if not names and not style_lock:
        return prompt
    parts = []
    labels = []
    index = 1
    while index <= len(names):
        name = names[index - 1]
        end = index
        while end < len(names) and names[end] == name:
            end += 1
        span = f"Image {index}" if end == index else f"Images {index}-{end}"
        verb = "is" if end == index else "are"
        if name == PANEL_LABEL:
            what = "the webtoon panel" if end == index else "the webtoon panels"
            labels.append(
                f"{span} {verb} {what} this shot brings to life, in story order "
                "(follow their setting, art style and events)"
            )
        elif name is None:
            labels.append(f"{span} {verb} an art-style reference only (not a character)")
        elif end == index:
            labels.append(f"{span} is {name}")
        else:
            labels.append(f"{span} are all {name} (the same person from different views)")
        index = end + 1
    if labels:
        parts.append("; ".join(labels) + ".")
    if any(name is not None for name in names):
        parts.append(
            "Keep every character exactly as shown in their reference images: the same face shape, eyes "
            "and eye shape, eyebrows, nose, mouth, skin tone, hairstyle, hair color, outfit, accessories "
            "and body type, from the first frame to the last. Copy the faces from the close-up references. "
            "Do not redesign, beautify, restyle or swap any character."
        )
    if style_lock:
        parts.append(
            "Render the whole video in exactly the same art style as the reference images: the same line "
            "work, coloring, shading, color palette, rendering and level of detail, in every frame. Do not "
            "switch to a different style (no photorealism if the references are drawn, no cartoon look if "
            "they are photographs)."
        )
    parts.append(f"Scene: {prompt}")
    return " ".join(parts)


ONE_TAKE_ID = "take"
# reference_prompt 의 names 에서 "이 샷이 맡은 웹툰 장면" 이미지를 뜻하는 표식.
PANEL_LABEL = "#panel"


def strip_style(prompt: str, style_sentence: str) -> str:
    """샷 문장 맨 앞의 화풍 문장(모든 샷에 같은 문장)을 뗀다. 한 번에 만들기에서는 앞에 한 번만 둔다."""
    text = (prompt or "").strip()
    head = (style_sentence or "").strip().rstrip(".")
    if head and text.startswith(head):
        text = text[len(head):].lstrip(". ").strip()
    return text


def one_take_prompt(beats: list[tuple[int, str]], style_sentence: str = "") -> str:
    """A안(설계자 2026-10-08): ③ 의 샷들을 시간대별 지시 하나로 합쳐 Seedance 한 번에 이어서 만든다.

    beats = [(초, 샷 장면 문장)] (③ 순서). 캐릭터 유지 지시는 reference_prompt() 가 앞에 붙인다.
    """
    parts = []
    if style_sentence:
        parts.append(style_sentence.strip().rstrip(".") + ".")
    parts.append(
        "One continuous video in a single take. Move smoothly from each beat to the next with no jump "
        "cuts, keeping the same location, lighting, time of day, wardrobe and characters throughout."
    )
    start = 0
    for index, (seconds, text) in enumerate(beats, start=1):
        end = start + int(seconds)
        parts.append(f"Beat {index} ({start}-{end}s): {text.strip()}")
        start = end
    return " ".join(parts)


def _call(method: str, path: str, payload: dict | None = None) -> dict:
    request = urllib.request.Request(
        settings.ARK_BASE_URL + path,
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        method=method,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + settings.ARK_API_KEY,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            detail = (json.loads(raw).get("error") or {}).get("message") or raw
        except ValueError:
            detail = raw
        raise VpocError("ark." + method.lower(), f"HTTP{exc.code}", str(detail)[:500], 502) from exc


def submit(
    *,
    model: str,
    owner: str,
    project_id: str,
    plan_id: str,
    shot_id: str,
    attempt: int,
    prompt: str,
    keyframe_jpeg_b64: str = "",
    gen_id: str = "",
    reference_jpegs_b64: list[str] | None = None,
    resolution: str = "",
    duration_s: int | None = None,
    last_frame_jpeg_b64: str = "",
) -> dict:
    """작업을 만들고 즉시 돌아온다. video.submit 과 같은 키를 돌려준다.

    reference_jpegs_b64: 샷에 나오는 캐릭터 이미지(사용자 업로드 또는 ③에서 승인한 AI 이미지).
    `prompt` 는 reference_prompt() 로 "Image N = 누구" 지시를 붙인 최종 문장이어야 한다 — 순서가
    이미지 순서와 같아야 BytePlus 가 누가 누구인지 안다.
    """
    reason = unavailable_reason(model)
    if reason:
        raise VpocError("ark.submit", "ModelUnavailable", f"{LABELS.get(model, model)}: {reason}", 400)
    if not prompt.strip():
        raise VpocError("ark.submit", "EmptyPrompt", "프롬프트가 비어 있습니다.", 400)
    if len(prompt) > MAX_PROMPT_CHARS:
        # 자르지 않는다(R4).
        raise VpocError(
            "ark.submit",
            "PromptOverBudget",
            f"프롬프트가 {MAX_PROMPT_CHARS}자를 넘습니다 ({len(prompt)}자). 자르지 않고 제출을 멈췄습니다.",
            400,
        )

    content: list[dict] = [{"type": "text", "text": prompt}]
    # 캐릭터 참고 이미지. URL 이 아니라 본문 base64 로 보낸다(BytePlus 쪽에 이미지를 남기지 않는다).
    for b64 in reference_jpegs_b64 or []:
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": "data:image/jpeg;base64," + b64},
                "role": "reference_image",
            }
        )
    if keyframe_jpeg_b64 and not reference_jpegs_b64:
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": "data:image/jpeg;base64," + keyframe_jpeg_b64},
                "role": "first_frame",
            }
        )
        # 키 장면 방식: 끝 장면까지 주면 Seedance 가 그 사이를 움직임으로 채운다(참고 이미지와 함께 못 쓴다).
        if last_frame_jpeg_b64:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/jpeg;base64," + last_frame_jpeg_b64},
                    "role": "last_frame",
                }
            )
    body = {
        "model": model_id(model),
        "content": content,
        "ratio": settings.ASPECT,
        "duration": int(duration_s or settings.DURATION_S),
        "resolution": resolution or settings.ARK_RESOLUTION,
        "generate_audio": bool(settings.ARK_GENERATE_AUDIO),
        "watermark": False,
    }
    started = time.time()
    result = _call("POST", "/contents/generations/tasks", body)
    task_id = str(result.get("id") or "")
    log.info(
        "vpoc.call where=ark.submit model=%s plan=%s shot=%s ms=%d ok=%s",
        model,
        plan_id,
        shot_id,
        int((time.time() - started) * 1000),
        bool(task_id),
    )
    if not task_id:
        raise VpocError("ark.submit", "NoTaskId", f"작업 ID 가 없습니다: {str(result)[:300]}", 502)
    prefix = video_output_prefix(owner, project_id, plan_id, shot_id, attempt, gen_id)
    return {
        "provider": model,
        "invocationArn": ARN_PREFIX + task_id,
        # 원장 키. Luma 토큰과 겹치지 않게 작업 ID 를 그대로 쓴다(작업 하나당 1회 계상).
        "clientRequestToken": "ark-" + task_id,
        "outputPrefix": prefix,
        "submittedAt": now_iso(),
        "promptChars": len(prompt),
    }


def poll(invocation_arn: str, output_prefix: str) -> dict:
    """작업 상태. 완료면 결과 영상을 우리 버킷 `{prefix}/{taskId}/output.mp4` 로 옮긴 뒤 Completed."""
    task_id = invocation_arn[len(ARN_PREFIX):]
    result = _call("GET", f"/contents/generations/tasks/{task_id}")
    raw_status = str(result.get("status") or "")
    status = _STATUS.get(raw_status, "InProgress")
    message = ""
    if status == "Completed":
        url = str((result.get("content") or {}).get("video_url") or "")
        if not url:
            status, message = "Failed", "완료됐지만 영상 URL 이 없습니다."
        else:
            key = f"{output_prefix.rstrip('/')}/{task_id}/output.mp4"
            if not STORE.exists(key, bucket=settings.VIDEO_BUCKET):
                started = time.time()
                with urllib.request.urlopen(url, timeout=300) as response:
                    STORE.put_bytes(key, response.read(), "video/mp4", bucket=settings.VIDEO_BUCKET)
                log.info("vpoc.call where=ark.copy task=%s ms=%d", task_id, int((time.time() - started) * 1000))
    elif status == "Failed":
        error = result.get("error") or {}
        message = str(error.get("message") or error.get("code") or raw_status)
    return {
        "status": status,
        "failureMessage": message,
        "usage": result.get("usage"),
    }
