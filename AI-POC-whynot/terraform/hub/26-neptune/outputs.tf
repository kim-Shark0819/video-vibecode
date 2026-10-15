output "config_env_values" {
  description = <<-EOT
    deploy/config.<target>.env 에 넣을 값.

    NEPTUNE_ENDPOINT 는 **writer(클러스터) 엔드포인트**다. reader 엔드포인트를 넣으면
    읽기는 되고 쓰기만 실패해서, 로그인은 되는데 회원가입·프로젝트 생성만 안 되는
    헷갈리는 상태가 된다.
  EOT
  value = {
    for key, cluster in aws_neptune_cluster.this : key => {
      NEPTUNE_ENDPOINT       = cluster.endpoint
      NEPTUNE_PORT           = tostring(cluster.port)
      NEPTUNE_CLUSTER_ID     = cluster.cluster_identifier
      NEPTUNE_SECURITY_GROUP = aws_security_group.neptune[key].id
    }
  }
}

output "reader_endpoints" {
  description = "참고용. 앱은 쓰기를 하므로 이 값을 config 에 넣지 않는다."
  value       = { for key, cluster in aws_neptune_cluster.this : key => cluster.reader_endpoint }
}
