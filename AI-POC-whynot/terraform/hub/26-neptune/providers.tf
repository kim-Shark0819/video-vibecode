provider "aws" {
  region  = var.region
  profile = var.aws_profile

  allowed_account_ids = [var.account_id]

  default_tags {
    tags = local.common_tags
  }
}

data "aws_caller_identity" "current" {}

resource "terraform_data" "account_guard" {
  input = data.aws_caller_identity.current.account_id

  lifecycle {
    precondition {
      condition     = data.aws_caller_identity.current.account_id == var.account_id
      error_message = "현재 자격증명 계정이 var.account_id 와 다릅니다. 실행을 중단합니다."
    }
  }
}
