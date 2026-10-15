# Wan 2.2 I2V 추론 워커.
#
# ## 가중치를 이미지에 굽지 않는다
#
# Wan 2.2 I2V-A14B 가중치는 **실측 118GB** 다(계획서의 68GB 추정과 다르다).
# 이미지에 넣으면 배스천에서 빌드할 수 없고 ECR pull 시간도 감당이 안 된다.
# 가중치는 EBS PVC 에 두고 `WEIGHTS_DIR` 로 경로만 받는다.
#
# 그래서 이 이미지는 5GB 수준으로 유지된다. 배스천(100GB, 97GB 여유)에서 빌드된다.
#
# ## base 이미지
#
# L40S 는 compute capability sm_89 다. CUDA 12.4 이상 + PyTorch 2.4 이상이 필요하다.
# devel 이 아니라 runtime 을 쓰는 이유: flash-attn 같은 컴파일 확장을 아직 넣지 않는다
# (계획서 P3 항목). runtime 이 1.5GB 정도 작다.
FROM pytorch/pytorch:2.6.0-cuda12.4-cudnn9-runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    # 가중치 경로. 실제 값은 k8s 매니페스트가 준다.
    WEIGHTS_DIR=/weights

# ffmpeg: 클립을 mp4 로 쓰고 컷 편집에도 쓴다.
# git: Wan2.2 저장소를 클론해 추론 코드를 가져온다 (Apache 2.0).
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Wan 2.2 공식 추론 코드. 라이선스 Apache 2.0 (github.com/Wan-Video/Wan2.2 LICENSE.txt).
#
# ## 커밋을 고정한다
#
# 이전에는 ARG 기본값이 `main` 이었다. 주석은 "커밋을 박는다"고 적혀 있었지만 실제로는
# 브랜치를 따라가서 **빌드 시점마다 의존성 목록이 달라졌다.** 업스트림
# requirements.txt 가 실제 임포트와 어긋나는 상태라, 이 흔들림이 "빌드할 때마다
# 다른 모듈이 없다"는 증상으로 나타난다. v3 -> v4 -> v5 가 그렇게 소모됐다.
#
# ## clone 방식을 바꾼 이유
#
# `git clone --depth 1 --branch <ref>` 는 브랜치·태그만 받고 **커밋 SHA 는 거부한다.**
# 그래서 init + fetch --depth 1 <sha> + checkout FETCH_HEAD 로 바꿨다. 얕은 클론을
# 유지하면서 커밋을 정확히 고정한다.
ARG WAN_REPO=https://github.com/Wan-Video/Wan2.2.git
ARG WAN_REF=42bf4cfaa384bc21833865abc2f9e6c0e67233dc
RUN mkdir -p /app/Wan2.2 \
 && cd /app/Wan2.2 \
 && git init -q . \
 && git remote add origin "${WAN_REPO}" \
 && git fetch -q --depth 1 origin "${WAN_REF}" \
 && git checkout -q FETCH_HEAD \
 && echo "${WAN_REF}" > /app/Wan2.2/.wan-ref \
 && rm -rf /app/Wan2.2/.git

# Wan 의 requirements 를 먼저 넣고 우리 것을 덧붙인다. 순서를 바꾸면 우리가 고정한
# 버전이 Wan 쪽 해석에 덮인다.
#
# flash-attn 은 뺀다. 이 패키지는 소스에서 컴파일되고 nvcc(CUDA 컴파일러)가 필요한데
# 이 이미지는 devel 이 아니라 runtime 이라 nvcc 가 없다. 실측: "CUDA_HOME environment
# variable is not set" 로 빌드가 즉시 죽었다. flash-attn / SageAttention 도입은
# 계획서 P3(최적화) 항목이고, 그때 devel 이미지 + 빌드 시간을 감안해 별도로 붙인다.
# P2 는 bf16 기본 어텐션으로도 적재·생성이 되는지만 확인하면 된다.
#
# ## 업스트림 requirements.txt 에 빠진 패키지를 보충한다
#
# Wan2.2 의 requirements.txt 는 실제 임포트 목록과 어긋난다(저장소 쪽 선언 누락).
# 실측으로 확인한 누락은 네 개다.
#
#   einops   wan/modules/vae2_1.py · vae2_2.py · modules/animate/* · modules/s2v/*
#   decord   비디오 디코딩. wan.configs 임포트 경로에서 끌려온다
#   librosa  오디오 처리(s2v 경로). configs 가 태스크 전체를 등록하며 함께 임포트된다
#   peft     LoRA 어댑터. 같은 경로에서 임포트된다
#
# ## 왜 한 번에 넣는가
#
# 이 목록을 하나씩 찾으면 이미지 재빌드(7GB)와 push 를 모듈마다 반복하게 된다.
# 실제로 v3 -> v4 -> v5 가 그렇게 소모됐다. `import wan; from wan.configs import
# WAN_CONFIGS` 를 GPU 파드 안에서 돌려 누락을 한 번에 전수 열거했다.
#
# ## 배스천에서는 이 누락이 보이지 않는다
#
# `wan.configs` 는 임포트 도중 CUDA 를 건드린다. GPU 가 없는 호스트에서는
# "Found no NVIDIA driver" RuntimeError 로 **decord 임포트에 도달하기 전에** 죽는다.
# 즉 GPU 없는 곳에서 검증하면 통과한 것으로 오판한다. 반드시 GPU 노드에서 확인한다.
#
# flash-attn 은 뺀다. 이 패키지는 소스에서 컴파일되고 nvcc(CUDA 컴파일러)가 필요한데
# 이 이미지는 devel 이 아니라 runtime 이라 nvcc 가 없다. 실측: "CUDA_HOME environment
# variable is not set" 로 빌드가 즉시 죽었다. flash-attn / SageAttention 도입은
# 계획서 P3(최적화) 항목이고, 그때 devel 이미지 + 빌드 시간을 감안해 별도로 붙인다.
# P2 는 bf16 기본 어텐션으로도 적재·생성이 되는지만 확인하면 된다.
RUN grep -v -i '^flash.attn' /app/Wan2.2/requirements.txt > /tmp/wan-requirements.txt \
 && python -m pip install --no-cache-dir -r /tmp/wan-requirements.txt \
      einops decord librosa peft \
 && rm -f /tmp/wan-requirements.txt \
 && rm -rf /root/.cache/pip

COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir -r /app/requirements.txt

COPY src /app/src

# 루트로 돌리지 않는다. 가중치는 readOnly 로 마운트하고 출력은 emptyDir 에 쓴다.
#
# USER 는 숫자 UID 로 지정한다. k8s 매니페스트가 securityContext.runAsNonRoot: true 를
# 쓰는데, kubelet 은 이미지 메타데이터의 USER 가 사용자 이름이면 그것이 root(0) 인지
# 판단할 수 없어 CreateContainerConfigError 로 막는다(실측:
# "container has runAsNonRoot and image has non-numeric user (worker)").
RUN useradd --create-home --uid 10001 worker \
 && mkdir -p /out \
 && chown -R worker:worker /app /out
USER 10001

ENV PYTHONPATH=/app:/app/Wan2.2

# 상주 워커. 큐를 폴링하고 모델을 메모리에 유지한다.
CMD ["python", "-m", "src.worker"]
