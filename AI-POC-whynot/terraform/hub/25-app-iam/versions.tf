terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    # OIDC provider 등록에 필요한 thumbprint(발급자 인증서의 SHA-1 지문)를 얻는다.
    # 값을 손으로 박아 두면 EKS 가 인증서를 교체할 때 IRSA 가 조용히 끊긴다.
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
    }
  }
}
