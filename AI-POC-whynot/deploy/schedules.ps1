<#
.SYNOPSIS
    비용 절감용 시작·중지 스케줄의 상태를 보거나 바꾼다.

.DESCRIPTION
    EventBridge Scheduler는 State만 따로 바꾸는 API가 없다. update-schedule이 전체 정의를
    다시 받으므로, get-schedule 결과에서 읽기 전용 필드만 걷어내고 State를 바꿔 되돌려 넣는다.

    수동으로 하면 Target.Input(JSON 문자열)이 깨지기 쉬워 스크립트로 묶었다.

.EXAMPLE
    # 현재 상태만 본다
    powershell -NoProfile -ExecutionPolicy Bypass -File AI-POC/deploy/schedules.ps1

.EXAMPLE
    # 18시 중지 스케줄을 켠다
    powershell -NoProfile -ExecutionPolicy Bypass -File AI-POC/deploy/schedules.ps1 -Enable stop

.EXAMPLE
    # 롱테이크처럼 몇 시간 걸리는 작업을 돌리기 전에 중지 스케줄을 끈다
    powershell -NoProfile -ExecutionPolicy Bypass -File AI-POC/deploy/schedules.ps1 -Disable stop

.EXAMPLE
    # GPU 기동 시각을 평일 10시로 바꾼다
    powershell ... -File AI-POC/deploy/schedules.ps1 `
        -ScheduleName quick-test-eks-gpu-start -Cron 'cron(0 10 ? * MON-FRI *)'

.EXAMPLE
    # 60분 뒤에 한 번만 자원을 내린다. 상시 18시 스케줄은 그대로 둔다.
    powershell ... -File AI-POC/deploy/schedules.ps1 -StopInMinutes 60
#>
[CmdletBinding()]
param(
    # 'stop' / 'start' / 'all' 또는 **스케줄 이름 하나**.
    # 이름을 받는 이유: GPU 노드그룹이 둘이고 한쪽이 유휴일 때가 있다. 종류로만 끄고 켤 수
    # 있으면 쓰지 않는 노드그룹의 start 를 파킹할 방법이 없어, 다음 평일마다 시간당
    # $12.9 가 그냥 붙는다. 종류 단위로만 다루면 그 비용을 피할 수단이 아예 없다.
    [string]$Enable,

    [string]$Disable,

    # 시각을 바꿀 스케줄 이름. -Cron 과 함께 쓴다.
    [string]$ScheduleName,

    # 새 일정 표현식. 예: 'cron(0 10 ? * MON-FRI *)'
    [string]$Cron,

    # 이 프로젝트의 **모든** 중지 스케줄 시각을 한 번에 바꾼다.
    # 중지 대상이 다섯 개(Neptune, 앱 노드, GPU 노드 2종, 배스천)라 -ScheduleName 으로
    # 하나씩 고치면 다섯 번 실행해야 하고, 하나를 빠뜨리면 그 자원만 계속 돌아간다.
    # 값은 시(0~23) 또는 cron 표현식. 예: -StopHour 23  또는  -StopHour 'cron(30 22 ? * * *)'
    [string]$StopHour,

    # N분 뒤에 한 번만 실행되는 중지 스케줄을 만든다. 실행 후 스스로 삭제된다.
    [int]$StopInMinutes,

    # -StopInMinutes 로 만든 1회성 스케줄을 지운다(예정 시각 전에 취소할 때).
    [switch]$CancelOnce,

    # GPU 시작·중지 스케줄이 가리키는 노드그룹을 바꾼다.
    # 스케줄 Target.Input 에 NodegroupName 이 박혀 있어서, 노드그룹을 새로 만들면
    # 스케줄이 옛 것을 계속 가리킨다. 그러면 새 GPU 노드가 18시에 내려가지 않아
    # 시간당 단가가 그대로 24시간 과금된다(g6e.24xlarge 는 $18.5/hr).
    # 예: -GpuNodegroup quick-test-eks-gpu-g6e24
    [string]$GpuNodegroup
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$scriptDir = $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($scriptDir)) {
    $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
}

. (Join-Path $scriptDir 'preflight.ps1') -Quiet
$config = $script:ToonConfig
$region = $config['AWS_REGION']

# 이 계정에서 켜고 끌 수 있는 자원의 스케줄. 이름은 EventBridge Scheduler에 등록된 값이다.
$known = @(
    @{ Name = 'toon-change-neptune-start';   Kind = 'start'; Resource = 'Neptune toon-change-ai-pj-graph' }
    @{ Name = 'toon-change-neptune-stop';    Kind = 'stop';  Resource = 'Neptune toon-change-ai-pj-graph' }
    @{ Name = 'quick-test-eks-nodes-start';  Kind = 'start'; Resource = 'EKS 노드그룹 (t3.medium 3대)' }
    @{ Name = 'quick-test-eks-nodes-stop';   Kind = 'stop';  Resource = 'EKS 노드그룹 (t3.medium 3대)' }
    # GPU 노드그룹이 둘이다. 스케줄 하나는 Target.Input 에 NodegroupName 을 하나만 담으므로
    # 노드그룹마다 짝이 필요하다. 목록에서 빠지면 상태 표와 start/stop 짝 경고에서 사라지고,
    # 그러면 stop 이 꺼진 것을 아무도 모른 채 시간당 단가가 24시간 과금된다.
    @{ Name = 'quick-test-eks-gpu2-start';   Kind = 'start'; Resource = 'GPU 노드그룹 2 (영상 생성)' }
    @{ Name = 'quick-test-eks-gpu2-stop';    Kind = 'stop';  Resource = 'GPU 노드그룹 2 (영상 생성)' }
    # GPU 노드는 시간당 단가가 일반 노드의 수십 배라 스케줄에서 빠지면 비용이 바로 튄다.
    # 어떤 노드그룹을 가리키는지는 Target.Input 에 있으므로 아래 표에 적지 않는다.
    # 적어 두면 노드그룹을 바꿨을 때 이 문서가 거짓말을 한다(실제로 g6e.4xlarge 로
    # 적혀 있었는데 노드그룹이 g6e.24xlarge 로 바뀌었다). 실행 시 Input 에서 읽어 보여준다.
    @{ Name = 'quick-test-eks-gpu-start';    Kind = 'start'; Resource = 'GPU 노드그룹 (영상 생성)' }
    @{ Name = 'quick-test-eks-gpu-stop';     Kind = 'stop';  Resource = 'GPU 노드그룹 (영상 생성)' }
    @{ Name = 'quick-test-bastion-start';    Kind = 'start'; Resource = '배스천 EC2 (t3.medium)' }
    @{ Name = 'quick-test-bastion-stop';     Kind = 'stop';  Resource = '배스천 EC2 (t3.medium)' }
    # 이 프로젝트와 무관한 자원이다. 상태는 보여 주되 일괄 변경 대상에서는 뺀다
    # (작업 규칙: config.env 에 명시되지 않은 기존 리소스는 건드리지 않는다).
    @{ Name = 'quick-test-rds-start';        Kind = 'start'; Resource = 'Aurora MySQL quick-test-rds (프로젝트 외부)'; External = $true }
    @{ Name = 'quick-test-rds-stop';         Kind = 'stop';  Resource = 'Aurora MySQL quick-test-rds (프로젝트 외부)'; External = $true }
)

function Get-ScheduleState {
    param([string]$Name)
    $result = Invoke-AwsCli @('scheduler', 'get-schedule', '--name', $Name, '--region', $region, '--output', 'json')
    if ($result.ExitCode -ne 0) { return $null }
    return $result.Output | ConvertFrom-Json
}

function Write-CliInputJson {
    <#
      AWS CLI에 넘길 --cli-input-json 파일을 쓴다.

      한글이 든 값(예: Description)을 그대로 쓰면 CLI가 파일을 디코드하지 못한다.
      BOM 없는 UTF-8로 저장해도 마찬가지였다. 실제 오류는 이렇게 난다.

        Error parsing parameter '--cli-input-json': Unable to load paramfile (...),
        text contents could not be decoded.

      CLI가 Windows 로컬 코드페이지(cp949)로 먼저 읽으려 하기 때문이다.
      그래서 ASCII 밖의 문자를 JSON 표준 \uXXXX 이스케이프로 바꿔 순수 ASCII로 저장한다.
      JSON 규격상 동등하고 어떤 로케일에서도 디코드된다.
    #>
    param([string]$Path, $Body)

    $json = $Body | ConvertTo-Json -Depth 12 -Compress
    $builder = [Text.StringBuilder]::new()
    foreach ($char in $json.ToCharArray()) {
        if ([int]$char -lt 128) {
            [void]$builder.Append($char)
        }
        else {
            [void]$builder.AppendFormat('\u{0:x4}', [int]$char)
        }
    }
    [IO.File]::WriteAllText($Path, $builder.ToString(), (New-Object Text.UTF8Encoding($false)))
}

function Set-ScheduleState {
    param([string]$Name, [ValidateSet('ENABLED', 'DISABLED')][string]$State)

    $schedule = Get-ScheduleState -Name $Name
    if (-not $schedule) {
        Write-Host "  ${Name}: 찾을 수 없습니다. 건너뜁니다." -ForegroundColor Yellow
        return $false
    }
    if ($schedule.State -eq $State) {
        Write-Host "  ${Name}: 이미 $State 입니다."
        return $false
    }

    # 읽기 전용 필드는 update-schedule이 거부한다.
    foreach ($field in @('Arn', 'CreationDate', 'LastModificationDate')) {
        if ($schedule.PSObject.Properties.Name -contains $field) {
            $schedule.PSObject.Properties.Remove($field)
        }
    }
    $schedule.State = $State

    $inputPath = Join-Path $env:TEMP "schedule-$Name.json"
    Write-CliInputJson -Path $inputPath -Body $schedule

    $update = Invoke-AwsCli @('scheduler', 'update-schedule', '--region', $region, '--cli-input-json', "file://$inputPath")
    Remove-Item -Force -ErrorAction SilentlyContinue $inputPath
    if ($update.ExitCode -ne 0) {
        throw "${Name} 상태를 바꾸지 못했습니다: $($update.Output)"
    }
    Write-Host "  ${Name}: -> $State" -ForegroundColor Green
    return $true
}

function Get-ScheduleNodegroup {
    <# Target.Input 에서 NodegroupName 을 읽는다. 없으면 빈 문자열. #>
    param($Schedule)
    if (-not $Schedule -or -not $Schedule.Target -or [string]::IsNullOrWhiteSpace($Schedule.Target.Input)) {
        return ''
    }
    try {
        $payload = $Schedule.Target.Input | ConvertFrom-Json
    }
    catch {
        return ''
    }
    if ($payload.PSObject.Properties.Name -contains 'NodegroupName') { return [string]$payload.NodegroupName }
    return ''
}

function Set-ScheduleNodegroup {
    <#
      GPU 스케줄이 가리키는 노드그룹을 바꾼다.

      Target.Input 은 JSON **문자열**이다. 객체로 다루다가 다시 문자열로 넣어야 하는데,
      여기서 형식이 조금이라도 어긋나면 스케줄이 조용히 실패한다(실행 시각에 아무 일도
      일어나지 않고, 그 사실은 다음 청구서에서 드러난다). 그래서 ScalingConfig 같은
      다른 필드는 손대지 않고 NodegroupName 만 바꿔 되돌려 넣는다.
    #>
    param([string]$Name, [string]$Nodegroup)

    $schedule = Get-ScheduleState -Name $Name
    if (-not $schedule) { throw "${Name} 스케줄을 찾을 수 없습니다." }

    $before = Get-ScheduleNodegroup -Schedule $schedule
    if ($before -eq $Nodegroup) {
        Write-Host "  ${Name}: 이미 $Nodegroup 을 가리킵니다."
        return $false
    }
    if ([string]::IsNullOrWhiteSpace($before)) {
        throw "${Name} 의 Target.Input 에 NodegroupName 이 없습니다. 이 스케줄은 노드그룹을 다루지 않습니다."
    }

    $payload = $schedule.Target.Input | ConvertFrom-Json
    $payload.NodegroupName = $Nodegroup
    $schedule.Target.Input = ($payload | ConvertTo-Json -Depth 8 -Compress)

    foreach ($field in @('Arn', 'CreationDate', 'LastModificationDate')) {
        if ($schedule.PSObject.Properties.Name -contains $field) {
            $schedule.PSObject.Properties.Remove($field)
        }
    }

    $inputPath = Join-Path $env:TEMP "schedule-ng-$Name.json"
    Write-CliInputJson -Path $inputPath -Body $schedule
    $update = Invoke-AwsCli @('scheduler', 'update-schedule', '--region', $region, '--cli-input-json', "file://$inputPath")
    Remove-Item -Force -ErrorAction SilentlyContinue $inputPath
    if ($update.ExitCode -ne 0) {
        throw "${Name} 의 노드그룹을 바꾸지 못했습니다: $($update.Output)"
    }
    Write-Host "  ${Name}: $before -> $Nodegroup" -ForegroundColor Green
    return $true
}

function Set-ScheduleCron {
    <#
      일정 표현식만 바꾼다. State 변경과 같은 이유로 전체 정의를 다시 넣어야 한다.
      시간대(ScheduleExpressionTimezone)는 기존 값을 유지한다. 이 계정은 전부 Asia/Seoul이다.
    #>
    param([string]$Name, [string]$Expression)

    $schedule = Get-ScheduleState -Name $Name
    if (-not $schedule) { throw "${Name} 스케줄을 찾을 수 없습니다." }
    $before = $schedule.ScheduleExpression
    if ($before -eq $Expression) {
        Write-Host "  ${Name}: 이미 $Expression 입니다."
        return $false
    }

    foreach ($field in @('Arn', 'CreationDate', 'LastModificationDate')) {
        if ($schedule.PSObject.Properties.Name -contains $field) {
            $schedule.PSObject.Properties.Remove($field)
        }
    }
    $schedule.ScheduleExpression = $Expression

    $inputPath = Join-Path $env:TEMP "schedule-cron-$Name.json"
    Write-CliInputJson -Path $inputPath -Body $schedule
    $update = Invoke-AwsCli @('scheduler', 'update-schedule', '--region', $region, '--cli-input-json', "file://$inputPath")
    Remove-Item -Force -ErrorAction SilentlyContinue $inputPath
    if ($update.ExitCode -ne 0) {
        throw "${Name} 일정을 바꾸지 못했습니다: $($update.Output)"
    }
    Write-Host "  ${Name}: $before -> $Expression" -ForegroundColor Green
    return $true
}

# 1회성 중지에 포함할 스케줄. quick-test-rds 는 이 프로젝트와 무관해서 제외한다
# (작업 규칙: config.env에 명시되지 않은 기존 리소스는 건드리지 않는다).
$onceSources = @(
    'quick-test-eks-gpu-stop'
    'quick-test-eks-nodes-stop'
    'quick-test-bastion-stop'
    'toon-change-neptune-stop'
)
$onceSuffix = '-once'

function Remove-OnceSchedules {
    <# 1회성 스케줄을 지운다. 이미 실행돼 스스로 삭제된 것은 조용히 넘어간다. #>
    $removed = 0
    foreach ($source in $onceSources) {
        $name = "$source$onceSuffix"
        $result = Invoke-AwsCli @('scheduler', 'delete-schedule', '--name', $name, '--region', $region)
        if ($result.ExitCode -eq 0) {
            Write-Host "  ${name}: 삭제" -ForegroundColor Yellow
            $removed++
        }
    }
    return $removed
}

function New-OnceStopSchedules {
    <#
      상시 18시 스케줄을 건드리지 않고, N분 뒤에 한 번만 도는 중지 스케줄을 만든다.

      상시 스케줄의 cron을 임시로 옮기는 방법은 쓰지 않는다. 되돌리는 것을 잊으면
      매일 그 시각에 자원이 내려가고, 그 사실을 아무도 모른다.

      ActionAfterCompletion=DELETE 로 실행 후 스스로 사라지게 한다. 남겨 두면
      다음에 같은 이름을 만들 수 없고 목록도 지저분해진다.
    #>
    param([int]$Minutes)

    $runAt = (Get-Date).AddMinutes($Minutes)
    # at() 는 초까지 받는다. 분 단위로 떨어뜨려 예측 가능하게 만든다.
    $stamp = $runAt.ToString('yyyy-MM-ddTHH:mm:00')
    Write-Host ("  실행 예정: {0} (약 {1}분 뒤, Asia/Seoul)" -f $runAt.ToString('yyyy-MM-dd HH:mm'), $Minutes)

    # 같은 이름이 남아 있으면 먼저 지운다(앞선 요청이 취소된 경우).
    Remove-OnceSchedules | Out-Null

    $created = 0
    foreach ($source in $onceSources) {
        $schedule = Get-ScheduleState -Name $source
        if (-not $schedule) {
            Write-Host "  ${source}: 찾을 수 없어 건너뜁니다." -ForegroundColor Yellow
            continue
        }
        $name = "$source$onceSuffix"
        $body = [ordered]@{
            Name                        = $name
            GroupName                   = $schedule.GroupName
            ScheduleExpression          = "at($stamp)"
            ScheduleExpressionTimezone  = $schedule.ScheduleExpressionTimezone
            State                       = 'ENABLED'
            Description                 = "1회성 중지 ($source 와 같은 동작). 실행 후 자동 삭제됩니다."
            FlexibleTimeWindow          = $schedule.FlexibleTimeWindow
            Target                      = $schedule.Target
            ActionAfterCompletion       = 'DELETE'
        }
        $inputPath = Join-Path $env:TEMP "schedule-once-$name.json"
        Write-CliInputJson -Path $inputPath -Body $body
        $create = Invoke-AwsCli @('scheduler', 'create-schedule', '--region', $region, '--cli-input-json', "file://$inputPath")
        Remove-Item -Force -ErrorAction SilentlyContinue $inputPath
        if ($create.ExitCode -ne 0) {
            throw "${name} 을 만들지 못했습니다: $($create.Output)"
        }
        Write-Host "  ${name}: at($stamp)" -ForegroundColor Green
        $created++
    }
    return $created
}

function Select-Names {
    <#
      일괄 활성/비활성 대상. **External 로 표시한 것은 제외한다.**

      quick-test-rds-* 는 이 프로젝트와 무관한 자원이다. -Disable stop 이 그것까지 껐으면
      남의 DB 가 24시간 돌게 된다. 작업 규칙(config.env 에 없는 리소스는 건드리지 않는다)에
      어긋나고, 되돌려야 하는지도 알 수 없다. -StopHour 도 같은 기준으로 제외한다.
    #>
    param([string]$Kind)

    # 스케줄 이름을 그대로 준 경우. 이름은 알려진 목록 안에 있어야 한다. 오타를 그대로
    # 넘기면 Set-ScheduleState 가 "찾을 수 없습니다" 만 찍고 조용히 지나가서,
    # 껐다고 생각한 스케줄이 그대로 살아 있게 된다.
    $exact = $known | Where-Object { $_.Name -eq $Kind }
    if ($exact) {
        if ($exact.External) {
            throw "$Kind 는 이 프로젝트와 무관한 자원입니다. 직접 바꾸지 않습니다."
        }
        return @($exact.Name)
    }

    if ($Kind -notin @('stop', 'start', 'all')) {
        throw ("'$Kind' 은 stop / start / all 도 아니고 알려진 스케줄 이름도 아닙니다. " +
               "이름 목록: " + (($known | Where-Object { -not $_.External }).Name -join ', '))
    }

    $target = $known | Where-Object { -not $_.External }
    if ($Kind -eq 'all') { return @($target.Name) }
    return @(($target | Where-Object { $_.Kind -eq $Kind }).Name)
}

Write-Host "계정 $($config['AWS_ACCOUNT_ID']) / 리전 $region"

$changed = 0
if ($ScheduleName -or $Cron) {
    if (-not ($ScheduleName -and $Cron)) {
        throw '-ScheduleName 과 -Cron 은 함께 지정해야 합니다.'
    }
    Write-Host ''
    Write-Host '=== 일정 변경 ===' -ForegroundColor Cyan
    if (Set-ScheduleCron -Name $ScheduleName -Expression $Cron) { $changed++ }
}
if ($StopHour) {
    Write-Host ''
    Write-Host '=== 중지 시각 일괄 변경 ===' -ForegroundColor Cyan
    # 숫자만 주면 매일 정시로 만든다. cron 을 직접 주면 그대로 쓴다.
    if ($StopHour -match '^\d{1,2}$') {
        $hour = [int]$StopHour
        if ($hour -lt 0 -or $hour -gt 23) { throw "-StopHour 는 0~23 사이여야 합니다: $StopHour" }
        $expression = "cron(0 $hour ? * * *)"
    }
    elseif ($StopHour -like 'cron(*') {
        $expression = $StopHour
    }
    else {
        throw "-StopHour 는 0~23 숫자 또는 cron(...) 표현식이어야 합니다: $StopHour"
    }
    Write-Host "  적용할 일정: $expression (Asia/Seoul)"
    foreach ($item in ($known | Where-Object { $_.Kind -eq 'stop' -and -not $_.External })) {
        try {
            if (Set-ScheduleCron -Name $item.Name -Expression $expression) { $changed++ }
        }
        catch {
            # 하나가 실패해도 나머지는 바꾼다. 중간에 멈추면 자원마다 시각이 갈린다.
            Write-Host "  $($item.Name): 실패 - $($_.Exception.Message)" -ForegroundColor Red
        }
    }
}
if ($GpuNodegroup) {
    Write-Host ''
    Write-Host '=== GPU 스케줄 대상 노드그룹 변경 ===' -ForegroundColor Cyan
    # 실제로 있는 노드그룹인지 먼저 본다. 없는 이름을 넣으면 스케줄이 실행 시각에
    # 조용히 실패하고, 그 사실은 다음 청구서에서 드러난다.
    $check = Invoke-AwsCli @('eks', 'describe-nodegroup', '--cluster-name', $config['EKS_CLUSTER_NAME'],
        '--nodegroup-name', $GpuNodegroup, '--region', $region, '--query', 'nodegroup.status', '--output', 'text')
    if ($check.ExitCode -ne 0) {
        throw "노드그룹 $GpuNodegroup 을 찾을 수 없습니다. 이름을 확인하세요."
    }
    Write-Host "  대상 확인: $GpuNodegroup ($($check.Output.Trim()))"
    foreach ($name in @('quick-test-eks-gpu-start', 'quick-test-eks-gpu-stop')) {
        if (Set-ScheduleNodegroup -Name $name -Nodegroup $GpuNodegroup) { $changed++ }
    }
}
if ($CancelOnce) {
    Write-Host ''
    Write-Host '=== 1회성 중지 취소 ===' -ForegroundColor Cyan
    $removed = Remove-OnceSchedules
    if ($removed -eq 0) { Write-Host '  예약된 1회성 중지가 없습니다.' }
    $changed += $removed
}
if ($StopInMinutes -gt 0) {
    Write-Host ''
    Write-Host '=== 1회성 중지 예약 ===' -ForegroundColor Cyan
    $changed += New-OnceStopSchedules -Minutes $StopInMinutes
}
if ($Disable) {
    Write-Host ''
    Write-Host "=== 비활성화: $Disable ===" -ForegroundColor Cyan
    foreach ($name in Select-Names $Disable) {
        if (Set-ScheduleState -Name $name -State 'DISABLED') { $changed++ }
    }
}
if ($Enable) {
    Write-Host ''
    Write-Host "=== 활성화: $Enable ===" -ForegroundColor Cyan
    foreach ($name in Select-Names $Enable) {
        if (Set-ScheduleState -Name $name -State 'ENABLED') { $changed++ }
    }
}

Write-Host ''
Write-Host "=== 현재 상태 ===" -ForegroundColor Cyan
$rows = @()
foreach ($item in $known) {
    $schedule = Get-ScheduleState -Name $item.Name
    # 노드그룹을 다루는 스케줄이면 실제로 가리키는 이름을 Input 에서 읽어 붙인다.
    # 표에 적어 둔 설명만 보면 노드그룹이 바뀐 것을 알 수 없다.
    $resource = $item.Resource
    $nodegroup = Get-ScheduleNodegroup -Schedule $schedule
    if ($nodegroup) { $resource = "$resource -> $nodegroup" }
    $rows += [pscustomobject]@{
        이름   = $item.Name
        종류   = $item.Kind
        상태   = if ($schedule) { $schedule.State } else { '없음' }
        일정   = if ($schedule) { $schedule.ScheduleExpression } else { '-' }
        대상   = $resource
    }
}
$rows | Format-Table -AutoSize | Out-String -Width 200 | Write-Host

# 1회성 중지가 예약되어 있으면 함께 보여준다. 이것이 보이지 않으면 상시 스케줄만 보고
# "왜 자원이 이 시각에 내려갔는지" 알 수 없다.
$onceRows = @()
foreach ($source in $onceSources) {
    $schedule = Get-ScheduleState -Name "$source$onceSuffix"
    if ($schedule) {
        $onceRows += [pscustomobject]@{
            이름 = $schedule.Name
            상태 = $schedule.State
            일정 = $schedule.ScheduleExpression
            대상 = ($known | Where-Object { $_.Name -eq $source }).Resource
        }
    }
}
if ($onceRows.Count -gt 0) {
    Write-Host '=== 예약된 1회성 중지 (실행 후 자동 삭제) ===' -ForegroundColor Cyan
    $onceRows | Format-Table -AutoSize | Out-String -Width 200 | Write-Host
    Write-Host '취소: schedules.ps1 -CancelOnce' -ForegroundColor DarkGray
    Write-Host ''
}

# start만 켜져 있고 stop이 꺼져 있으면 자원이 계속 돌아 24시간 과금된다. 가장 흔한 사고다.
$startOn = @($rows | Where-Object { $_.종류 -eq 'start' -and $_.상태 -eq 'ENABLED' }).Count
$stopOff = @($rows | Where-Object { $_.종류 -eq 'stop' -and $_.상태 -ne 'ENABLED' }).Count
if ($startOn -gt 0 -and $stopOff -gt 0) {
    Write-Host "경고: start는 켜져 있는데 stop이 꺼진 스케줄이 $stopOff 개 있습니다. 자원이 24시간 과금됩니다." -ForegroundColor Yellow
}
else {
    Write-Host "start/stop 짝이 맞습니다." -ForegroundColor Green
}
if ($changed -gt 0) { Write-Host "$changed 개를 변경했습니다." }
