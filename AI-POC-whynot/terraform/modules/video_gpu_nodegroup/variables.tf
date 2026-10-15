variable "name_prefix" {
  type        = string
  description = "리소스 이름 접두어. 예: ai-poc-dev"
}

variable "cluster_name" {
  type        = string
  description = "노드그룹을 붙일 EKS 클러스터 이름"
}

variable "subnet_ids" {
  type        = list(string)
  description = <<-EOT
    노드그룹 서브넷.

    [하드 제약] g6e 계열은 ap-northeast-2a / 2b 에만 있다.
    private1(2a) / private2(2b) 만 넣는다.
  EOT
}

variable "instance_type" {
  type        = string
  description = <<-EOT
    GPU 인스턴스 타입.

    서울 온디맨드 실측 단가(2026-09-09):
      g6e.xlarge  $2.288/h  1x L40S 44GB  4 vCPU   32 GiB  스토어 250GB
      g6e.2xlarge $2.757/h  1x L40S 44GB  8 vCPU   64 GiB  스토어 450GB
      g6e.4xlarge $3.694/h  1x L40S 44GB  16 vCPU  128 GiB 스토어 600GB

    GPU 와 VRAM 이 세 타입 모두 같으므로 타입 변경이 모델 코드에 영향을 주지 않는다.
  EOT
  default     = "g6e.xlarge"
}

variable "max_size" {
  type        = number
  description = "노드그룹 max. min/desired 는 0 으로 고정한다."
  default     = 1
}

variable "disk_size" {
  type        = number
  description = "루트 EBS 크기(GB)"
  default     = 100
}

variable "tags" {
  type        = map(string)
  description = "공통 태그"
  default     = {}
}
