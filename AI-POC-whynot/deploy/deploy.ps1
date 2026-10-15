<#
.SYNOPSIS
    소스를 패키징해 배스천에서 이미지를 빌드하고 EKS에 배포한다.

.DESCRIPTION
    로컬에 Docker가 없고 EKS API가 프라이빗 전용이라, 빌드와 kubectl 실행은 모두 배스천에서 일어난다.
    이 스크립트는 그 과정을 한 번에 수행한다.

      1) preflight.ps1로 계정/클러스터/OIDC 검증 (실패 시 중단)
      2) ECR에 IMAGE_TAG가 이미 있는지 확인 (태그가 immutable이라 재푸시 불가)
      3) 템플릿 렌더링 후 렌더 결과가 config.env의 태그와 일치하는지 확인
      4) app/frontend/k8s를 tar.gz로 묶어 S3 _build/에 올리고 presigned URL 생성
      5) infra/build-deploy.sh를 base64로 감싸 SSM으로 배스천에 전달해 실행
      6) 완료까지 폴링하고 출력을 보여준다

    태그는 config.env의 IMAGE_TAG를 단일 원본으로 쓴다. 스크립트에 태그를 넣지 않는다.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File AI-POC/deploy/deploy.ps1

.EXAMPLE
    # 렌더링을 건너뛰고 (이미 렌더한 경우) 배포만 다시 시도
    powershell -NoProfile -ExecutionPolicy Bypass -File AI-POC/deploy/deploy.ps1 -SkipRender

.NOTES
    Windows PowerShell 5.1 호환.
    zip을 쓰지 않는 이유: Windows에서 만든 zip은 배스천 unzip이 경로 구분자를 깨뜨린다.
#>
[CmdletBinding()]
param(
    # 배포 대상 클러스터. config.<Target>.env 오버레이를 적용한다.
    #
    #   prod -> AI-POC-eks / AI-POC-web-alb / ai-poc-web.meta-clouds.com
    #
    # **dev 대상은 제거했다.** `Dev-AI-POC-eks` 클러스터를 삭제해서 배포할 곳이 없다.
    # 오버레이 구조 자체는 남긴다 — 값을 클러스터별로 분리하는 것이 이 파이프라인의
    # 버그 하나(호출부와 스크립트의 필수값 목록이 어긋난 것)를 만든 원인이었고,
    # Phase 5 의 고객사별 배포에서 다시 필요하다.
    #
    # 대상이 하나라 기본값을 둔다. 인수를 빼도 운영으로 간다.
    [ValidateSet('prod')]
    [string]$Target = 'prod',
    [switch]$SkipRender,
    [int]$TimeoutMinutes = 25,
    # 이미 존재하는 태그로 다시 배포해야 할 때만 사용한다. 이미지 재푸시는 실패하고 apply만 다시 돈다.
    [switch]$AllowExistingTag,
    # 커밋되지 않은 변경이 있어도 경고 문구를 줄인다. **막지는 않는다** — 지금 개발 흐름이
    # 로컬 워킹 디렉터리를 배포하는 방식이라 막으면 아무것도 배포할 수 없다.
    # 이 스위치는 "알고 있다"는 표시일 뿐이고, 커밋 SHA·dirty 여부는 어느 쪽이든 기록된다.
    [switch]$AllowDirty,
    # DEPLOY_MANAGED_BY=argocd 인 대상에 강제로 수동 배포한다. ArgoCD selfHeal 이 되돌리므로 디버깅용.
    [switch]$ForceManual
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$scriptDir = $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($scriptDir)) {
    $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
}
$projectRoot = Split-Path -Parent $scriptDir
$workspaceRoot = Split-Path -Parent $projectRoot

function Write-Stage {
    param([string]$Message)
    Write-Host ''
    Write-Host "=== $Message ===" -ForegroundColor Cyan
}

