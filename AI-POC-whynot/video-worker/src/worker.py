"""상주 GPU 워커. SQS 를 폴링하고 모델을 메모리에 유지한다.

## 왜 상주인가 (계획서 G-12)

매 잡마다 서브프로세스를 띄우고 모델을 로드하면 로드 시간이 생성 시간을 넘는다.
이 프로세스는 기동 시 모델을 한 번 로드하고 죽을 때까지 유지한다.

## 실패 시 메시지를 지우지 않는다

가시성 타임아웃이 지나면 메시지가 큐로 돌아가 다른(또는 같은) 워커가 재처리한다.
파드가 강제 종료돼도 진행 중이던 잡이 사라지지 않는다 — 계획서 G-10 의 검증 기준이
"워커 파드를 강제 종료해도 메시지가 큐로 돌아와 재처리된다"이다.

## S3 경로

`users/{u}/video-jobs/{jobId}/clips/{shotNo}.mp4` 로 쓴다(요구사항 R-22).
키프레임은 같은 리전(서울) 버킷에서 읽는다 — us-west-2 Bedrock 호출은 앱 쪽이 하고
결과 이미지는 서울에 저장하기로 했다(design.md §2).
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import tempfile
import threading
import time

import boto3

from . import config, inference, vram

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("video-worker")

_shutdown = threading.Event()


def _handle_signal(signum, _frame):
    logger.info("종료 신호 %s 수신. 진행 중인 잡을 마치고 멈춥니다.", signum)
    _shutdown.set()


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


class VisibilityKeeper:
    """긴 처리 동안 가시성 타임아웃을 주기적으로 늘린다.

    확산 단계가 5분을 넘으면 기본 가시성 타임아웃(큐 설정값)이 지나 메시지가
    다른 워커에게 다시 보인다. 같은 잡이 두 번 GPU 를 태우는 사고를 막는다.
    """

    def __init__(self, sqs, queue_url: str, receipt_handle: str, extend_seconds: int):
        self._sqs = sqs
        self._queue_url = queue_url
        self._receipt_handle = receipt_handle
        self._extend_seconds = extend_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *_exc):
        self._stop.set()
        self._thread.join(timeout=5)

    def _run(self):
        while not self._stop.wait(self._extend_seconds * 0.6):
            try:
                self._sqs.change_message_visibility(
                    QueueUrl=self._queue_url,
                    ReceiptHandle=self._receipt_handle,
                    VisibilityTimeout=self._extend_seconds,
                )
            except Exception as error:  # noqa: BLE001 - 연장 실패가 처리를 막아선 안 된다
                logger.warning("가시성 연장 실패: %s", error)


def process_message(engine: inference.WanEngine, sqs, s3, body: dict) -> dict:
    """메시지 하나 = 샷 하나. manifest 를 돌려준다."""
    job_id = str(body.get("jobId") or "")
    shot = body.get("shot") or {}
    owner = str(body.get("owner") or "")
    keyframe_key = str(shot.get("keyframeKey") or "")

    manifest = vram.Manifest(
        job_id=job_id,
        shot_id=f"shot-{shot.get('no', 0)}",
        instance_type=config.INSTANCE_TYPE,
        hourly_usd=config.INSTANCE_HOURLY_USD,
    )

    with tempfile.TemporaryDirectory() as tmp:
        keyframe_path = os.path.join(tmp, "keyframe.png")
        with manifest.stage("download_keyframe"):
            s3.download_file(config.S3_BUCKET, keyframe_key, keyframe_path)

        last_path = ""
        if shot.get("keyframeLastKey"):
            last_path = os.path.join(tmp, "keyframe_last.png")
            s3.download_file(config.S3_BUCKET, str(shot["keyframeLastKey"]), last_path)

        clip_path = engine.generate(
            {**shot, "keyframe_last_path": last_path or None},
            keyframe_path,
            manifest,
        )

        clip_key = f"users/{owner}/video-jobs/{job_id}/clips/{shot.get('no', 0)}.mp4"
        with manifest.stage("upload_clip"):
            s3.upload_file(clip_path, config.S3_BUCKET, clip_key)

    result = manifest.to_dict()
    result["clipKey"] = clip_key
    return result


def main() -> int:
    problems = config.missing()
    if problems:
        # 기동 시점에 바로 죽는다. GPU 가 켜진 채 알 수 없는 이유로 아무 일도 안 하는
        # 상태보다, 로그에 이유가 남고 CrashLoopBackOff 로 눈에 띄는 것이 낫다.
        logger.error("필수 설정이 없어 기동할 수 없습니다: %s", problems)
        return 1

    logger.info(
        "워커 기동 task=%s ckpt=%s queue=%s bucket=%s",
        config.WAN_TASK, config.CKPT_DIR, config.CLIP_QUEUE_URL, config.S3_BUCKET,
    )

    sqs = boto3.client("sqs", region_name=config.AWS_REGION)
    s3 = boto3.client("s3", region_name=config.AWS_REGION)

    engine = inference.WanEngine()
    load_manifest = vram.Manifest("startup", "-", config.INSTANCE_TYPE, config.INSTANCE_HOURLY_USD)
    try:
        engine.load(load_manifest)
    except inference.LoadError as error:
        logger.error("모델 적재 실패: %s", error)
        logger.error("계측: %s", json.dumps(load_manifest.to_dict(), ensure_ascii=False))
        return 1
    logger.info("모델 적재 완료: %s", json.dumps(load_manifest.to_dict(), ensure_ascii=False))

    # startupProbe 가 이 파일의 존재로 준비 여부를 판단한다(k8s-gpu/video-worker.yaml.tmpl).
    # HTTP 서버가 아니라 SQS 폴링 루프라 exec 프로브를 쓴다.
    with open("/tmp/ready", "w", encoding="utf-8") as handle:
        handle.write(str(int(time.time())))

    processed = 0
    while not _shutdown.is_set():
        received = sqs.receive_message(
            QueueUrl=config.CLIP_QUEUE_URL,
            MaxNumberOfMessages=1,
            WaitTimeSeconds=config.POLL_WAIT_SECONDS,
        )
        messages = received.get("Messages") or []
        if not messages:
            continue

        message = messages[0]
        receipt = message["ReceiptHandle"]
        try:
            body = json.loads(message["Body"])
        except (json.JSONDecodeError, KeyError) as error:
            logger.error("메시지 파싱 실패, 삭제하고 넘어갑니다: %s", error)
            sqs.delete_message(QueueUrl=config.CLIP_QUEUE_URL, ReceiptHandle=receipt)
            continue

        job_id = body.get("jobId")
        shot_no = (body.get("shot") or {}).get("no")
        logger.info("잡 수신 jobId=%s shot=%s", job_id, shot_no)

        try:
            with VisibilityKeeper(sqs, config.CLIP_QUEUE_URL, receipt,
                                  config.VISIBILITY_EXTEND_SECONDS):
                manifest = process_message(engine, sqs, s3, body)
            logger.info("잡 완료 jobId=%s shot=%s %s",
                       job_id, shot_no, json.dumps(manifest, ensure_ascii=False))
            # 성공한 뒤에만 지운다. 실패하면 가시성 타임아웃 후 재처리된다.
            sqs.delete_message(QueueUrl=config.CLIP_QUEUE_URL, ReceiptHandle=receipt)
            processed += 1
        except Exception as error:  # noqa: BLE001 - 한 잡의 실패가 워커 전체를 죽여선 안 된다
            logger.error("잡 처리 실패 jobId=%s shot=%s: %s", job_id, shot_no, error, exc_info=True)
            # 메시지를 지우지 않는다. 3회 실패하면 큐 정책이 DLQ 로 보낸다.

        vram.release()

    logger.info("종료합니다. 처리한 잡 수=%s", processed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
