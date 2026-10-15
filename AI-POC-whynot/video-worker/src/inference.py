"""Wan 2.2 I2V 추론. 상주 모델을 감싼다.

## 단일 GPU (계획서 G-03 · R-08.7)

4-GPU FSDP / Ulysses 병렬 경로는 되살리지 않는다. F-05 실험에서 CPU T5 인코딩
~900초 하한선, FSDP 샤드 미해제로 인한 CUDA OOM, rank-0 broadcast 의 NCCL watchdog
타임아웃(~904초)으로 세 접근 모두 실패했다. 단일 GPU 상주 워커로 간다.

## L40S 44GB — 48GB 가 아니다

계획서가 참고한 원본 사양은 48GB 였다. 실측은 44GB 다. bf16 로 안 들어가면
fp8 양자화가 필요하고 그것은 P3 다. 이 모듈은 bf16 을 먼저 시도하고 실패를
숨기지 않는다 — P2 게이트가 "적재 성공/실패를 숫자로 기록"하라고 요구한다.

## FLF2V 라우팅 (요구사항 R-15)

`keyframe_last` 가 있으면 FLF2V, 없으면 I2V. 시작과 끝이 다른 샷(변신·전환)은
I2V 로 원리상 만들 수 없다.
"""

from __future__ import annotations

import logging
import os
import sys

from . import config, vram

logger = logging.getLogger("video-worker.inference")


class LoadError(RuntimeError):
    """모델 적재 실패. P2 게이트가 요구하는 '실패도 숫자로 기록'의 대상이다."""


class WanEngine:
    """Wan 2.2 I2V 모델을 한 번 로드해 여러 잡에 재사용한다.

    서브프로세스로 매번 새로 띄우지 않는다(계획서 G-12) — 로드 시간이 생성 시간을
    넘는 것을 막기 위해서다. 로드는 워커 기동 시 1회.
    """

    def __init__(self):
        self._pipeline = None
        self._task = config.WAN_TASK

    def load(self, manifest: vram.Manifest) -> None:
        """모델을 GPU 에 올린다. 실패하면 LoadError — 숨기지 않는다."""
        with manifest.stage("load_model"):
            self._pipeline = self._load_pipeline()

    def _load_pipeline(self):
        """Wan2.2 공식 추론 코드를 호출한다.

        공식 저장소(github.com/Wan-Video/Wan2.2, Apache 2.0)의 `wan` 패키지가
        Dockerfile 에서 /app/Wan2.2 로 클론되어 PYTHONPATH 에 있다. 정확한 클래스명은
        저장소 버전에 따라 다를 수 있어, 표준 진입점을 순서대로 시도하고 전부
        실패하면 이유를 그대로 올린다 — 추측한 이름으로 조용히 넘어가지 않는다.
        """
        import torch  # noqa: PLC0415

        if not torch.cuda.is_available():
            raise LoadError("CUDA GPU 를 찾지 못했습니다. nvidia-smi 와 device plugin 을 확인하세요.")

        gpu_name = torch.cuda.get_device_name(0)
        total_vram = torch.cuda.get_device_properties(0).total_memory / 1024 / 1024 / 1024
        logger.info("GPU %s, VRAM %.1fGB, task=%s", gpu_name, total_vram, self._task)

        if not os.path.isdir(config.CKPT_DIR):
            raise LoadError(f"가중치 디렉터리가 없습니다: {config.CKPT_DIR}")

        sys.path.insert(0, "/app/Wan2.2")
        try:
            import wan  # noqa: PLC0415
            from wan.configs import WAN_CONFIGS  # noqa: PLC0415
        except ImportError as error:
            raise LoadError(
                f"Wan2.2 패키지를 임포트하지 못했습니다: {error}. "
                "Dockerfile 의 git clone 과 requirements 설치를 확인하세요."
            ) from error

        if self._task not in WAN_CONFIGS:
            raise LoadError(
                f"'{self._task}' 는 이 Wan2.2 버전이 지원하는 태스크가 아닙니다. "
                f"가능한 값: {sorted(WAN_CONFIGS.keys())}"
            )

        try:
            pipeline = wan.WanI2V(
                config=WAN_CONFIGS[self._task],
                checkpoint_dir=config.CKPT_DIR,
                device_id=0,
                rank=0,
                # 단일 GPU. FSDP·Ulysses 를 켜지 않는다(계획서 G-03).
                t5_fsdp=False,
                dit_fsdp=False,
                use_usp=False,
            )
        except Exception as error:  # noqa: BLE001 - 적재 실패 원인을 그대로 올린다
            raise LoadError(f"WanI2V 적재 실패: {type(error).__name__}: {error}") from error

        logger.info("Wan2.2 I2V 적재 완료 task=%s ckpt=%s", self._task, config.CKPT_DIR)
        return pipeline

    def generate(self, shot: dict, keyframe_path: str, manifest: vram.Manifest) -> str:
        """샷 하나를 클립으로 만든다. mp4 경로를 돌려준다.

        Args:
            shot: {"no", "seconds", "keyframe_last_path"(선택), ...} — shot_planner 산출물
            keyframe_path: 첫 프레임 로컬 경로 (이미 다운로드된 상태)
        """
        if self._pipeline is None:
            raise LoadError("모델이 로드되지 않았습니다. load() 를 먼저 호출하세요.")

        from PIL import Image  # noqa: PLC0415

        with manifest.stage("load_keyframe"):
            image = Image.open(keyframe_path).convert("RGB")

        mode = "flf2v" if shot.get("keyframe_last_path") else "i2v"
        frames = _frames_for_seconds(shot.get("seconds") or 4)

        with manifest.stage("diffusion"):
            kwargs = {
                "input_prompt": shot.get("keyframe_prompt") or "",
                "img": image,
                "size": _parse_size(config.DEFAULT_SIZE),
                "frame_num": frames,
                "sampling_steps": config.DEFAULT_STEPS,
                "seed": -1,
            }
            if mode == "flf2v":
                # 끝 프레임이 있으면 시작·끝을 모두 준다. 공식 API 가 last_frame 인자를
                # 지원하지 않는 빌드라면 여기서 TypeError 가 나고 그대로 올라간다 —
                # 조용히 I2V 로 강등하지 않는다. 강등하면 "왜 변신 샷이 안 이어지는지"가
                # 사용자에게 원인 없이 보인다.
                last_path = shot["keyframe_last_path"]
                kwargs["last_frame"] = Image.open(last_path).convert("RGB")

            try:
                video_frames = self._pipeline.generate(**kwargs)
            except TypeError as error:
                if mode == "flf2v":
                    raise LoadError(
                        f"이 Wan2.2 빌드는 FLF2V(last_frame) 를 지원하지 않습니다: {error}. "
                        "샷에서 keyframe_last_prompt 를 빼고 I2V 로 다시 시도하세요."
                    ) from error
                raise

        with manifest.stage("vae_decode_and_write"):
            out_path = os.path.join(config.OUTPUT_DIR, f"shot-{shot.get('no', 0)}.mp4")
            _write_video(video_frames, out_path, fps=_FPS)

        vram.release()
        return out_path


