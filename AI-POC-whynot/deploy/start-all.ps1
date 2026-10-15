<#
.SYNOPSIS
    09시·10시 기동 스케줄이 하는 일을 지금 수동으로 실행한다. stop-all.ps1 의 짝이다.

.DESCRIPTION
    start 스케줄을 전부 DISABLED 로 두고 필요할 때만 켜는 운용으로 바꾸면, 기동도 손으로
    해야 한다. 그때 대상을 하나씩 떠올려 aws 명령을 치면 꼭 하나를 빠뜨린다(특히 Neptune).
    Neptune 이 빠지면 화면은 떠 있는데 로그인만 실패해서 원인을 찾는 데 시간이 걸린다.

    **어떤 GPU 노드그룹을 켤지는 적어 두지 않고 기동 스케줄에서 읽는다.**
    이 클러스터에는 GPU 노드그룹이 여러 개 있고(옛것 포함) 실제로 쓰는 것은 하나다.
    이름을 여기에 또 적으면 노드그룹을 교체했을 때 두 곳이 어긋나고, 그러면 비어 있는
    노드그룹을 켜 놓고 "GPU 가 안 붙는다"를 디버깅하게 된다. schedules.ps1 -GpuNodegroup
    이 이미 그 스케줄을 단일 원본으로 쓰고 있으므로 같은 값을 본다.

.EXAMPLE
    # 전부 기동 (Neptune, 앱 노드, 배스천, GPU)
    powershell -NoProfile -ExecutionPolicy Bypass -File AI-POC/deploy/start-all.ps1

.EXAMPLE
    # GPU 없이. 화면·콘티·문서 작업만 할 때. 시간당 단가가 크게 다르다.
    powershell -NoProfile -ExecutionPolicy Bypass -File AI-POC/deploy/start-all.ps1 -NoGpu
#>
[CmdletBinding()]
param(
    # 기동 명령만 보내고 상태 확인 폴링을 건너뛴다.
    [switch]$NoWait,

    # GPU 노드그룹은 켜지 않는다. g6e.24xlarge 는 시간당 $18.5 로 나머지를 합친 것보다
    # 훨씬 비싸서, 영상 생성을 쓰지 않는 날은 빼는 것이 기본에 가깝다.
    [switch]$NoGpu
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$scriptDir = $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($scriptDir)) {
    $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
}

# --------------------------------------------------------------------------- #
# 이 스크립트는 더 이상 이 계정에서 동작하지 않는다. 실행을 막는다.
# --------------------------------------------------------------------------- #
# 아래 본문은 **원본 계정**의 스케줄 이름을 찾는다.
#   quick-test-eks-nodes-start / quick-test-eks-gpu-start
# 이 계정에는 EventBridge 스케줄이 **하나도 없다**(Phase 0 실측: 이 프로젝트와 무관한
# 항목 1건뿐). 그래서 Get-ScheduleTarget 이 전부 null 을 돌려주고, 앱 노드그룹은
# 존재하지 않는 `quick-test-eks-nodegroup` 으로 떨어진다.
#
# **문제는 그것이 오류로 보이지 않는다는 점이다.** Set-NodegroupDesired 는 조회
# 실패를 노란 경고 한 줄로 넘기고, 스크립트는 계속 진행해 "기동 완료" 흐름을 탄다.
# 즉 앱 노드는 0대 그대로인데 화면에는 성공처럼 보인다. 이 스크립트의 docstring 이
# "꼭 하나를 빠뜨린다(특히 Neptune)" 를 막겠다고 적어 놓고, 정작 자신이 앱 노드를
# 빠뜨리는 상태다.
#
# 대체 경로는 **아직 이 저장소에 없다.** Overlay 는 `terraform_infra/power.ps1`(대상
# 이름을 terraform output 에서 읽는다)로 대체했지만 그 폴더는 이매지너스 계정 전용이라
# 병합하지 않았다. 이 저장소의 기동/중지 자동화는 Terraform 모듈(EventBridge Scheduler)로
# 다시 만들 예정이고, 그때까지는 아래 안내대로 직접 조작한다.
Write-Host ''
Write-Host '이 스크립트는 사용하지 않습니다.' -ForegroundColor Yellow
Write-Host '원본 계정의 스케줄 이름(quick-test-*)을 찾기 때문에 앱 노드그룹을 켜지 못하고,' -ForegroundColor Yellow
Write-Host '그 실패가 경고 한 줄로만 지나가 "켜졌다"고 오해하게 됩니다.' -ForegroundColor Yellow
Write-Host ''
Write-Host '지금은 노드그룹을 직접 조작하세요:' -ForegroundColor Cyan
Write-Host '  aws eks update-nodegroup-config --cluster-name <cluster> --nodegroup-name <ng> \' -ForegroundColor Cyan
Write-Host '      --scaling-config minSize=1,maxSize=4,desiredSize=2 --profile ai-poc-hub' -ForegroundColor Cyan
Write-Host ''
Write-Host '상태 확인: aws eks list-nodegroups --cluster-name <cluster> --profile ai-poc-hub' -ForegroundColor DarkGray
Write-Host '기동/중지 스케줄은 Terraform 모듈로 도입할 예정입니다(변경 원장 L-42).' -ForegroundColor DarkGray
exit 2

