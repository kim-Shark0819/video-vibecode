<#
.SYNOPSIS
    18시 중지 스케줄이 하는 일을 지금 수동으로 실행한다.

.DESCRIPTION
    작업이 18시 이후에 끝나면 그날의 중지 스케줄은 이미 지나가 있다. 그대로 두면 다음
    중지(다음날 18시)까지 자원이 계속 돌아 밤새 과금된다. GPU 노드는 시간당 단가가
    일반 노드의 수십 배라 이 차이가 크다.

    이 스크립트는 스케줄과 같은 대상에 같은 동작을 한다. 끝난 뒤 start 스케줄이
    ENABLED인지 확인해, 다음 평일 09시(Asia/Seoul)에 스스로 올라오는지 알려준다.

    quick-test-rds는 이 프로젝트가 쓰지 않아 건드리지 않는다.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File AI-POC/deploy/stop-all.ps1

.EXAMPLE
    # GPU 만 파킹하고 서비스는 계속 띄워 둔다. 단가 차이가 커서 이쪽만 내리는 경우가 많다.
    powershell -NoProfile -ExecutionPolicy Bypass -File AI-POC/deploy/stop-all.ps1 -GpuOnly
#>
[CmdletBinding()]
param(
    # 중지 명령만 보내고 상태 확인 폴링을 건너뛴다.
    [switch]$NoWait,

    # GPU 노드그룹만 내린다. 배스천·Neptune·앱 노드는 그대로 둔다.
    # GPU 단가가 나머지를 다 합친 것보다 훨씬 높아서(g6e.24xlarge $18.5/hr 대 t3.medium 3대
    # 약 $0.15/hr), 화면은 계속 띄워 두고 GPU 만 파킹하고 싶은 경우가 자주 있다.
    # 이 스위치가 없을 때는 임시 스크립트를 따로 만들어 돌렸는데, 그러면 노드그룹 이름이
    # 두 곳에 생겨 한쪽만 갱신되는 사고가 다시 열린다.
    [switch]$GpuOnly)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$scriptDir = $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($scriptDir)) {
    $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
}
. (Join-Path $scriptDir 'preflight.ps1') -Quiet
$config = $script:ToonConfig
$region = $config['AWS_REGION']
$cluster = $config['EKS_CLUSTER_NAME']
$bastion = $config['BASTION_INSTANCE_ID']
$neptune = $config['NEPTUNE_CLUSTER_ID']

Write-Host "계정 $($config['AWS_ACCOUNT_ID']) / 리전 $region"
Write-Host "지금(KST): $(Get-Date -Format 'yyyy-MM-dd HH:mm dddd')"

function Invoke-Step {
    param([string]$Label, [string[]]$Arguments)
    $result = Invoke-AwsCli ($Arguments + @('--region', $region))
    if ($result.ExitCode -eq 0) {
        Write-Host "  $Label : 요청 완료" -ForegroundColor Green
    }
    else {
        # 이미 내려가 있으면 InvalidState 계열 오류가 난다. 실패로 볼 이유가 없다.
        $text = ($result.Output | Out-String).Trim()
        if ($text -match 'InvalidDBClusterState|IncorrectInstanceState|InvalidParameterCombination') {
            Write-Host "  $Label : 이미 내려가 있습니다" -ForegroundColor DarkGray
        }
        else {
            Write-Host "  $Label : 실패 - $text" -ForegroundColor Yellow
        }
    }
}

Write-Host ''
Write-Host '=== 대상 노드그룹 ===' -ForegroundColor Cyan
<#
  노드그룹 이름을 **적어 두지 않고 클러스터에서 조회한다.**

  예전에는 'quick-test-eks-gpu' 와 'quick-test-eks-nodegroup' 두 개를 적어 두었다.
  그 뒤 GPU 노드그룹이 g6e24 / g6e12 로 늘어났는데, 옛 이름도 desired 0 인 상태로
  남아 있어서 이 스크립트는 **오류 없이 "요청 완료" 를 찍고 새 GPU 노드 두 대를
  그대로 켜 두었다.** 실패로 보이지 않는다는 점이 가장 나쁘다 (g6e.24xlarge $18.5/hr,
  g6e.12xlarge $12.9/hr). 조회하면 노드그룹을 새로 만들어도 빠뜨릴 수 없다.

  maxSize 는 노드그룹마다 다르므로(GPU 1, 일반 3) 현재 값을 읽어 그대로 둔다.
  적어 두면 다음 사람이 max 를 늘렸을 때 이 스크립트가 조용히 되돌린다.
#>
$listed = Invoke-AwsCli @('eks', 'list-nodegroups', '--cluster-name', $cluster,
    '--region', $region, '--query', 'nodegroups', '--output', 'text')
