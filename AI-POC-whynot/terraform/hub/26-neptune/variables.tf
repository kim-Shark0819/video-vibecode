variable "account_id" {
  type        = string
  description = "이 구성이 적용될 AWS 계정. 다르면 실행을 중단한다."

  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "account_id: 12자리 숫자여야 합니다."
  }
}

variable "region" {
  type    = string
  default = "ap-northeast-2"
}

variable "aws_profile" {
  type        = string
  description = "AWS CLI named profile. 기본 프로필은 다른 계정을 가리키므로 반드시 지정한다."
  default     = "ai-poc-hub"
}

variable "project_name" {
  type    = string
  default = "ai-poc"
}

variable "vpc_id" {
  type        = string
  description = "AI-poc-vpc. 작업 범위가 이 VPC 내부로 한정된다."
}

variable "subnet_ids" {
  type        = list(string)
  description = <<-EOT
    Neptune 서브넷 그룹에 쓸 프라이빗 서브넷. **최소 2개 AZ** 가 필요하다.

    퍼블릭 서브넷을 넣지 않는다. Neptune 은 퍼블릭 접근을 지원하지 않지만,
    퍼블릭 서브넷에 두면 라우팅 실수 한 번의 여지를 남긴다.
  EOT

  validation {
    condition     = length(var.subnet_ids) >= 2
    error_message = "subnet_ids: Neptune 서브넷 그룹은 서로 다른 AZ 의 서브넷이 2개 이상 필요합니다."
  }
}

# --------------------------------------------------------------------------- #
# 대상별 설정
# --------------------------------------------------------------------------- #
variable "targets" {
  type = map(object({
    # RDS/Neptune 식별자는 소문자여야 한다. 대문자를 넣으면 거절된다.
    cluster_identifier = string
    subnet_group_name  = string
    security_group_name = string
    # 이 클러스터의 파드만 8182 인바운드를 허용한다. EKS 클러스터 보안그룹 ID.
    # 관리형 노드그룹의 ENI 에 이 SG 가 붙으므로 VPC CNI 파드도 이 SG 로 나간다.
    # (origin 계정도 같은 방식이었다 — infra/neptune-sg-ingress.json)
    allowed_security_group_id = string
    instance_class            = string
    backup_retention_days     = number
  }))
  description = "키는 deploy.ps1 -Target 값(dev|prod)과 같게 둔다."

  validation {
    condition = alltrue([
      for t in values(var.targets) : lower(t.cluster_identifier) == t.cluster_identifier
    ])
    error_message = "cluster_identifier: 소문자만 허용됩니다 (RDS/Neptune 제약)."
  }
}

variable "engine_version" {
  type        = string
  description = "Neptune 엔진 버전. 명시하지 않으면 배포 시점마다 달라져 재현이 깨진다."
  default     = "1.4.8.0"
}

variable "neptune_port" {
  type        = number
  description = "config.env 의 NEPTUNE_PORT 와 같아야 한다."
  default     = 8182
}

variable "iam_database_authentication_enabled" {
  type        = bool
  description = <<-EOT
    Neptune IAM 인증.

    **false 여야 앱이 동작한다.** app/src/graph_store.py 의 run() 은 순수 urllib POST 로
    openCypher 를 호출하고 SigV4 서명을 하지 않는다. true 로 두면 모든 질의가 403 이 되고,
    앱은 기동하지만 로그인·프로젝트가 전부 실패한다.

    대신 접근 통제가 네트워크 한 겹(보안그룹)뿐이다. 이 DB 에는 사용자 비밀번호 해시
    (PBKDF2 210k)가 들어간다. 그래서 인바운드를 해당 클러스터 보안그룹에서 8182 로만 열고
    저장 시 암호화를 켠다. 앱에 SigV4 서명을 넣으면 이 값을 true 로 올릴 수 있다.
  EOT
  default     = false
}

variable "deletion_protection" {
  type        = bool
  description = "실수로 지우는 것을 AWS 단계에서 막는다. 지울 때는 이 값을 먼저 false 로 apply 한다."
  default     = true
}