# 아래는 참고용으로 남긴 옛 본문이다. 위 exit 로 도달하지 않는다.
. (Join-Path $scriptDir 'preflight.ps1') -Quiet
$config = $script:ToonConfig
$region = $config['AWS_REGION']
$cluster = $config['EKS_CLUSTER_NAME']
$bastion = $config['BASTION_INSTANCE_ID']
$neptune = $config['NEPTUNE_CLUSTER_ID']

Write-Host "계정 $($config['AWS_ACCOUNT_ID']) / 리전 $region"
Write-Host "지금(KST): $(Get-Date -Format 'yyyy-MM-dd HH:mm dddd')"

function Get-ScheduleTarget {
    <#
      기동 스케줄의 Target.Input 에서 노드그룹 이름과 목표 대수를 읽는다.
      스케줄이 없거나 형식이 다르면 빈 값을 돌려주고, 호출부가 기본값을 쓴다.
    #>
    param([string]$Name)
    $result = Invoke-AwsCli @('scheduler', 'get-schedule', '--name', $Name, '--region', $region, '--output', 'json')
    if ($result.ExitCode -ne 0) { return $null }
    $schedule = $result.Output | ConvertFrom-Json
    if ([string]::IsNullOrWhiteSpace($schedule.Target.Input)) { return $null }
    try { return $schedule.Target.Input | ConvertFrom-Json } catch { return $null }
}

function Set-NodegroupDesired {
    <#
      노드그룹을 원하는 대수로 올린다.

      maxSize 는 현재 값을 읽어 그대로 둔다. 여기에 숫자를 적으면 누군가 max 를 늘려 둔
      것을 이 스크립트가 조용히 되돌린다. desired 가 max 를 넘으면 API 가 거부하므로
      넘지 않도록 깎고, 깎았다는 사실을 알린다(조용히 적게 띄우면 원인을 못 찾는다).
    #>
    param([string]$Nodegroup, [int]$Desired)

    $cfg = Invoke-AwsCli @('eks', 'describe-nodegroup', '--cluster-name', $cluster,
        '--nodegroup-name', $Nodegroup, '--region', $region,
        '--query', 'nodegroup.scalingConfig', '--output', 'json')
    if ($cfg.ExitCode -ne 0) {
        Write-Host "  ${Nodegroup} : 조회 실패 - $(($cfg.Output | Out-String).Trim())" -ForegroundColor Yellow
        return
    }
    $scaling = $cfg.Output | ConvertFrom-Json
    $max = [int]$scaling.maxSize
    $want = $Desired
    if ($want -gt $max) {
        Write-Host "  ${Nodegroup} : maxSize 가 $max 이라 $Desired 대신 $max 로 올립니다." -ForegroundColor Yellow
        $want = $max
    }
    if ([int]$scaling.desiredSize -eq $want) {
        Write-Host "  ${Nodegroup} : 이미 $want 입니다" -ForegroundColor DarkGray
        return
    }
    $update = Invoke-AwsCli @('eks', 'update-nodegroup-config', '--cluster-name', $cluster,
        '--nodegroup-name', $Nodegroup,
        '--scaling-config', "minSize=0,maxSize=$max,desiredSize=$want",
        '--region', $region, '--query', 'update.status', '--output', 'text')
    if ($update.ExitCode -eq 0) {
        Write-Host "  ${Nodegroup} : desired $($scaling.desiredSize) -> $want ($($update.Output.Trim()))" -ForegroundColor Green
    }
    else {
        Write-Host "  ${Nodegroup} : 실패 - $(($update.Output | Out-String).Trim())" -ForegroundColor Red
    }
}