_FPS = 16  # Wan 2.2 기본 프레임레이트


def _frames_for_seconds(seconds: float) -> int:
    """Wan 은 4n+1 프레임을 요구한다(diffusers 계열 VAE 시간축 제약과 동일한 이유)."""
    raw = max(1, round(seconds * _FPS))
    return raw - ((raw - 1) % 4)


def _parse_size(size_str: str) -> tuple[int, int]:
    parts = (size_str or config.DEFAULT_SIZE).replace("x", "*").split("*")
    return int(parts[0]), int(parts[1])


def _write_video(frames, path: str, fps: int) -> None:
    """생성된 프레임을 mp4 로 쓴다. Wan 결과 타입에 따라 두 경로를 시도한다."""
    if hasattr(frames, "shape"):
        _write_via_ffmpeg_pipe(frames, path, fps)
        return
    raise LoadError(f"알 수 없는 프레임 출력 타입입니다: {type(frames)}")


def _write_via_ffmpeg_pipe(tensor, path: str, fps: int) -> None:
    import subprocess  # noqa: PLC0415

    import numpy as np  # noqa: PLC0415
    import torch  # noqa: PLC0415

    if isinstance(tensor, torch.Tensor):
        array = tensor.detach().to("cpu")
        # Wan 출력은 보통 [-1, 1] 범위의 (C, T, H, W) 다. 0~255 uint8 로 정규화한다.
        array = ((array.clamp(-1, 1) + 1) / 2 * 255).to(torch.uint8)
        array = array.permute(1, 2, 3, 0).numpy() if array.ndim == 4 else array.numpy()
    else:
        array = np.asarray(tensor)

    height, width = array.shape[1], array.shape[2]
    cmd = [
        "ffmpeg", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
        "-s", f"{width}x{height}", "-r", str(fps), "-i", "-",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        path,
    ]
    proc = subprocess.run(cmd, input=array.tobytes(), capture_output=True)
    if proc.returncode != 0:
        raise LoadError(f"ffmpeg 인코딩 실패: {proc.stderr.decode('utf-8', 'replace')[:400]}")
