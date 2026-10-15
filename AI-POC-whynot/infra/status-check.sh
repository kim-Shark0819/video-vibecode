#!/bin/bash
# 현재 클러스터/워크로드 상태 조회 (읽기 전용)
export HOME=/root KUBECONFIG=/root/.kube/config AWS_DEFAULT_REGION=ap-northeast-2
aws eks update-kubeconfig --region ap-northeast-2 --name quick-test-eks --kubeconfig "$KUBECONFIG" >/dev/null 2>&1

echo "--- nodes ---"
kubectl get nodes --no-headers 2>&1

echo "--- toon-change-ai-pj pods ---"
kubectl get pods -n toon-change-ai-pj --no-headers 2>&1

echo "--- deployments ---"
kubectl get deploy -n toon-change-ai-pj --no-headers 2>&1

echo "--- images ---"
kubectl get deploy -n toon-change-ai-pj -o jsonpath='{range .items[*]}{.metadata.name}{" -> "}{.spec.template.spec.containers[0].image}{"\n"}{end}' 2>&1

echo "--- ingress ---"
kubectl get ingress -n toon-change-ai-pj --no-headers 2>&1

echo "--- app health (in-cluster) ---"
POD=$(kubectl get pods -n toon-change-ai-pj -l app.kubernetes.io/name=toon-change-ai-pj-app -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
if [ -n "$POD" ]; then
  kubectl exec -n toon-change-ai-pj "$POD" -- python -c "import json,urllib.request; print(json.dumps(json.load(urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=10)), ensure_ascii=False))" 2>&1
else
  echo "app pod not found"
fi

echo "--- billing (참고, 건드리지 않음) ---"
kubectl get deploy -n billing --no-headers 2>&1