# --- 1. 선행 확인 (계정 불일치면 여기서 종료된다) ---
Write-Stage "선행 확인 (대상 $Target)"
. (Join-Path $scriptDir 'preflight.ps1') -Target $Target
$config = $script:ToonConfig

# --- ArgoCD 관리 대상 가드 ---
# config.<target>.env 의 DEPLOY_MANAGED_BY 가 argocd 이면 이 스크립트로 배포하지 않는다.
# ArgoCD 의 selfHeal 이 몇 분 안에 git(ai-deploy) 상태로 되돌리므로 여기서 apply 한 것은 사라진다.
# 배포 경로는 "ai 저장소 main 에 푸시 -> CI 빌드·렌더 -> ArgoCD sync" 다.
if ($config['DEPLOY_MANAGED_BY'] -eq 'argocd' -and -not $ForceManual) {
    throw "대상 '$Target' 은 ArgoCD 가 관리한다. main 에 푸시하면 자동 배포된다. 정말 수동 배포가 필요하면 -ForceManual 을 붙인다(ArgoCD 가 되돌린다)."
}

# build-deploy.sh 가 `${VAR:?}` 로 요구하는 값을 여기서 함께 검사한다.
# 예전에는 이 목록이 build-deploy.sh 의 요구와 어긋나 있었다. deploy.ps1 은 통과시키고
# 배스천에서 20분 뒤에 "AWS_ACCOUNT_ID 필요"로 죽었다. 두 곳이 같은 목록을 봐야 한다.
$requiredForBuild = @(
    'AWS_ACCOUNT_ID', 'AWS_REGION', 'EKS_CLUSTER_NAME', 'K8S_NAMESPACE',
    'ECR_APP_REPO', 'ECR_FRONTEND_REPO', 'APP_SERVICE_ACCOUNT', 'FRONTEND_DEPLOYMENT_NAME',
    'AUTH_SECRET_NAME', 'IMAGE_TAG', 'S3_BUCKET', 'BASTION_INSTANCE_ID',
    'ADMIN_USERNAME', 'ADMIN_PASSWORD', 'ALB_NAME', 'IAM_APP_ROLE_NAME'
)
foreach ($key in $requiredForBuild) {
    if (-not $config.ContainsKey($key) -or [string]::IsNullOrWhiteSpace($config[$key])) {
        throw "config.env(또는 config.$Target.env)에 $key 값이 필요합니다."
    }
}
$tag = $config['IMAGE_TAG']
$bucket = $config['S3_BUCKET']
$instanceId = $config['BASTION_INSTANCE_ID']
$region = $config['AWS_REGION']
$cluster = $config['EKS_CLUSTER_NAME']
Write-Host "대상 클러스터: $cluster / ALB: $($config['ALB_NAME']) / 앱 역할: $($config['IAM_APP_ROLE_NAME'])"
# 인증 Secret을 config.env 값으로 맞춘다. 값은 출력하지 않는다.
# bash 단일 인용 안으로 들어가므로 작은따옴표만 이스케이프하면 된다.
$adminUsername = $config['ADMIN_USERNAME']
$adminPasswordEscaped = $config['ADMIN_PASSWORD'].Replace("'", "'\''")
# 아래 세 값은 workloads 템플릿이 Secret 에서 읽으므로 build-deploy.sh 가 Secret 키로 만든다.
$companyCodeEscaped = ([string]$config['DEFAULT_COMPANY_CODE']).Replace("'", "'\''")
$seedUsersEscaped   = ([string]$config['SEED_USERS']).Replace("'", "'\''")
$userQuotasEscaped  = ([string]$config['USER_QUOTAS']).Replace("'", "'\''")
Write-Host "배포 태그: $tag"

