provider "aws" {
  region  = var.region
  profile = var.aws_profile

  # 1차 방어: 자격증명이 다른 계정이면 provider 초기화 단계에서 막는다.
  # 로컬 기본 프로필은 원본 계정(061051247564)을 가리키므로 실수 여지가 크다.
  allowed_account_ids = [var.account_id]

  default_tags {
    tags = local.common_tags
  }
}

# 키프레임 생성용 Bedrock 이미지 모델은 서울 리전에 없다 (실측 2026-09-09:
# ap-northeast-2 의 IMAGE 출력 모델 0개). 이 alias 는 그 호출에 쓰는 리전을
# 인프라 쪽에서도 한 곳으로 모아 두기 위한 것이다. 서울에 모델이 들어오면
# var.bedrock_image_region 만 바꾼다.
provider "aws" {
  alias   = "bedrock_image"
  region  = var.bedrock_image_region
  profile = var.aws_profile

  allowed_account_ids = [var.account_id]

  default_tags {
    tags = local.common_tags
  }
}

data "aws_caller_identity" "current" {}

# 2차 방어: plan 단계에서 계정을 다시 확인한다. allowed_account_ids 만으로는
# assume_role 이 끼었을 때 놓칠 수 있다.
resource "terraform_data" "account_guard" {
  input = data.aws_caller_identity.current.account_id

  lifecycle {
    precondition {
      condition     = data.aws_caller_identity.current.account_id == var.account_id
      error_message = "현재 자격증명 계정이 var.account_id 와 다릅니다. 실행을 중단합니다."
    }
  }
}