if ($listed.ExitCode -ne 0) {
    throw "노드그룹 목록을 조회하지 못했습니다: $(($listed.Output | Out-String).Trim())"
}
$nodegroups = @(($listed.Output -split '\s+') | Where-Object { $_ })
if ($nodegroups.Count -eq 0) { throw '클러스터에 노드그룹이 없습니다. 이름이나 리전을 확인하세요.' }
if ($GpuOnly) {
    $nodegroups = @($nodegroups | Where-Object { $_ -like '*gpu*' })
    if ($nodegroups.Count -eq 0) { throw 'GPU 노드그룹을 찾지 못했습니다.' }
    Write-Host '  -GpuOnly: GPU 노드그룹만 내립니다 (배스천·Neptune·앱 노드는 유지).'
}
Write-Host "  $($nodegroups -join ', ')"

Write-Host ''
Write-Host '=== 중지 ===' -ForegroundColor Cyan
# GPU 노드를 먼저 내린다. 단가가 가장 높아 1분이라도 빨리 내리는 것이 이득이다.
foreach ($nodegroup in ($nodegroups | Sort-Object { $_ -notlike '*gpu*' })) {
    $cfg = Invoke-AwsCli @('eks', 'describe-nodegroup', '--cluster-name', $cluster,
        '--nodegroup-name', $nodegroup, '--region', $region,
        '--query', 'nodegroup.scalingConfig', '--output', 'json')
    if ($cfg.ExitCode -ne 0) {
        Write-Host "  $nodegroup : 조회 실패 - $(($cfg.Output | Out-String).Trim())" -ForegroundColor Yellow
        continue
    }
    $scaling = $cfg.Output | ConvertFrom-Json
    if ([int]$scaling.desiredSize -eq 0 -and [int]$scaling.minSize -eq 0) {
        Write-Host "  $nodegroup : 이미 0입니다" -ForegroundColor DarkGray
        continue
    }
    Invoke-Step "$nodegroup (desired $($scaling.desiredSize) -> 0)" @('eks', 'update-nodegroup-config',
        '--cluster-name', $cluster, '--nodegroup-name', $nodegroup,
        '--scaling-config', "minSize=0,maxSize=$([int]$scaling.maxSize),desiredSize=0",
        '--query', 'update.status', '--output', 'text')
}
if (-not $GpuOnly) {
    Invoke-Step '배스천 EC2' @('ec2', 'stop-instances', '--instance-ids', $bastion,
        '--query', 'StoppingInstances[0].CurrentState.Name', '--output', 'text')
    Invoke-Step 'Neptune 클러스터' @('neptune', 'stop-db-cluster', '--db-cluster-identifier', $neptune,
        '--query', 'DBCluster.Status', '--output', 'text')
}

if (-not $NoWait) {
    Write-Host ''
    Write-Host '=== 상태 확인 ===' -ForegroundColor Cyan
    for ($i = 1; $i -le 12; $i++) {
        Start-Sleep -Seconds 30
        # 조회한 노드그룹 전부를 확인한다. 두 개만 보면 나머지가 남아도 "완료"로 보인다.
        $parts = @()
        $allZero = $true
        foreach ($nodegroup in $nodegroups) {
            $desired = (Invoke-AwsCli @('eks', 'describe-nodegroup', '--cluster-name', $cluster,
                '--nodegroup-name', $nodegroup, '--region', $region,
                '--query', 'nodegroup.scalingConfig.desiredSize', '--output', 'text')).Output.Trim()
            $parts += "$nodegroup=$desired"
            if ($desired -ne '0') { $allZero = $false }
        }
        # -GpuOnly 에서는 배스천과 Neptune 을 일부러 살려 두므로 완료 조건에 넣지 않는다.
        # 넣으면 절대 만족되지 않아 12번을 다 돌고 6분을 버린다.
        if ($GpuOnly) {
            Write-Host "  [$i] $($parts -join ' ')"
            if ($allZero) { break }
            continue
        }
        $bst = (Invoke-AwsCli @('ec2', 'describe-instances', '--instance-ids', $bastion, '--region', $region, '--query', 'Reservations[0].Instances[0].State.Name', '--output', 'text')).Output.Trim()
        $nep = (Invoke-AwsCli @('neptune', 'describe-db-clusters', '--db-cluster-identifier', $neptune, '--region', $region, '--query', 'DBClusters[0].Status', '--output', 'text')).Output.Trim()
        Write-Host "  [$i] $($parts -join ' ') bastion=$bst neptune=$nep"
        if ($allZero -and $bst -eq 'stopped' -and $nep -eq 'stopped') { break }
    }

    # desired 0 은 요청이 접수된 것이고 인스턴스 종료는 몇 분 더 걸린다. 실제로 남아
    # 있는 것이 없는지 EC2 쪽에서 한 번 더 본다. 여기서 GPU 인스턴스가 보이면
    # 노드그룹은 0인데 인스턴스가 아직 사는 중이거나, 노드그룹 밖에서 뜬 것이다.
    Write-Host ''
    Write-Host '=== 아직 running 인 EC2 ===' -ForegroundColor Cyan
    # 노드그룹 이름을 함께 뽑는다. -GpuOnly 에서는 배스천과 앱 노드를 **일부러** 살려 두므로
    # 그것들을 "종료 중일 수 있습니다" 로 보여 주면 안 내려간 것처럼 읽힌다.
    # 이 목록은 이번에 내리라고 지시한 노드그룹의 인스턴스만 담아야 뜻이 분명하다.
    $running = Invoke-AwsCli @('ec2', 'describe-instances', '--region', $region,
        '--filters', 'Name=instance-state-name,Values=running,pending',
        '--query', ('Reservations[].Instances[].[InstanceId,InstanceType,' +
                    'Tags[?Key==`Name`].Value|[0],Tags[?Key==`eks:nodegroup-name`].Value|[0]]'),
        '--output', 'text')
    $runningText = ($running.Output | Out-String).Trim()
    if ($GpuOnly -and -not [string]::IsNullOrWhiteSpace($runningText)) {
        $kept = @(($runningText -split "`n") | Where-Object {
            $columns = $_ -split "`t"
            $columns.Count -ge 4 -and ($nodegroups -contains $columns[3].Trim())
        })
        $runningText = ($kept -join "`n").Trim()
    }
    if ([string]::IsNullOrWhiteSpace($runningText)) {
        Write-Host '  없습니다.' -ForegroundColor Green
    }
    else {
        Write-Host $runningText
        Write-Host '  종료 중일 수 있습니다. 몇 분 뒤 다시 확인하세요.' -ForegroundColor Yellow
    }
}

