<#
.SYNOPSIS
    내장 계정의 비밀번호가 설정값과 맞는지, 예전 비밀번호가 막혔는지 검증한다.

.DESCRIPTION
    이 스크립트가 잡으려는 회귀는 두 가지다.

      1. `merge_user`가 `ON CREATE SET`만 쓰던 시절에는 계정이 이미 있으면 설정을 바꿔도
         비밀번호가 그대로였다. 약한 초기 비밀번호를 교체할 방법이 아예 없었다.
      2. 로그인 화면이 사용 가능한 자격증명을 인쇄하고 자동 입력까지 해 주고 있었다.
         인터넷에 열린 화면(ai-pj.testlink.click, IP 제한 없음)이라 그대로 유효했다.

    비밀번호는 config.env에서 읽는다. 이 스크립트에는 어떤 자격증명도 적지 않는다.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File AI-POC/deploy/verify/verify-credentials.ps1
#>
[CmdletBinding()]
param()

. (Join-Path $PSScriptRoot 'common.ps1')

# 예전에 화면에 인쇄되어 있던 값들. 이제 막혀 있어야 한다.
$RetiredPasswords = @{
    admin = 'admin123'
    user1 = 'user1'
    user2 = 'user2'
}

function Test-LoginFails {
    param([string]$Username, [string]$Password)
    try {
        Invoke-Api -Method POST -Path '/api/auth/login' -BodyObj @{ username = $Username; password = $Password } | Out-Null
        return 200
    }
    catch {
        if ($_.Exception.Response) { return [int]$_.Exception.Response.StatusCode }
        throw
    }
}

Write-Host "대상: $BaseUrl"

# --------------------------------------------------------------------------- #
Write-Stage '1. 설정된 비밀번호로 로그인'
# --------------------------------------------------------------------------- #
# config.env가 단일 원본이다. 기동 시 bootstrap이 계정 비밀번호를 이 값으로 맞춘다.
foreach ($username in @($Config['ADMIN_USERNAME']) + @($script:TestCredentials.Keys | Where-Object { $_ -ne $Config['ADMIN_USERNAME'] } | Sort-Object)) {
    $token = $null
    try { $token = Get-TestToken -Username $username } catch { }
    Check "$username 로그인 성공" $true ([bool]$token)
}

$adminToken = Get-TestToken -Username $Config['ADMIN_USERNAME']
$me = Invoke-Api -Method GET -Path '/api/auth/me' -Token $adminToken
Check '관리자 역할 확인' $true ([bool]$me.user.isAdmin)

$userToken = Get-TestToken -Username 'user1'
$meUser = Invoke-Api -Method GET -Path '/api/auth/me' -Token $userToken
Check '일반 사용자는 관리자 아님' $false ([bool]$meUser.user.isAdmin)

# --------------------------------------------------------------------------- #
Write-Stage '2. 예전 비밀번호는 막혀야 한다'
# --------------------------------------------------------------------------- #
foreach ($entry in $RetiredPasswords.GetEnumerator()) {
    # 설정값이 우연히 예전 값과 같으면 이 검사는 의미가 없다. 그 경우를 먼저 잡는다.
    if ($script:TestCredentials[$entry.Key] -eq $entry.Value) {
        Check "$($entry.Key) 비밀번호가 교체됨" $true $false
        continue
    }
    $status = Test-LoginFails -Username $entry.Key -Password $entry.Value
    Check "$($entry.Key) 예전 비밀번호 거부 (401)" 401 $status
}

# --------------------------------------------------------------------------- #
Write-Stage '3. 로그인 화면에 자격증명이 없어야 한다'
# --------------------------------------------------------------------------- #
$html = (Invoke-WebRequest -Uri $BaseUrl -UseBasicParsing).Content
Check '테스트 계정 안내 블록 제거' $false ($html -match 'test-account')
Check '자동 입력 버튼 제거' $false ($html -match 'fillAccount')
Check '화면에 admin123 문자열 없음' $false ($html -match 'admin123')
# 새 비밀번호도 당연히 노출되면 안 된다.
foreach ($password in $script:TestCredentials.Values) {
    Check '화면에 비밀번호 노출 없음' $false ($html.Contains($password))
}

$assetVersion = if ($html -match 'app\.js\?v=([^"]+)') { $Matches[1] } else { '' }
$js = (Invoke-WebRequest -Uri "$BaseUrl/app.js$(if ($assetVersion) { "?v=$assetVersion" })" -UseBasicParsing).Content
Check 'app.js에 자동 입력 리스너 없음' $false ($js -match 'fillAccount')
Check 'app.js에 admin123 없음' $false ($js -match 'admin123')

# --------------------------------------------------------------------------- #
Write-Stage '4. 잘못된 자격증명 처리'
# --------------------------------------------------------------------------- #
Check '없는 계정 401' 401 (Test-LoginFails -Username 'no-such-user' -Password 'whatever')
Check '빈 비밀번호 400' 400 (Test-LoginFails -Username $Config['ADMIN_USERNAME'] -Password '')

Complete-Verify -Name '자격증명 검증'
