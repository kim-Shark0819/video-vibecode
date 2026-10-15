provider "aws" {
  region  = var.region
  profile = var.aws_profile

  allowed_account_ids = [var.account_id]

  # 기존 리소스를 import 하므로 default_tags 가 태그를 **추가**한다. 삭제는 하지 않는다.
  # 운영 리소스에 붙는 변경이라 plan 에서 tags_all 증가만 나타나는지 확인한 뒤 적용한다.
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
