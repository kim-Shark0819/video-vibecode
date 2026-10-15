variable "account_id" {
  type = string

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
  description = "기본 프로필은 다른 계정을 가리키므로 반드시 지정한다."
  default     = "ai-poc-hub"
}

variable "project_name" {
  type    = string
  default = "ai-poc"
}

variable "cluster_name" {
  type        = string
  description = "운영 클러스터. 이 레이어는 클러스터를 관리하지 않고 data 로만 읽는다."
  default     = "AI-POC-eks"
}

# --------------------------------------------------------------------------- #
# 노드그룹 (import 대상)
# --------------------------------------------------------------------------- #
# 아래 값은 **현재 실물과 정확히 같아야 한다.** 하나라도 다르면 Terraform 이 교체를
# 제안하고, 교체는 이 노드에 함께 올라가 있는 argocd·gitlab-runner(범위 밖)를 함께
# 내린다. `prevent_destroy` 로 그 경우 apply 가 실패하도록 막아 두었다.
variable "nodegroup_name" {
  type    = string
  default = "AI-POC-eks-ng"
}

variable "node_role_arn" {
  type    = string
  default = "arn:aws:iam::730335451955:role/AI-POC-eks-node-role"
}

variable "subnet_ids" {
  type        = list(string)
  description = "프라이빗 서브넷 3개 (2a / 2b / 2c)."
}

variable "instance_types" {
  type    = list(string)
  default = ["t3.medium"]
}

variable "ami_type" {
  type    = string
  default = "AL2023_x86_64_STANDARD"
}

variable "capacity_type" {
  type    = string
  default = "ON_DEMAND"
}

variable "disk_size" {
  type    = number
  default = 20
}

variable "kubernetes_version" {
  type        = string
  description = "노드그룹 K8s 마이너 버전. 실물과 같게 두어 의도치 않은 업그레이드를 막는다."
  default     = "1.36"
}

variable "release_version" {
  type        = string
  description = <<-EOT
    AMI 릴리스 버전. **명시한다.** 비워 두면 provider 가 최신값을 원해 plan 이 노드
    교체를 제안한다. 올릴 때는 이 값을 의도적으로 바꾼다.
  EOT
  default     = "1.36.3-20260827"
}

variable "node_labels" {
  type    = map(string)
  default = { environment = "prod" }
}

# --------------------------------------------------------------------------- #
# 스케일
# --------------------------------------------------------------------------- #
variable "min_size" {
  type    = number
  default = 1
}

variable "max_size" {
  type    = number
  default = 2
}

variable "desired_size" {
  type        = number
  description = <<-EOT
    파드 상한 때문에 올리는 값이다 (D-23 A안).

    `t3.medium` 의 파드 상한은 17개(VPC CNI ENI 한도)다. 운영 노드에는 argocd 7개 +
    gitlab-runner + kube-system 이 이미 올라가 있어 앱 4개 중 2개가 스케줄되지 않았다.
    CPU·메모리는 여유였다(요청 25% / 10%). 노드를 하나 더 두면 상한이 34가 된다.

    **`ignore_changes` 를 걸지 않는다.** 설계 문서(tasks 3.2.2)는 노드그룹의
    `desired_size` 를 무시하라고 적었는데 그것은 오토스케일러가 값을 바꿀 때의 이야기다.
    이 클러스터에는 cluster-autoscaler 도 Karpenter 도 없다(kube-system 에
    aws-load-balancer-controller 와 coredns 만 있다). 지금은 Terraform 이 이 값의
    단일 원본이다. 오토스케일러를 도입하면 그때 `ignore_changes` 를 붙인다.
  EOT
  default     = 2
}

variable "max_unavailable" {
  type        = number
  description = "노드그룹 업데이트 시 동시 교체 허용 수. 실물과 같게 둔다."
  default     = 1
}