Write-Host ''
Write-Host '=== 다음 기동 ===' -ForegroundColor Cyan
$rows = @()
foreach ($name in @('quick-test-eks-gpu-start', 'quick-test-eks-nodes-start', 'quick-test-bastion-start', 'toon-change-neptune-start')) {
    $result = Invoke-AwsCli @('scheduler', 'get-schedule', '--name', $name, '--region', $region, '--output', 'json')
    if ($result.ExitCode -ne 0) {
        $rows += [pscustomobject]@{ 스케줄 = $name; 상태 = '없음'; 일정 = '-' }
        continue
    }
    $schedule = $result.Output | ConvertFrom-Json
    $rows += [pscustomobject]@{ 스케줄 = $name; 상태 = $schedule.State; 일정 = "$($schedule.ScheduleExpression) $($schedule.ScheduleExpressionTimezone)" }
}
$rows | Format-Table -AutoSize | Out-String -Width 200 | Write-Host

$disabled = @($rows | Where-Object { $_.상태 -ne 'ENABLED' })
if ($disabled.Count -gt 0) {
    <#
      start 가 꺼져 있는 것은 **정상 상태일 수 있다.** 필요할 때만 손으로 켜는 운용으로
      바꾸면 이것이 기본이 된다.

      예전에는 여기서 exit 1 을 냈다. 그러면 중지는 전부 성공했는데도 스크립트가 실패로
      끝나고, 호출하는 쪽(사람이든 나중의 자동화든)이 "자원이 안 내려갔다"고 읽는다.
      종료 코드는 이 스크립트가 하는 일, 즉 중지의 성패만 나타내야 한다.
      자동 기동 여부는 정책이라 알려 주기만 한다.
    #>
    Write-Host "안내: start 스케줄 $($disabled.Count)개가 DISABLED입니다. 다음 영업일에 자동 기동되지 않습니다." -ForegroundColor Yellow
    foreach ($row in $disabled) { Write-Host "  - $($row.스케줄)" -ForegroundColor DarkGray }
    Write-Host "  필요할 때 켜기: powershell -File AI-POC/deploy/schedules.ps1 -Enable start" -ForegroundColor DarkGray
    Write-Host "  하나만 켜기:   powershell -File AI-POC/deploy/schedules.ps1 -Enable quick-test-eks-gpu-start" -ForegroundColor DarkGray
    Write-Host ''
    Write-Host '중지는 정상 완료했습니다.' -ForegroundColor Green
    exit 0
}

# 다음 평일 기동 시각을 계산해 알려준다. 토·일에 끝낸 경우 월요일로 넘어간다.
# GPU 는 10시, 나머지는 9시라 -GpuOnly 일 때 9시라고 적으면 한 시간 일찍 온다고 말하는 셈이다.
$startHour = if ($GpuOnly) { 10 } else { 9 }
$next = (Get-Date).Date.AddDays(1).AddHours($startHour)
while ($next.DayOfWeek -in @([DayOfWeek]::Saturday, [DayOfWeek]::Sunday)) {
    $next = $next.AddDays(1)
}
Write-Host "모든 start 스케줄이 ENABLED입니다." -ForegroundColor Green
Write-Host "다음 자동 기동: $($next.ToString('yyyy-MM-dd(dddd) HH:mm')) KST" -ForegroundColor Green
if (-not $GpuOnly) {
    Write-Host "Neptune은 available까지 4~5분 걸립니다. 그 전에는 로그인이 실패합니다."
}
exit 0