# --- 1-2. 소스 상태 확인 (F-24) ---
#
# ## 왜 필요한가
#
# 이 파이프라인은 **로컬 워킹 디렉터리를 그대로** tar 로 묶어 배포한다. GitLab 을 거치지
# 않으므로 커밋하지 않은 변경도 그대로 운영에 올라간다. 편리하지만 대가가 있다.
#
#   - 배포된 이미지에 어떤 소스가 들어갔는지 커밋으로 되짚을 수 없다
#   - 워크스테이션이 고장나면 그 사이 작업이 사라진다
#   - 품질 실험 결과를 "어느 코드로 낸 값"인지 특정할 수 없다
#
# 마지막 항목이 지금 실제 문제다. 영상 품질을 A/B 하려면 결과마다 코드 버전이 붙어야 한다.
# 그래서 막지는 않되(막으면 지금 개발 흐름이 끊긴다) **커밋 SHA 와 dirty 여부를 기록**하고
# 눈에 보이게 알린다. 배포 산출물(manifest)에도 같은 값이 들어간다.
Write-Stage '소스 상태'
. (Join-Path $scriptDir 'source-stamp.ps1')
# git 저장소는 $projectRoot(AI-POC) 다. 예전 주석은 워크스페이스가 저장소라고 적고
# $workspaceRoot 를 넘겼는데, 워크스페이스 루트에는 .git 이 없다. git 이 실패해 스탬프가
# 조용히 'unknown-dirty' 로 떨어졌고 커밋 맥락이 배포 기록에서 사라졌다.
$stampInfo = Get-SourceStamp -RepoRoot $projectRoot
Write-SourceStamp -Info $stampInfo -AllowDirty:$AllowDirty
# **여기서는 맥락만 확인한다.** 실제 소스 스탬프는 아래 패키징 단계에서 아카이브 SHA256 으로
# 정한다(Get-ArchiveStamp). 커밋 SHA 는 워킹 디렉터리 배포에서 결과를 특정하지 못한다.

# --- 2. ECR 태그 중복 확인 ---
# ECR 저장소가 IMMUTABLE이라 같은 태그로 푸시하면 빌드 후반에 실패한다. 미리 걸러 시간을 아낀다.
Write-Stage 'ECR 태그 확인'
foreach ($repo in @($config['ECR_APP_REPO'], $config['ECR_FRONTEND_REPO'])) {
    $existing = Invoke-AwsCli @('ecr', 'describe-images', '--repository-name', $repo, '--image-ids', "imageTag=$tag", '--region', $region, '--query', 'imageDetails[0].imageDigest', '--output', 'text')
    if ($existing.ExitCode -eq 0) {
        if (-not $AllowExistingTag) {
            Write-Host ''
            Write-Host "=== 태그가 이미 존재합니다: ${repo}:$tag ===" -ForegroundColor Red
            Write-Host 'ECR 태그는 immutable이라 같은 태그로 다시 푸시할 수 없습니다.'
            Write-Host 'config.env의 IMAGE_TAG를 올린 뒤 다시 실행하세요.'
            exit 5
        }
        Write-Host "경고: ${repo}:$tag 가 이미 있습니다. 이미지 푸시는 실패하고 매니페스트만 다시 적용됩니다." -ForegroundColor Yellow
    }
    else {
        Write-Host "사용 가능: ${repo}:$tag"
    }
}

# --- 3. 렌더링 + 태그 일치 확인 ---
if (-not $SkipRender) {
    Write-Stage '템플릿 렌더링'
    & python (Join-Path $scriptDir 'render.py') '--target' $Target
    if ($LASTEXITCODE -ne 0) { throw '렌더링에 실패했습니다.' }
}

$workloadsPath = Join-Path $scriptDir 'rendered\k8s\workloads.yaml'
if (-not (Test-Path -LiteralPath $workloadsPath)) {
    throw "렌더 결과가 없습니다: $workloadsPath (-SkipRender를 빼고 실행하세요)"
}
# 렌더를 건너뛰었거나 실패한 경우 이전 태그로 배포되어 조용히 롤백되는 사고를 막는다.
$imageLines = Select-String -LiteralPath $workloadsPath -Pattern '^\s*image:\s*(\S+)' -AllMatches
foreach ($line in $imageLines) {
    $image = $line.Matches[0].Groups[1].Value
    if (-not $image.EndsWith(":$tag")) {
        throw "렌더된 매니페스트의 이미지 태그가 config.env와 다릅니다: $image (기대 태그 $tag)"
    }
}
Write-Host "매니페스트 이미지 태그 확인: $($imageLines.Count)건 모두 $tag"

