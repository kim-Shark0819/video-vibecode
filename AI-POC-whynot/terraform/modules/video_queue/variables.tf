variable "name" {
  type        = string
  description = "큐 이름. DLQ 는 <name>-dlq 로 만든다."
}

variable "visibility_timeout" {
  type        = number
  description = <<-EOT
    가시성 타임아웃(초). 최장 잡 시간의 2배로 둔다.

    이보다 짧으면 처리 중인 메시지가 다른 워커에게 다시 보여 같은 잡이
    두 번 돌아간다. GPU 잡은 수 분 단위이므로 특히 주의한다.
  EOT

  validation {
    condition     = var.visibility_timeout >= 30 && var.visibility_timeout <= 43200
    error_message = "visibility_timeout: SQS 허용 범위는 0~43200초입니다. 30초 이상으로 둡니다."
  }
}

variable "max_receive_count" {
  type        = number
  description = "DLQ 로 보내기 전 재시도 횟수"
  default     = 3
}

variable "retention_seconds" {
  type        = number
  description = "메인 큐 메시지 보존(초). 기본 4일."
  default     = 345600
}

variable "dlq_retention_seconds" {
  type        = number
  description = "DLQ 보존(초). 조사 여유를 위해 기본 14일."
  default     = 1209600
}

variable "tags" {
  type        = map(string)
  description = "공통 태그"
  default     = {}
}