# --- 대상 결정 -------------------------------------------------------------
$nodesTarget = Get-ScheduleTarget 'quick-test-eks-nodes-start'
$appNodegroup = if ($nodesTarget -and $nodesTarget.NodegroupName) { [string]$nodesTarget.NodegroupName } else { 'quick-test-eks-nodegroup' }
$appDesired = if ($nodesTarget -and $nodesTarget.ScalingConfig -and $nodesTarget.ScalingConfig.DesiredSize) { [int]$nodesTarget.ScalingConfig.DesiredSize } else { 3 }

$gpuNodegroup = ''
$gpuDesired = 1
if (-not $NoGpu) {
    $gpuTarget = Get-ScheduleTarget 'quick-test-eks-gpu-start'
    if ($gpuTarget -and $gpuTarget.NodegroupName) {
        $gpuNodegroup = [string]$gpuTarget.NodegroupName
        if ($gpuTarget.ScalingConfig -and $gpuTarget.ScalingConfig.DesiredSize) {
            $gpuDesired = [int]$gpuTarget.ScalingConfig.DesiredSize
        }
    }
    else {
        Write-Host '경고: quick-test-eks-gpu-start 스케줄에서 노드그룹을 읽지 못해 GPU 를 건너뜁니다.' -ForegroundColor Yellow
    }
}

Write-Host ''
Write-Host '=== 대상 ===' -ForegroundColor Cyan
Write-Host "  Neptune     : $neptune"
Write-Host "  배스천      : $bastion"
Write-Host "  앱 노드그룹 : $appNodegroup (desired $appDesired)"
if ($gpuNodegroup) { Write-Host "  GPU 노드그룹: $gpuNodegroup (desired $gpuDesired)" }
else { Write-Host '  GPU 노드그룹: 건너뜀' }

# --- 기동 -----------------------------------------------------------------
Write-Host ''
Write-Host '=== 기동 ===' -ForegroundColor Cyan

# Neptune 을 가장 먼저 시작한다. available 까지 4~5분으로 가장 오래 걸리고, 이것이
# 늦으면 파드는 떠도 로그인이 실패한다. 다른 것을 기다리는 동안 함께 올라오게 한다.
$startDb = Invoke-AwsCli @('neptune', 'start-db-cluster', '--db-cluster-identifier', $neptune,
    '--region', $region, '--query', 'DBCluster.Status', '--output', 'text')
if ($startDb.ExitCode -eq 0) {
    Write-Host "  Neptune : $($startDb.Output.Trim())" -ForegroundColor Green
}
else {
    $text = ($startDb.Output | Out-String).Trim()
    if ($text -match 'InvalidDBClusterState') { Write-Host '  Neptune : 이미 켜져 있거나 기동 중입니다' -ForegroundColor DarkGray }
    else { Write-Host "  Neptune : 실패 - $text" -ForegroundColor Yellow }
}

$startEc2 = Invoke-AwsCli @('ec2', 'start-instances', '--instance-ids', $bastion, '--region', $region,
    '--query', 'StartingInstances[0].CurrentState.Name', '--output', 'text')
if ($startEc2.ExitCode -eq 0) {
    Write-Host "  배스천  : $($startEc2.Output.Trim())" -ForegroundColor Green
}
else {
    Write-Host "  배스천  : $(($startEc2.Output | Out-String).Trim())" -ForegroundColor Yellow
}

