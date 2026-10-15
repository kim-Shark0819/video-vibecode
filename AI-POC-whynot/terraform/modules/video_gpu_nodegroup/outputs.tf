output "nodegroup_name" {
  description = "생성된 노드그룹 이름"
  value       = aws_eks_node_group.this.node_group_name
}

output "node_role_arn" {
  description = "노드 IAM 역할 ARN. EKS access entry 등록에 쓴다."
  value       = aws_iam_role.node.arn
}

output "node_role_name" {
  description = "노드 IAM 역할 이름"
  value       = aws_iam_role.node.name
}

output "status" {
  description = "노드그룹 상태"
  value       = aws_eks_node_group.this.status
}