# --- 3-2. 렌더 결과가 이 대상의 것인지 확인 ---
#
# 렌더 출력 경로는 대상과 무관하게 deploy/rendered 하나다. 즉 dev 로 렌더한 트리가
# 남아 있는 상태에서 -SkipRender 로 prod 를 배포하면 **dev 값이 운영에 올라간다.**
# ALB 이름이 겹치면 두 클러스터의 컨트롤러가 같은 ALB 를 서로 고쳐 쓰고,
# IRSA 역할이 어긋나면 파드가 S3·Bedrock 권한을 얻지 못한다. 둘 다 조용히 실패한다.
# 그래서 태그와 같은 방식으로 **실제 렌더 결과의 내용**을 검증한다.
$targetChecks = @(
    @{ File = 'ingress.yaml'; Pattern = 'load-balancer-name:\s*(\S+)'; Expected = $config['ALB_NAME']; Label = 'ALB 이름' }
    @{ File = 'service-account.yaml'; Pattern = 'role-arn:\s*(\S+)'; Expected = "arn:aws:iam::$($config['AWS_ACCOUNT_ID']):role/$($config['IAM_APP_ROLE_NAME'])"; Label = 'IRSA 역할 ARN' }
)
foreach ($check in $targetChecks) {
    $path = Join-Path $scriptDir "rendered\k8s\$($check.File)"
    if (-not (Test-Path -LiteralPath $path)) { throw "렌더 결과가 없습니다: $path" }
    $found = Select-String -LiteralPath $path -Pattern $check.Pattern | Select-Object -First 1
    if (-not $found) { throw "$($check.File) 에서 $($check.Label) 을 찾지 못했습니다." }
    $actual = $found.Matches[0].Groups[1].Value
    if ($actual -ne $check.Expected) {
        throw "렌더 결과가 대상 '$Target' 과 다릅니다. $($check.Label): $actual (기대 $($check.Expected)). -SkipRender 를 빼고 다시 실행하세요."
    }
    Write-Host "  $($check.Label) 확인: $actual"
}

# --- 4. 패키징 + 업로드 ---
Write-Stage '소스 패키징'
$buildDir = Join-Path $workspaceRoot '.build'
New-Item -ItemType Directory -Path $buildDir -Force | Out-Null
$archive = Join-Path $buildDir "toon-$tag.tar.gz"
Remove-Item -LiteralPath $archive -Force -ErrorAction SilentlyContinue

& tar -czf $archive -C $projectRoot app frontend -C (Join-Path $projectRoot 'deploy\rendered') k8s
if ($LASTEXITCODE -ne 0) { throw '패키징에 실패했습니다.' }
Write-Host ("아카이브: {0} ({1:N0} bytes)" -f $archive, (Get-Item -LiteralPath $archive).Length)

$s3Key = "_build/toon-$tag.tar.gz"
# --- 소스 신원 확정 (F-24) ---
#
# **커밋이 아니라 아카이브 해시를 기준값으로 쓴다.** 이 파이프라인은 워킹 디렉터리를
# 배포하므로 커밋 SHA 는 `<sha>-dirty` 가 되어 "무엇이 바뀌었는지"를 말하지 못하고,
# 같은 커밋의 서로 다른 두 배포가 같은 값을 갖는다. 아카이브 해시는 실제로 빌드에 들어간
# 바이트를 가리켜 실험 결과를 유일하게 특정한다. 커밋 정보는 맥락으로 함께 남긴다.
$archiveStamp = Get-ArchiveStamp -ArchivePath $archive -S3Key $s3Key
$sourceStamp = $archiveStamp.Stamp
Write-Host "소스 스탬프: $sourceStamp  (sha256 $($archiveStamp.Sha256))"
Write-Host "  맥락: 커밋 $($stampInfo.Sha)$(if ($stampInfo.Dirty) { " + 미커밋 $($stampInfo.DirtyCount)건" })"