Set-NodegroupDesired -Nodegroup $appNodegroup -Desired $appDesired
if ($gpuNodegroup) { Set-NodegroupDesired -Nodegroup $gpuNodegroup -Desired $gpuDesired }

if ($NoWait) {
    Write-Host ''
    Write-Host 'Neptune 은 available 까지 4~5분, 파드 준비까지 몇 분 더 걸립니다.'
    exit 0
}

# --- 확인 -----------------------------------------------------------------
Write-Host ''
Write-Host '=== 상태 확인 ===' -ForegroundColor Cyan
$watch = @($appNodegroup)
if ($gpuNodegroup) { $watch += $gpuNodegroup }

for ($i = 1; $i -le 20; $i++) {
    Start-Sleep -Seconds 30
    $parts = @()
    $ready = $true
    foreach ($nodegroup in $watch) {
        # desired 만 보면 "요청은 됐지만 노드가 아직 없음"을 놓친다. 실제 Ready 노드 수는
        # kubectl 이 필요해 여기서는 보지 않고, 노드그룹 status 로 ACTIVE 여부만 본다.
        $info = Invoke-AwsCli @('eks', 'describe-nodegroup', '--cluster-name', $cluster,
            '--nodegroup-name', $nodegroup, '--region', $region,
            '--query', 'nodegroup.[scalingConfig.desiredSize,status]', '--output', 'text')
        $fields = @((($info.Output | Out-String).Trim() -split '\s+') | Where-Object { $_ })
        $parts += "$nodegroup=$($fields[0])/$($fields[1])"
        if ($fields[1] -ne 'ACTIVE') { $ready = $false }
    }
    $bst = (Invoke-AwsCli @('ec2', 'describe-instances', '--instance-ids', $bastion, '--region', $region,
        '--query', 'Reservations[0].Instances[0].State.Name', '--output', 'text')).Output.Trim()
    $nep = (Invoke-AwsCli @('neptune', 'describe-db-clusters', '--db-cluster-identifier', $neptune,
        '--region', $region, '--query', 'DBClusters[0].Status', '--output', 'text')).Output.Trim()
    Write-Host "  [$i] $($parts -join ' ') bastion=$bst neptune=$nep"
    if ($ready -and $bst -eq 'running' -and $nep -eq 'available') { break }
}

# 서비스가 실제로 응답하는지 본다. 노드가 떠도 파드가 Ready 가 되기까지 시간이 더 걸리고,
# 그 사이 ALB 는 504 를 준다. 여기까지 확인해야 "켜졌다"고 말할 수 있다.
Write-Host ''
Write-Host '=== 서비스 응답 ===' -ForegroundColor Cyan
$url = "https://$($config['APP_DOMAIN'])/api/health"
$ok = $false
for ($i = 1; $i -le 20; $i++) {
    try {
        $probe = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 15
        if ($probe.StatusCode -eq 200) {
            Write-Host "  [$i] HTTP 200 $(($probe.Content -replace '\s+', ' '))" -ForegroundColor Green
            $ok = $true
            break
        }
        Write-Host "  [$i] HTTP $($probe.StatusCode)"
    }
    catch {
        Write-Host "  [$i] 아직 응답하지 않습니다"
    }
    Start-Sleep -Seconds 20
}

Write-Host ''
if ($ok) {
    Write-Host "기동 완료: https://$($config['APP_DOMAIN'])" -ForegroundColor Green
}
else {
    Write-Host '서비스가 아직 응답하지 않습니다. 파드 상태를 확인하세요.' -ForegroundColor Yellow
    Write-Host '  powershell -File AI-POC/deploy/bastion.ps1' -ForegroundColor DarkGray
}
if ($gpuNodegroup) {
    Write-Host 'GPU 는 노드가 Ready 가 된 뒤 모델 가중치를 읽습니다. NEW_영상 제작 화면의 서버 칩으로 확인하세요.'
}
Write-Host '중지: powershell -File AI-POC/deploy/stop-all.ps1' -ForegroundColor DarkGray
exit 0
