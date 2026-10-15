variable "account_id" {
  type        = string
  description = "이 구성이 적용될 AWS 계정. 다르면 실행을 중단한다."

  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "account_id: 12자리 숫자여야 합니다."
  }
}

variable "region" {
  type        = string
  description = "리전"
  default     = "ap-northeast-2"
}

variable "bedrock_image_region" {
  type        = string
  description = <<-EOT
    Bedrock 이미지 모델(키프레임 생성) 호출 리전.

    실측 2026-09-09: ap-northeast-2 의 IMAGE 출력 모델은 0개다.
    us-west-2 에서 stability.sd3-5-large-v1:0 / stability.stable-image-core-v1:1
    호출이 확인됐다. us-east-1 의 amazon.nova-canvas-v1:0 은 접근 거부다.
    서울에 IMAGE 모델이 들어오면 이 값만 바꾼다.
  EOT
  default     = "us-west-2"
}

variable "aws_profile" {
  type        = string
  description = "AWS CLI named profile. 기본 프로필은 다른 계정을 가리키므로 반드시 지정한다."
  default     = "ai-poc-hub"
}

variable "project_name" {
  type        = string
  description = "리소스 이름 접두어."
  default     = "ai-poc"
}

variable "environment" {
  type        = string
  description = <<-EOT
    dev 또는 prd. 클러스터 선택과 태그, 그리고 리소스 이름 접두어에 쓴다.

    리소스 이름 맨 앞에 붙어 두 환경을 구분한다 (dev-ai-poc-* / prd-ai-poc-*).
    운영도 접두어를 생략하지 않는다.
  EOT
  default     = "dev"

  validation {
    condition     = contains(["dev", "prd"], var.environment)
    error_message = "environment: dev 또는 prd 여야 합니다."
  }
}

# ---------------------------------------------------------------------------
# 클러스터
# ---------------------------------------------------------------------------

variable "cluster_name" {
  type        = string
  description = <<-EOT
    GPU 노드그룹을 붙일 EKS 클러스터.

    개발 Dev-AI-POC-eks 에서 먼저 검증하고 그 결과로 운영 AI-POC-eks 에 올린다(R-24.5).
    운영 클러스터에는 팀원의 ArgoCD·GitLab Runner 가 동작 중이므로 plan 이
    No changes 가 아니면 apply 하지 않는다(R-24.4).
  EOT
}

# ---------------------------------------------------------------------------
# GPU 노드그룹
# ---------------------------------------------------------------------------

variable "gpu_subnet_ids" {
  type        = list(string)
  description = <<-EOT
    GPU 노드그룹 서브넷.

    [하드 제약] g6e 계열은 ap-northeast-2a / 2b 에만 있다 (실측:
    describe-instance-type-offerings). 3-AZ 로 두면 2c/2d 에 배치된 노드가
    용량 없음으로 실패한다. private1(2a) / private2(2b) 만 넣는다.
  EOT

  validation {
    condition     = length(var.gpu_subnet_ids) >= 1 && length(var.gpu_subnet_ids) <= 2
    error_message = "gpu_subnet_ids: g6e 는 2개 AZ(2a/2b)에만 있으므로 1~2개만 지정합니다."
  }
}

variable "gpu_instance_type" {
  type        = string
  description = <<-EOT
    GPU 인스턴스 타입. 서울 온디맨드 실측 단가(2026-09-09):

      g6e.xlarge   1x L40S 44GB  4 vCPU   32 GiB  스토어 250GB  $2.288/h
      g6e.2xlarge  1x L40S 44GB  8 vCPU   64 GiB  스토어 450GB  $2.757/h
      g6e.4xlarge  1x L40S 44GB  16 vCPU  128 GiB 스토어 600GB  $3.694/h

    세 타입 모두 GPU 1장 / VRAM 44GB 가 같으므로 타입 변경이 모델 코드에
    영향을 주지 않는다. 호스트 RAM 이 부족하면 이 값만 올린다(V-04).

    주의: L40S VRAM 은 AWS 보고값 44GB 다. 48GB 가 아니다.
  EOT
  default     = "g6e.xlarge"

  validation {
    condition     = can(regex("^g6e\\.", var.gpu_instance_type))
    error_message = "gpu_instance_type: 현재 검증된 계열은 g6e 뿐입니다."
  }
}

variable "gpu_max_size" {
  type        = number
  description = <<-EOT
    GPU 노드그룹 max. 동시 처리 목표(V-13)가 정해지면 확정한다.

    쿼터는 제약이 아니다 — L-DB2E81BA(온디맨드 G/VT) = 768 vCPU.
    g6e.xlarge 4 vCPU 기준 192대까지 가능하다.
    단 L-3819A6DF(Spot G/VT) = 0 이므로 Spot 은 쓸 수 없다.
  EOT
  default     = 1

  validation {
    condition     = var.gpu_max_size >= 1 && var.gpu_max_size <= 8
    error_message = "gpu_max_size: 비용 사고 방지를 위해 1~8 로 제한합니다."
  }
}

