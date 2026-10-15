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
  description = "리소스 이름 접두어에 쓰인다."
  default     = "ai-poc"
}
