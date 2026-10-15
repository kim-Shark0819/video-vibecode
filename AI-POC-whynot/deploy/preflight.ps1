<#
.SYNOPSIS
    AWS 작업 전 대상 계정 일치 여부를 검증한다.

.DESCRIPTION
    config.env의 값과 실제 로그인된 자격증명을 비교한다.
    계정이 다르면 0이 아닌 코드로 종료하므로, 다른 계정에 실수로 배포되는 것을 막는다.
    성공 시 이후 명령에서 사용할 자격증명을 현재 세션 환경변수로 주입한다.

    종료 코드
      0 통과 / 1 설정 오류 / 2 계정 불일치 / 3 클러스터 확인 실패 / 4 OIDC 불일치

.PARAMETER SkipCluster
    EKS 클러스터와 OIDC 검증을 건너뛰고 계정 확인까지만 한다.
    클러스터를 아직 만들지 않은 계정에서 자격증명과 계정만 확인할 때 쓴다.
    배포 경로에서는 쓰지 않는다. 이 스위치 없이는 EKS 값이 비어 있으면 설정 오류로 멈춘다.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File AI-POC/deploy/preflight.ps1

.EXAMPLE
    # EKS를 아직 만들지 않은 신규 계정에서 계정/자격증명만 확인한다
    powershell -NoProfile -ExecutionPolicy Bypass -File AI-POC/deploy/preflight.ps1 -SkipCluster

.NOTES
    Windows PowerShell 5.1 호환. 다른 스크립트에서 dot-source하면 검증 실패 시 호출자도 함께 중단되고,
    Invoke-AwsCli 헬퍼와 로드된 설정($script:ToonConfig)을 그대로 재사용할 수 있다.
#>
[CmdletBinding()]
param(
    [string]$ConfigPath,
    # config.<Target>.env 오버레이를 적용한다.
    # dev 는 클러스터 삭제로 제거했다. 빈 값은 오버레이 없이 config.env 만 쓴다.
    [ValidateSet('', 'prod')]
    [string]$Target = '',
    [switch]$Quiet,
    [switch]$SkipCluster
)

$ErrorActionPreference = 'Stop'

# dot-source(. preflight.ps1)로 불린 경우, 성공 시 exit을 쓰면 호출자까지 종료된다.
# 실패 경로는 의도적으로 exit을 유지해 호출자도 함께 중단시킨다(fail closed).
$isDotSourced = ($MyInvocation.InvocationName -eq '.')

# Windows PowerShell 5.1에서는 param 기본값 평가 시점에 $PSScriptRoot가 비어 있을 수 있어 본문에서 해석한다.
$scriptDir = $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($scriptDir)) {
    $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
}
if ([string]::IsNullOrWhiteSpace($ConfigPath)) {
    $ConfigPath = Join-Path $scriptDir 'config.env'
}

function Write-Step {
    param([string]$Message)
    if (-not $Quiet) { Write-Host $Message }
}

function Invoke-AwsCli {
    <#
      AWS CLI를 호출하고 종료 코드와 출력을 함께 돌려준다.
      $ErrorActionPreference='Stop' 상태에서 네이티브 명령의 stderr가 곧바로 예외를 던져
      종료 코드 기반 분기를 막는 문제를 피하기 위한 래퍼다.
    #>
    param([Parameter(Mandatory = $true)][string[]]$Arguments)

    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = & aws @Arguments 2>&1
        return [pscustomobject]@{
            ExitCode = $LASTEXITCODE
            Output   = ($output | Out-String).Trim()
        }
    }
    finally {
        $ErrorActionPreference = $previous
    }
}

function Read-ToonConfig {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "설정 파일이 없습니다: $Path"
    }
    $config = @{}
    # -Encoding UTF8 필수: Windows PowerShell 5.1의 기본 ANSI 해석은 한글 주석 뒤 줄바꿈을 삼켜
    # 다음 줄의 KEY=VALUE를 주석에 합쳐버린다.
    foreach ($line in Get-Content -LiteralPath $Path -Encoding UTF8) {
        $trimmed = $line.Trim()
        if ([string]::IsNullOrWhiteSpace($trimmed) -or $trimmed.StartsWith('#')) { continue }
        $index = $trimmed.IndexOf('=')
        if ($index -lt 1) { throw "KEY=VALUE 형식이 아닙니다: $line" }
        $config[$trimmed.Substring(0, $index).Trim()] = $trimmed.Substring($index + 1).Trim()
    }
    return $config
}

