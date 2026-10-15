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

variable "aws_profile" {
  type        = string
  description = "AWS CLI named profile. 기본 프로필은 다른 계정을 가리키므로 반드시 지정한다."
  default     = "ai-poc-hub"
}

variable "project_name" {
  type        = string
  description = "공통 태그에 쓰인다."
  default     = "ai-poc"
}

# --------------------------------------------------------------------------- #
# 배포 대상
# --------------------------------------------------------------------------- #
# deploy/config.dev.env · config.prod.env 와 **같은 값**이어야 한다. 두 곳이 어긋나면
# 렌더된 ServiceAccount 가 존재하지 않는 역할을 가리키고, 파드는 기동하지만 S3·Bedrock
# 호출에서만 실패한다. 그 실패는 화면에서 "생성 실패"로만 보여 원인이 멀다.
variable "targets" {
  type = map(object({
    cluster_name       = string
    role_name          = string
    s3_policy_name     = string
    bedrock_policy_name = string
  }))
  description = "대상 클러스터별 이름. 키는 deploy.ps1 -Target 값(dev|prod)과 같게 둔다."
}

variable "k8s_namespace" {
  type        = string
  description = "앱 네임스페이스. config.env 의 K8S_NAMESPACE 와 같아야 한다."
  default     = "ai-poc"
}

variable "app_service_account" {
  type        = string
  description = "앱 ServiceAccount 이름. config.env 의 APP_SERVICE_ACCOUNT 와 같아야 한다."
  default     = "ai-poc-app"
}

# --------------------------------------------------------------------------- #
# 스토리지
# --------------------------------------------------------------------------- #
variable "s3_bucket" {
  type        = string
  description = "앱 콘텐츠 버킷 이름. config.env 의 S3_BUCKET 과 같아야 한다."
}

variable "s3_video_bucket" {
  type        = string
  description = <<-EOT
    영상 출력 버킷(us-west-2). 아직 없으면 빈 문자열로 둔다.

    비어 있으면 정책의 S3_VIDEO_BUCKET_ARN 을 콘텐츠 버킷 ARN 으로 대체한다.
    빈 값을 그대로 붙이면 `arn:aws:s3:::` 이 되어 IAM 이 MalformedPolicyDocument 로
    거절한다. deploy/render.py 의 add_derived 와 같은 규칙이다.
  EOT
  default     = ""
}

# --------------------------------------------------------------------------- #
# 기존 OIDC provider
# --------------------------------------------------------------------------- #
variable "existing_oidc_provider_targets" {
  type        = set(string)
  description = <<-EOT
    OIDC provider 가 **이미 IAM 에 등록된** 대상 키 목록.

    운영 클러스터(AI-POC-eks)의 provider 는 이미 있고 다른 역할(albc)이 쓰고 있다.
    Terraform 이 관리하면 향후 실수로 지울 때 그 역할까지 끊긴다. 그래서 여기 적힌
    대상은 **data 로 읽기만** 하고, 나머지는 resource 로 새로 등록한다.
  EOT
  default     = ["prod"]
}
