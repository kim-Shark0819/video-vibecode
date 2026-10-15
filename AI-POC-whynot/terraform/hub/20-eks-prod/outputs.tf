output "nodegroup" {
  description = "적용 후 확인용. desired_size 가 의도한 값인지 본다."
  value = {
    name            = aws_eks_node_group.prod.node_group_name
    status          = aws_eks_node_group.prod.status
    instance_types  = aws_eks_node_group.prod.instance_types
    scaling         = aws_eks_node_group.prod.scaling_config
    release_version = aws_eks_node_group.prod.release_version
  }
}

output "pod_capacity_note" {
  description = "t3.medium 의 파드 상한은 노드당 17개다. desired_size 를 곱해 총 용량을 가늠한다."
  value       = "nodes=${var.desired_size} x 17 pods = ${var.desired_size * 17} pod slots"
}