function Add-ToonOverlay {
    <#
      config.<Target>.env 의 값으로 기본 설정을 덮어쓴다.

      배포 대상이 두 클러스터(DEV / 운영)이고 EKS_CLUSTER_NAME, EKS_OIDC_ID,
      EKS_CLUSTER_SECURITY_GROUP, IAM_APP_ROLE_NAME, IAM_APP_POLICY_NAME, ALB_NAME 이
      서로 다르다. config.env 를 두 벌 만들면 공통 값이 두 곳에 생기고 한쪽만 고쳐서
      어긋난다. 그래서 공통은 한 곳에 두고 다른 값만 오버레이한다.

      기본 설정에 없는 키는 거절한다. 오타로 새 키를 만들면 아무 효과 없이 통과하고
      원래 값이 그대로 쓰여서, 다른 클러스터에 배포한 줄 알고 같은 곳에 두 번 올린다.
      render.py 의 load_config_chain 과 같은 규칙이다.
    #>
    param(
        [Parameter(Mandatory = $true)][hashtable]$Config,
        [Parameter(Mandatory = $true)][string]$OverlayPath
    )
    $overlay = Read-ToonConfig -Path $OverlayPath
    $unknown = @($overlay.Keys | Where-Object { -not $Config.ContainsKey($_) } | Sort-Object)
    if ($unknown.Count -gt 0) {
        throw ("{0}: config.env 에 없는 키 [{1}]. 오버레이는 기존 키를 덮어쓰기만 합니다." -f (Split-Path -Leaf $OverlayPath), ($unknown -join ', '))
    }
    foreach ($key in $overlay.Keys) { $Config[$key] = $overlay[$key] }
    return $Config
}

