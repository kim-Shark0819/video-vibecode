<#
.SYNOPSIS
    배포된 API를 검증하는 스크립트들의 공통 헬퍼. 단독 실행하지 않고 dot-source한다.

.DESCRIPTION
    preflight.ps1을 dot-source해 계정을 검증하고, 대상 URL과 자격증명을 준비한다.

    Windows PowerShell 5.1 주의사항을 여기서 흡수한다.
      - 요청 본문: -Body에 문자열을 주면 ASCII로 인코딩되어 한글이 깨진다.
        UTF-8 바이트 배열로 넘기고 charset을 명시한다.
      - 응답 본문: Flask가 charset을 붙이지 않아 기본 ISO-8859-1로 해석된다.
        RawContentStream을 직접 UTF-8로 디코딩한다.

.NOTES
    이 파일과 이 폴더의 스크립트는 UTF-8 BOM으로 저장해야 한다.
    BOM이 없으면 PowerShell 5.1이 ANSI로 읽어 한글 리터럴과 주석이 깨지고 파서가 실패한다.
#>

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$verifyDir = $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($verifyDir)) {
    $verifyDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
}
$deployDir = Split-Path -Parent $verifyDir

# 계정 검증 + 자격증명 주입. 불일치면 여기서 종료된다.
#
# `-Target prod` 로 config.prod.env 오버레이를 적용한다. APP_DOMAIN 이 그 오버레이에만
# 있어서(config.env 는 빈 값) 없이 부르면 $BaseUrl 이 "https://" 가 되고 모든 호출이
# 실패한다. AI-POC 의 prod 오버레이는 이름만 prod 이고 실제로는 개발계(AI-POC-eks)라
# 검증 대상 환경과 같다 — CI 의 render.py 도 `--target prod` 로 돈다.
. (Join-Path $deployDir 'preflight.ps1') -Quiet -Target prod
$Config = $script:ToonConfig
$BaseUrl = "https://$($Config['APP_DOMAIN'])"
$Fixtures = Get-Content -Raw -Encoding UTF8 -LiteralPath (Join-Path $verifyDir 'fixtures.json') | ConvertFrom-Json
$script:VerifyFailures = 0

function Read-Utf8Json {
    param($Response)
    $stream = $Response.RawContentStream
    if ($null -eq $stream -or $stream.Length -eq 0) { return $null }
    $stream.Position = 0
    $bytes = New-Object byte[] $stream.Length
    $stream.Read($bytes, 0, $bytes.Length) | Out-Null
    return ([Text.Encoding]::UTF8.GetString($bytes) | ConvertFrom-Json)
}

function Invoke-Api {
    <# API를 호출하고 파싱된 JSON을 돌려준다. 2xx가 아니면 예외를 던진다. #>
    param(
        [Parameter(Mandatory = $true)][string]$Method,
        [Parameter(Mandatory = $true)][string]$Path,
        $BodyObj,
        [string]$Token
    )
    $headers = @{}
    if ($Token) { $headers['Authorization'] = "Bearer $Token" }
    $splat = @{ Method = $Method; Uri = "$BaseUrl$Path"; Headers = $headers; UseBasicParsing = $true }
    if ($null -ne $BodyObj) {
        $json = $BodyObj | ConvertTo-Json -Depth 8 -Compress
        $splat['Body'] = [Text.Encoding]::UTF8.GetBytes($json)
        $splat['ContentType'] = 'application/json; charset=utf-8'
    }
    return Read-Utf8Json (Invoke-WebRequest @splat)
}

function Get-ApiStatus {
    <# 실패를 기대하는 호출에 쓴다. HTTP 상태 코드만 돌려준다. #>
    param([string]$Method, [string]$Path, $BodyObj, [string]$Token)
    try {
        Invoke-Api -Method $Method -Path $Path -BodyObj $BodyObj -Token $Token | Out-Null
        return 200
    }
    catch {
        if ($_.Exception.Response) { return [int]$_.Exception.Response.StatusCode }
        throw
    }
}

function Get-ApiError {
    <#
      실패한 호출에서 서버가 보낸 오류 본문을 꺼낸다. "코드: 메시지" 형태로 돌려준다.

      PowerShell 5.1의 예외 메시지는 "원격 서버가 오류를 반환했습니다: (403)..." 까지만
      알려 주고 응답 본문을 버린다. 이 앱의 오류 응답에는 어떤 모델이 어느 리전에서
      거부됐는지가 들어 있고, 검증이 실패했을 때 정작 필요한 것은 그 문장이다.
      본문을 버리면 검증 로그만 보고는 원인을 알 수 없어 클러스터 로그를 다시 뒤져야 한다.
    #>
    param([Parameter(Mandatory = $true)]$ErrorRecord)
    $response = $ErrorRecord.Exception.Response
    if ($null -eq $response) { return $ErrorRecord.Exception.Message }
    try {
        $stream = $response.GetResponseStream()
        $reader = New-Object IO.StreamReader($stream, [Text.Encoding]::UTF8)
        $body = $reader.ReadToEnd()
        $reader.Dispose()
        # 오류 응답 형태: {"ok": false, "error": {"code": ..., "message": ...}}
        $parsed = $body | ConvertFrom-Json
        $detail = $parsed.error
        if ($null -eq $detail) { return "$([int]$response.StatusCode) $body" }
        return "{0} [{1}] {2}" -f [int]$response.StatusCode, $detail.code, $detail.message
    }
    catch {
        return $ErrorRecord.Exception.Message
    }
}