variable "gpu_disk_size" {
  type        = number
  description = <<-EOT
    GPU 노드 루트 EBS(GB).

    모델 가중치를 인스턴스 스토어(250GB, g6e.xlarge)나 EBS PVC 에 두면
    루트는 컨테이너 이미지 몫만 있으면 된다. 가중치를 이미지에 굽지 않는
    방침(V-05 권장안)에서는 이미지가 5GB 수준이다.
  EOT
  default     = 100
}

variable "device_plugin_version" {
  type        = string
  description = <<-EOT
    NVIDIA device plugin 이미지 태그.

    공식 README 확인: Kubernetes >= 1.10 이면 v0.0.0 보다 높은 어떤 버전이든
    배포 가능하고 device plugin API 는 v1beta1 로 안정적이다. 즉 EKS 1.36 과
    버전을 맞출 필요가 없다. 라이선스 Apache 2.0.
    NGC 카탈로그 최신 확인값이 v0.19.3 이다.
  EOT
  default     = "v0.19.3"
}

# ---------------------------------------------------------------------------
# 큐
# ---------------------------------------------------------------------------

variable "queue_visibility_timeout" {
  type        = map(number)
  description = <<-EOT
    큐별 가시성 타임아웃(초). 최장 잡 시간의 2배로 둔다(R-10.2).

    clip 값은 P2 에서 클립당 실제 생성 시간을 실측한 뒤 조정한다.
    지금 값은 잠정치다.
  EOT
  default = {
    keyframe = 600   # 이미지 생성 — 초 단위
    clip     = 3600  # 영상 생성 — 분 단위. P2 실측 후 조정
    edit     = 1800  # ffmpeg 병합
    lora     = 21600 # 학습 Job — 시간 단위. 미확인
  }
}

variable "queue_max_receive_count" {
  type        = number
  description = "DLQ 로 보내기 전 재시도 횟수. 3회 실패 시 needs_review."
  default     = 3
}

# ---------------------------------------------------------------------------
# Neptune — 이 레이어가 소유하지 않는다
#
# 그래프 DB 는 hub/26-neptune 레이어가 소유한다. 여기서는 값만 받아 워커
# 환경변수와 IAM 정책 리소스 ARN 에 쓴다.
#
# terraform_remote_state 로 그쪽 state 를 읽지 않는다. 이유는 두 가지다.
#   1) 그쪽이 for_each 구조라 output 키 이름이 바뀌면 이 레이어가 깨진다
#   2) 레이어 간 state 결합은 한쪽 리팩터링이 다른 쪽 apply 를 막는다
# 값은 아래 명령으로 얻어 tfvars 에 넣는다.
#
#   cd ../26-neptune
#   terraform output -json config_env_values
#
# 네트워크 접근은 추가 작업이 필요 없다. 관리형 노드그룹은 EKS 클러스터
# 보안 그룹을 상속하고, 26-neptune 이 그 SG 에서 8182 를 이미 허용한다.
#   dev-ai-poc-graph ← sg-09275dce10e4cfeff (Dev-AI-POC-eks)
#   ai-poc-graph     ← sg-022e24e853dfdc241 (AI-POC-eks)
# ---------------------------------------------------------------------------

variable "neptune_endpoint" {
  type        = string
  description = <<-EOT
    Neptune writer(클러스터) 엔드포인트.

    reader 엔드포인트를 넣으면 읽기는 되고 쓰기만 실패한다. 영상 잡은
    video_job / video_shot 정점을 쓰므로 반드시 writer 여야 한다.
  EOT
}

variable "neptune_port" {
  type        = number
  description = "Neptune 포트. 기본 8182."
  default     = 8182
}

variable "s3_bucket" {
  type        = string
  description = <<-EOT
    앱이 쓰는 S3 버킷. 워커 IAM 정책의 리소스 범위가 된다.

    워커는 users/*/video-jobs/* 와 _cache/keyframes/* 만 쓰고,
    users/*/characters/* 는 읽기만 한다. 버킷 전체를 열지 않는다.
  EOT
}

variable "k8s_namespace" {
  type        = string
  description = "워커가 들어갈 네임스페이스. 앱과 같은 곳을 쓴다."
  default     = "ai-poc"
}

variable "neptune_cluster_resource_id" {
  type        = string
  description = <<-EOT
    Neptune 클러스터 리소스 ID (cluster-XXXX 형태가 아니라 cluster_resource_id).

    IAM 인증을 쓸 때 neptune-db:* 정책의 리소스 ARN 이
    arn:aws:neptune-db:<region>:<account>:<resource_id>/* 형태라서 필요하다.

    [현재 상태] 26-neptune 이 만든 클러스터는 IAM 인증이 꺼져 있다.
    그래서 이 값이 없어도 접근은 된다. 빈 문자열이면 IAM 정책을 만들지 않는다.
    IAM 인증을 켜는 작업은 26-neptune 소관으로 남겨 두었다.
  EOT
  default     = ""
}