try {
    $config = Read-ToonConfig -Path $ConfigPath
    if (-not [string]::IsNullOrWhiteSpace($Target)) {
        $overlayPath = Join-Path (Split-Path -Parent $ConfigPath) ("config.{0}.env" -f $Target)
        $config = Add-ToonOverlay -Config $config -OverlayPath $overlayPath
        Write-Step ("오버레이 적용: config.{0}.env" -f $Target)
    }
    $requiredKeys = @('AWS_ACCOUNT_ID', 'AWS_REGION', 'CREDENTIALS_FILE')
    if (-not $SkipCluster) {
        $requiredKeys += @('EKS_CLUSTER_NAME', 'EKS_OIDC_ID')
    }
    foreach ($key in $requiredKeys) {
        if (-not $config.ContainsKey($key) -or [string]::IsNullOrWhiteSpace($config[$key])) {
            $hint = ''
            if ($key -in @('EKS_CLUSTER_NAME', 'EKS_OIDC_ID')) {
                $hint = ' 클러스터를 아직 만들지 않았다면 -SkipCluster로 계정 확인까지만 할 수 있습니다.'
            }
            throw "config.env에 $key 값이 필요합니다.$hint"
        }
    }

    $expectedAccount = $config['AWS_ACCOUNT_ID']
    $region = $config['AWS_REGION']
    $workspaceRoot = Split-Path -Parent (Split-Path -Parent $scriptDir)
    $credentialsPath = Join-Path $workspaceRoot $config['CREDENTIALS_FILE']

    if (-not (Test-Path -LiteralPath $credentialsPath)) {
        throw "자격증명 파일을 찾을 수 없습니다: $credentialsPath"
    }

    # 자격증명은 세션 환경변수로만 주입한다. 값 자체는 출력하지 않는다.
    $accountsList = (Get-Content -Raw -LiteralPath $credentialsPath | ConvertFrom-Json).accounts_list
    if (-not $accountsList) {
        throw "자격증명 파일에 accounts_list가 없습니다: $($config['CREDENTIALS_FILE'])"
    }
    # CREDENTIALS_ACCESS_NAME이 있으면 이름으로 고른다. 예전에는 무조건 accounts_list[0]이라
    # 항목 순서가 바뀌면 조용히 다른 계정의 키를 집어 들었다.
    $accessName = $null
    if ($config.ContainsKey('CREDENTIALS_ACCESS_NAME')) {
        $accessName = $config['CREDENTIALS_ACCESS_NAME']
    }
    if (-not [string]::IsNullOrWhiteSpace($accessName)) {
        $credentials = $accountsList | Where-Object { $_.access_name -eq $accessName } | Select-Object -First 1
        if (-not $credentials) {
            $available = ($accountsList | ForEach-Object { $_.access_name }) -join ', '
            throw "자격증명 파일에 access_name='$accessName' 항목이 없습니다. 있는 이름: $available"
        }
    }
    else {
        $credentials = $accountsList[0]
    }
    foreach ($field in @('access_key', 'secret_key')) {
        if ([string]::IsNullOrWhiteSpace($credentials.$field)) {
            throw "자격증명 파일에 $field 값이 없습니다: $($config['CREDENTIALS_FILE'])"
        }
    }
    $env:AWS_ACCESS_KEY_ID = $credentials.access_key
    $env:AWS_SECRET_ACCESS_KEY = $credentials.secret_key
    $env:AWS_DEFAULT_REGION = $region
    Write-Step "자격증명 로드: $($config['CREDENTIALS_FILE']) (access_name=$($credentials.access_name))"
}
catch {
    Write-Host "선행 확인 설정 오류: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}

$identityResult = Invoke-AwsCli @('sts', 'get-caller-identity', '--output', 'json')
if ($identityResult.ExitCode -ne 0) {
    Write-Host '=== 자격증명 확인 실패: 작업을 중단합니다 ===' -ForegroundColor Red
    Write-Host $identityResult.Output
    exit 1
}
$identity = $identityResult.Output | ConvertFrom-Json

if ($identity.Account -ne $expectedAccount) {
    Write-Host ''
    Write-Host '=== 계정 불일치: 작업을 중단합니다 ===' -ForegroundColor Red
    Write-Host "  기대 계정 (config.env) : $expectedAccount"
    Write-Host "  실제 계정 (로그인됨)   : $($identity.Account)"
    Write-Host "  주체                   : $($identity.Arn)"
    Write-Host ''
    Write-Host '다른 계정으로 옮기려면 AI-POC/deploy/config.env의 값을 갱신한 뒤 다시 실행하세요.'
    exit 2
}
Write-Step "계정 확인: $($identity.Account) / $($identity.Arn)"

if ($SkipCluster) {
    Write-Host ''
    Write-Host '클러스터/OIDC 검증을 건너뜁니다 (-SkipCluster). 배포 전에는 이 스위치 없이 다시 실행하세요.' -ForegroundColor Yellow
    $script:ToonConfig = $config
    if ($isDotSourced) { return }
    exit 0
}

$cluster = $config['EKS_CLUSTER_NAME']
$clusterResult = Invoke-AwsCli @('eks', 'describe-cluster', '--name', $cluster, '--region', $region, '--query', 'cluster.status', '--output', 'text')
if ($clusterResult.ExitCode -ne 0) {
    Write-Host ''
    Write-Host '=== 클러스터 확인 실패: 작업을 중단합니다 ===' -ForegroundColor Red
    Write-Host "  클러스터 : $cluster ($region)"
    Write-Host "  원인     : $($clusterResult.Output)"
    Write-Host ''
    Write-Host 'config.env의 EKS_CLUSTER_NAME과 AWS_REGION을 확인하세요.'
    exit 3
}
if ($clusterResult.Output -ne 'ACTIVE') {
    Write-Host "EKS 클러스터 상태가 ACTIVE가 아닙니다: $($clusterResult.Output)" -ForegroundColor Red
    exit 3
}
Write-Step "클러스터 확인: $cluster ($($clusterResult.Output))"

$issuerResult = Invoke-AwsCli @('eks', 'describe-cluster', '--name', $cluster, '--region', $region, '--query', 'cluster.identity.oidc.issuer', '--output', 'text')
if ($issuerResult.ExitCode -ne 0) {
    Write-Host "OIDC 발급자를 조회하지 못했습니다: $($issuerResult.Output)" -ForegroundColor Red
    exit 3
}
$actualOidcId = $issuerResult.Output.Split('/')[-1]
if ($actualOidcId -ne $config['EKS_OIDC_ID']) {
    Write-Host ''
    Write-Host '=== OIDC 발급자 불일치: 작업을 중단합니다 ===' -ForegroundColor Red
    Write-Host "  기대 OIDC ID (config.env) : $($config['EKS_OIDC_ID'])"
    Write-Host "  실제 OIDC ID (클러스터)   : $actualOidcId"
    Write-Host ''
    Write-Host 'config.env의 EKS_OIDC_ID를 갱신하고 IRSA 신뢰 정책을 다시 적용해야 합니다.'
    exit 4
}
Write-Step "OIDC 확인: $actualOidcId"

# dot-source한 호출자가 재사용할 수 있도록 설정을 남긴다.
$script:ToonConfig = $config

if (-not $Quiet) {
    Write-Host ''
    Write-Host '선행 확인 통과. 작업을 진행할 수 있습니다.' -ForegroundColor Green
}

if ($isDotSourced) { return }
exit 0