function Get-AuthToken {
    param([string]$Username, [string]$Password)
    return (Invoke-Api -Method POST -Path '/api/auth/login' -BodyObj @{ username = $Username; password = $Password }).token
}

# 검증용 계정 자격증명. config.env가 단일 원본이다.
# 예전에는 아이디와 비밀번호가 스크립트 9개에 흩어져 있어 비밀번호를 바꾸면 전부 고쳐야 했다.
$script:TestCredentials = @{ $Config['ADMIN_USERNAME'] = $Config['ADMIN_PASSWORD'] }
foreach ($chunk in (($Config['SEED_USERS'] -split ',') | Where-Object { $_ })) {
    # username:password 형태. 비밀번호에 콜론이 들어갈 수 있어 첫 콜론에서만 자른다.
    $at = $chunk.IndexOf(':')
    if ($at -lt 1) { continue }
    $script:TestCredentials[$chunk.Substring(0, $at).Trim()] = $chunk.Substring($at + 1).Trim()
}

function Get-TestToken {
    <#
      내장 계정으로 로그인한다. 비밀번호는 config.env에서 읽으므로 스크립트에 적지 않는다.
      비밀번호를 바꿀 때 config.env만 고치면 모든 검증이 따라온다.
    #>
    param([Parameter(Mandatory = $true)][string]$Username)
    $password = $script:TestCredentials[$Username]
    if ([string]::IsNullOrEmpty($password)) {
        throw "config.env에 '$Username' 계정의 비밀번호가 없습니다 (ADMIN_USERNAME/ADMIN_PASSWORD 또는 SEED_USERS를 확인하세요)."
    }
    return Get-AuthToken -Username $Username -Password $password
}

function Check {
    <# 기대값과 실제값을 비교해 결과를 출력하고 실패를 누적한다. #>
    param([string]$Name, $Expected, $Actual)
    $ok = ($Expected -eq $Actual)
    if (-not $ok) { $script:VerifyFailures++ }
    $mark = if ($ok) { 'PASS' } else { 'FAIL' }
    $color = if ($ok) { 'DarkGray' } else { 'Red' }
    if ($ok) {
        Write-Host ("[{0}] {1}" -f $mark, $Name) -ForegroundColor $color
    }
    else {
        Write-Host ("[{0}] {1}" -f $mark, $Name) -ForegroundColor $color
        Write-Host ("       기대: {0}" -f $Expected)
        Write-Host ("       실제: {0}" -f $Actual)
    }
}

function Write-Stage {
    param([string]$Message)
    Write-Host ''
    Write-Host "--- $Message ---" -ForegroundColor Cyan
}

function Complete-Verify {
    <# 결과를 요약하고 실패가 있으면 0이 아닌 코드로 종료한다. #>
    param([string]$Name)
    Write-Host ''
    if ($script:VerifyFailures -eq 0) {
        Write-Host "$Name 통과 (실패 0건)" -ForegroundColor Green
        exit 0
    }
    Write-Host "$Name 실패 $($script:VerifyFailures)건" -ForegroundColor Red
    exit 1
}

function Test-S3ObjectMissing {
    <# S3 객체가 없어야 하는 경우를 확인한다. 삭제 정리가 실제로 일어났는지 보기 위한 것. #>
    param([string]$Key)
    $result = Invoke-AwsCli @('s3api', 'head-object', '--bucket', $Config['S3_BUCKET'], '--key', $Key)
    return ($result.ExitCode -ne 0)
}

function Get-S3ObjectSize {
    <#
      객체 크기(바이트). 없으면 -1.
      presigned URL은 GET으로만 서명되어 HEAD 요청이 403이 된다. 크기 확인은 S3 API로 한다.
    #>
    param([string]$Key, [string]$Bucket)
    $target = if ($Bucket) { $Bucket } else { $Config['S3_BUCKET'] }
    $result = Invoke-AwsCli @('s3api', 'head-object', '--bucket', $target, '--key', $Key, '--query', 'ContentLength', '--output', 'text')
    if ($result.ExitCode -ne 0) { return -1 }
    return [long]$result.Output.Trim()
}


function Get-S3ObjectCount {
    <#
      prefix 아래 객체 수를 돌려준다.

      length(Contents)는 객체가 0건일 때 Contents 키가 없어 jmespath 오류가 난다.
      KeyCount는 CLI가 자동 페이지네이션할 때 응답에서 제거되어 None이 나온다.
      그래서 키 목록을 받아 직접 센다. 이 앱의 S3 키에는 공백이 없어 공백 분리로 충분하다.
    #>
    param([string]$Prefix)
    $result = Invoke-AwsCli @('s3api', 'list-objects-v2', '--bucket', $Config['S3_BUCKET'], '--prefix', $Prefix, '--query', 'Contents[].Key', '--output', 'text')
    if ($result.ExitCode -ne 0) { return -1 }
    $text = $result.Output.Trim()
    if ([string]::IsNullOrWhiteSpace($text) -or $text -eq 'None') { return 0 }
    return @($text -split '\s+' | Where-Object { $_ }).Count
}