Write-Stage 'S3 업로드'
$upload = Invoke-AwsCli @('s3', 'cp', $archive, "s3://$bucket/$s3Key", '--only-show-errors')
if ($upload.ExitCode -ne 0) { throw "업로드에 실패했습니다: $($upload.Output)" }
Write-Host "s3://$bucket/$s3Key"

$presign = Invoke-AwsCli @('s3', 'presign', "s3://$bucket/$s3Key", '--expires-in', '3600')
if ($presign.ExitCode -ne 0) { throw "presigned URL 생성에 실패했습니다: $($presign.Output)" }
$url = $presign.Output

# --- 5. 배스천 실행 ---
Write-Stage '배스천 빌드 및 배포'
# -Encoding UTF8 필수: 기본 ANSI 해석은 스크립트의 한글 주석을 깨뜨린다.
$buildScript = (Get-Content -Raw -Encoding UTF8 -LiteralPath (Join-Path $projectRoot 'infra\build-deploy.sh')) -replace "`r`n", "`n"

# 배스천에서 실행할 래퍼. 소스를 내려받아 풀고 build-deploy.sh를 실행한다.
# heredoc을 'SCRIPT_EOF'로 quote해 배스천 셸이 build-deploy.sh 내용을 치환하지 않게 한다.
$wrapper = @"
#!/bin/bash
set -euo pipefail
export HOME=/root KUBECONFIG=/root/.kube/config AWS_DEFAULT_REGION=$region
WORK=/tmp/toon-deploy
rm -rf "`$WORK"
mkdir -p "`$WORK"
curl -sSfL -o /tmp/toon-src.tar.gz '$url'
tar -xzf /tmp/toon-src.tar.gz -C "`$WORK" 2>/dev/null
cat > "`$WORK/build-deploy.sh" <<'SCRIPT_EOF'
$buildScript
SCRIPT_EOF
chmod +x "`$WORK/build-deploy.sh"
env \
  AWS_ACCOUNT_ID='$($config['AWS_ACCOUNT_ID'])' \
  AWS_REGION='$region' \
  EKS_CLUSTER_NAME='$cluster' \
  K8S_NAMESPACE='$($config['K8S_NAMESPACE'])' \
  ECR_APP_REPO='$($config['ECR_APP_REPO'])' \
  ECR_FRONTEND_REPO='$($config['ECR_FRONTEND_REPO'])' \
  APP_SERVICE_ACCOUNT='$($config['APP_SERVICE_ACCOUNT'])' \
  FRONTEND_DEPLOYMENT_NAME='$($config['FRONTEND_DEPLOYMENT_NAME'])' \
  AUTH_SECRET_NAME='$($config['AUTH_SECRET_NAME'])' \
  IMAGE_TAG='$tag' \
  SOURCE_STAMP='$sourceStamp' \
  SOURCE_ARCHIVE='$s3Key' \
  SOURCE_GIT='$($stampInfo.Sha)$(if ($stampInfo.Dirty) { "+$($stampInfo.DirtyCount)" })' \
  ADMIN_USERNAME='$adminUsername' \
  ADMIN_PASSWORD='$adminPasswordEscaped' \
  DEFAULT_COMPANY_CODE='$companyCodeEscaped' \
  SEED_USERS='$seedUsersEscaped' \
  USER_QUOTAS='$userQuotasEscaped' \
  bash "`$WORK/build-deploy.sh"
"@
$wrapper = $wrapper -replace "`r`n", "`n"

