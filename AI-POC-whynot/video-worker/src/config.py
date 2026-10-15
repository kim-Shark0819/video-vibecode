"""워커 설정. 전부 환경변수로 받는다.

## 왜 값을 코드에 두지 않는가

가중치 경로·큐 URL·버킷은 환경마다 다르고, 인프라(terraform/hub/40-video-gpu)가
만들어 내보낸 값이다. 코드에 박으면 dev/prod 가 갈릴 때 한쪽만 고쳐진다.

## WEIGHTS_DIR 을 추상화한 이유

가중치를 EBS PVC / 인스턴스 스토어 / S3 중 어디에 둘지는 P2 콜드 스타트 실측 뒤에
정하기로 했다(계획서 G-07). 워커는 **어디서 왔는지 모른다.** 경로만 안다.
"""

from __future__ import annotations

import os


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


# --- 가중치 ---
WEIGHTS_DIR = os.getenv("WEIGHTS_DIR", "/weights")
MODEL_DIR_NAME = os.getenv("WAN_MODEL_DIR", "Wan2.2-I2V-A14B")
CKPT_DIR = os.path.join(WEIGHTS_DIR, MODEL_DIR_NAME)

# Wan 태스크 이름. 공식 저장소의 generate.py 가 받는 값과 같아야 한다.
WAN_TASK = os.getenv("WAN_TASK", "i2v-A14B")

# --- 큐 ---
CLIP_QUEUE_URL = os.getenv("QUEUE_CLIP_URL", "")
# long polling. 빈 응답으로 API 호출을 태우지 않는다.
POLL_WAIT_SECONDS = _int("QUEUE_POLL_WAIT_SECONDS", 20)
# 처리 중 가시성 연장 주기. 잡이 이 값보다 오래 걸리면 메시지가 큐로 돌아가
# 같은 잡이 두 번 돈다.
VISIBILITY_EXTEND_SECONDS = _int("QUEUE_VISIBILITY_EXTEND_SECONDS", 300)

# --- 저장소 ---
S3_BUCKET = os.getenv("S3_BUCKET", "")
AWS_REGION = os.getenv("AWS_REGION", "ap-northeast-2")

# --- 생성 기본값 ---
# 원본 시스템의 ConfigMap 값과 같게 두었다. 화면에서 덮어쓸 수 있다.
DEFAULT_SIZE = os.getenv("WAN_DEFAULT_SIZE", "832*480")
DEFAULT_STEPS = _int("WAN_DEFAULT_STEPS", 40)
DEFAULT_FRAMES = _int("WAN_DEFAULT_FRAMES", 81)
# 단일 GPU 로 간다. 4-GPU FSDP 경로는 되살리지 않는다(계획서 G-03).
GPUS = _int("WAN_GPUS", 1)
JOB_TIMEOUT_SECONDS = _int("WAN_JOB_TIMEOUT_SECONDS", 5400)

# --- 계측 ---
INSTANCE_TYPE = os.getenv("GPU_INSTANCE_TYPE", "g6e.xlarge")
# 서울 온디맨드 실측 단가(2026-09-09). 단가를 코드에 박지 않는 것이 원칙이지만
# 이것은 **계측용 참고값**이고 청구서가 아니다. 값이 틀리면 manifest 의 costUsd 만
# 어긋나고 동작에는 영향이 없다. AWS 가격이 바뀌면 환경변수로 덮는다.
INSTANCE_HOURLY_USD = _float("GPU_HOURLY_USD", 2.288)

OUTPUT_DIR = os.getenv("OUTPUT_DIR", "/out")


def missing() -> list[str]:
    """없으면 기동할 수 없는 값. 기동 시점에 확인해 바로 알린다.

    런타임에 처음 쓰일 때 알면 GPU 노드가 이미 켜져 있고 큐 메시지도 소비된 뒤다.
    """
    problems = []
    if not CLIP_QUEUE_URL:
        problems.append("QUEUE_CLIP_URL")
    if not S3_BUCKET:
        problems.append("S3_BUCKET")
    if not os.path.isdir(CKPT_DIR):
        problems.append(f"가중치 디렉터리 없음: {CKPT_DIR}")
    else:
        marker = os.path.join(CKPT_DIR, ".download-complete")
        if not os.path.isfile(marker):
            problems.append(f"가중치 다운로드 완료 마커 없음: {marker}")
    return problems
