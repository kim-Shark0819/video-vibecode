output "app_role_arns" {
  description = "대상별 앱 역할 ARN. 렌더된 service-account.yaml 의 role-arn 과 일치해야 한다."
  value       = { for key, role in aws_iam_role.app : key => role.arn }
}

output "oidc_provider_arns" {
  description = "대상별 OIDC provider ARN (기존 + 신규)."
  value       = local.oidc_provider_arn
}

output "config_env_check" {
  description = <<-EOT
    deploy/config.<target>.env 에 들어가야 하는 값. 실제 config 와 눈으로 대조한다.
    두 곳이 어긋나면 파드는 뜨지만 S3·Bedrock 호출에서만 실패한다.
  EOT
  value = {
    for key, target in var.targets : key => {
      EKS_CLUSTER_NAME  = target.cluster_name
      EKS_OIDC_ID       = element(split("/", local.oidc_host[key]), length(split("/", local.oidc_host[key])) - 1)
      IAM_APP_ROLE_NAME = target.role_name
    }
  }
}