# SSM --parameters에 인라인 JSON을 넘기면 PowerShell에서 인용부호가 깨진다.
# 스크립트를 base64로 감싸고 파라미터는 BOM 없는 UTF-8 파일로 넘긴다(BOM이 있으면 CLI가 거부).
$encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($wrapper))
$parameters = @{ commands = @("echo $encoded | base64 -d > /tmp/toon-deploy.sh", 'bash /tmp/toon-deploy.sh') } |
    ConvertTo-Json -Depth 3 -Compress
$parametersPath = Join-Path $buildDir 'ssm-deploy.json'
[IO.File]::WriteAllText($parametersPath, $parameters, (New-Object Text.UTF8Encoding($false)))

$send = Invoke-AwsCli @('ssm', 'send-command', '--instance-ids', $instanceId, '--document-name', 'AWS-RunShellScript', '--parameters', "file://$parametersPath", '--timeout-seconds', '1800', '--query', 'Command.CommandId', '--output', 'text')
if ($send.ExitCode -ne 0) { throw "SSM 명령 전송에 실패했습니다: $($send.Output)" }
$commandId = $send.Output
Write-Host "CommandId: $commandId"

$deadline = (Get-Date).AddMinutes($TimeoutMinutes)
$status = 'Pending'
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 15
    $poll = Invoke-AwsCli @('ssm', 'get-command-invocation', '--command-id', $commandId, '--instance-id', $instanceId, '--query', 'Status', '--output', 'text')
    if ($poll.ExitCode -ne 0) { continue }
    $status = $poll.Output
    Write-Host "  상태: $status"
    if ($status -notin @('Pending', 'InProgress', 'Delayed')) { break }
}

$stdout = Invoke-AwsCli @('ssm', 'get-command-invocation', '--command-id', $commandId, '--instance-id', $instanceId, '--query', 'StandardOutputContent', '--output', 'text')
Write-Stage '배스천 출력'
Write-Host $stdout.Output

if ($status -ne 'Success') {
    $stderr = Invoke-AwsCli @('ssm', 'get-command-invocation', '--command-id', $commandId, '--instance-id', $instanceId, '--query', 'StandardErrorContent', '--output', 'text')
    Write-Host ''
    Write-Host '=== 배포 실패 ===' -ForegroundColor Red
    Write-Host "상태: $status"
    if (-not [string]::IsNullOrWhiteSpace($stderr.Output)) { Write-Host $stderr.Output }
    exit 6
}

# --- 6. 접속 주소 ---
#
# APP_DOMAIN 이 비어 있는 동안은 도메인도 인증서도 없다. 예전에는 여기서 무조건
# "https://$APP_DOMAIN" 을 찍어 `https://` 만 출력했다. 실제로 열어야 하는 주소는
# 컨트롤러가 만든 ALB 의 DNS 이름이고 리스너는 80 뿐이다(ingress.yaml.tmpl 주석 참고).
Write-Stage '접속 주소'
$albName = $config['ALB_NAME']
$dns = Invoke-AwsCli @('elbv2', 'describe-load-balancers', '--names', $albName, '--region', $region, '--query', 'LoadBalancers[0].DNSName', '--output', 'text')
if ($dns.ExitCode -eq 0 -and -not [string]::IsNullOrWhiteSpace($dns.Output)) {
    Write-Host "  ALB $albName -> http://$($dns.Output)"
    if (-not [string]::IsNullOrWhiteSpace($config['APP_DOMAIN'])) {
        Write-Host "  도메인 -> https://$($config['APP_DOMAIN'])"
    }
}
else {
    # 컨트롤러가 ALB 를 만드는 데 보통 2~3분 걸린다. rollout 이 끝난 직후에는 아직 없을 수 있다.
    Write-Host "  ALB $albName 을 아직 조회할 수 없습니다. 2~3분 뒤 다시 확인하세요." -ForegroundColor Yellow
    Write-Host "  확인: aws elbv2 describe-load-balancers --names $albName --region $region --query 'LoadBalancers[0].DNSName' --output text"
}

Write-Host ''
Write-Host "배포 완료: 대상 $Target / 클러스터 $cluster / 태그 $tag" -ForegroundColor Green
exit 0
