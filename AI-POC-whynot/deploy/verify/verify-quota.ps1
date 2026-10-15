<#
.SYNOPSIS
    결과물 생성 한도와 본인 사용량 조회를 검증한다.

.DESCRIPTION
    Bedrock 을 호출하지 않는다. 비용 0으로 확인할 수 있는 것만 본다.

      1. 한도가 걸린 계정으로 로그인되는지 (비밀번호에 특수문자 '!@#' 가 있어 전달 경로가
         깨지기 쉽다. YAML 평문 env 를 거치므로 실제로 확인해야 한다)
      2. 로그인 응답과 /api/auth/me 에 잔여 한도가 담기는지
      3. /api/usage/me 가 **자기 것만** 주는지, ?username= 으로 남의 것을 볼 수 없는지
      4. 일반 사용자가 /api/admin/usage 와 한도 변경 API 에 접근하지 못하는지
      5. 관리자가 한도를 바꾸고 초기화할 수 있는지 (바꾼 뒤 원래 값으로 되돌린다)
      6. 한도를 0 으로 만들면 생성이 429 QUOTA_EXCEEDED 로 막히는지
         -> **이것이 이 스크립트의 핵심이다.** 없는 modelId 를 함께 보내 Bedrock 을 부르지
            않고도 게이트가 먼저 걸리는지 볼 수 있다. verify-video-prompt.ps1 이 쓰는 방법과
            같은 논리인데, 여기서는 429 가 400 보다 먼저 나오는 것이 통과 조건이다.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File AI-POC/deploy/verify/verify-quota.ps1
#>
[CmdletBinding()]
param(
    # 검증 대상 계정. config.env 의 SEED_USERS 와 USER_QUOTAS 에 있어야 한다.
    [string]$Username = 'read-only-SAMG'
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

. (Join-Path $PSScriptRoot 'common.ps1')

Write-Stage "대상 계정: $Username"

# --- 1. 로그인 -------------------------------------------------------------
# 비밀번호는 config.env 가 단일 원본이다. 스크립트에 적지 않는다.
$token = Get-TestToken -Username $Username
Check '한도 계정 로그인' $true ([bool]$token)

$adminToken = Get-TestToken -Username $Config['ADMIN_USERNAME']
Check '관리자 로그인' $true ([bool]$adminToken)

# --- 2. 로그인·세션 응답에 잔여 한도가 담기는지 ----------------------------
Write-Stage '잔여 한도 전달'
$me = Invoke-Api -Method GET -Path '/api/auth/me' -Token $token
$quotas = $me.user.quotas
Check 'quotas.limited' $true ([bool]$quotas.limited)

$videoLimit = $quotas.categories.video.limit
$assetLimit = $quotas.categories.asset.limit
Write-Host ("  영상 한도 {0} / 이미지·문서 한도 {1}" -f $videoLimit, $assetLimit) -ForegroundColor DarkGray
Check '영상 한도가 지정됨' $true ($null -ne $videoLimit)
Check '이미지·문서 한도가 지정됨' $true ($null -ne $assetLimit)
# remaining 은 limit - used 여야 한다. 화면이 이 값을 그대로 보여 준다.
Check '영상 remaining 계산' ([int]$videoLimit - [int]$quotas.categories.video.used) ([int]$quotas.categories.video.remaining)

# --- 3. 본인 사용량 조회 ---------------------------------------------------
Write-Stage '본인 사용량 조회'
$mine = Invoke-Api -Method GET -Path '/api/usage/me' -Token $token
Check 'username 이 본인' $Username $mine.username
Check 'quotas 포함' $true ([bool]$mine.quotas)
# byUser 가 있으면 다른 사용자 목록이 함께 나간 것이다.
Check 'byUser 없음(다른 사용자 노출 차단)' $false ([bool]$mine.PSObject.Properties['byUser'])

# ?username= 으로 남의 것을 볼 수 없어야 한다. 서버가 토큰 주체로 고정하므로 무시된다.
$spoofed = Invoke-Api -Method GET -Path "/api/usage/me?username=$($Config['ADMIN_USERNAME'])" -Token $token
Check 'username 파라미터가 무시됨' $Username $spoofed.username

# --- 4. 권한 경계 ---------------------------------------------------------
Write-Stage '권한 경계'
Check '일반 사용자는 /api/admin/usage 차단' 403 (Get-ApiStatus -Method GET -Path '/api/admin/usage' -Token $token)
Check '일반 사용자는 한도 조회 차단' 403 (Get-ApiStatus -Method GET -Path "/api/admin/users/$Username/quota" -Token $token)
Check '일반 사용자는 한도 변경 차단' 403 (
    Get-ApiStatus -Method PATCH -Path "/api/admin/users/$Username/quota" -BodyObj @{ video = 9999 } -Token $token
)

# --- 5. 관리자 한도 조회·변경 --------------------------------------------
Write-Stage '관리자 한도 조정'
$view = Invoke-Api -Method GET -Path "/api/admin/users/$Username/quota" -Token $adminToken
Check '관리자 한도 조회' $Username $view.username

# 잘못된 값은 거절해야 한다. 음수 한도가 통과하면 _quota_value 가 None 으로 읽어
# 무제한이 된다(제한하려던 것이 정반대가 된다).
Check '음수 한도 거절' 400 (
    Get-ApiStatus -Method PATCH -Path "/api/admin/users/$Username/quota" -BodyObj @{ video = -1 } -Token $adminToken
)
Check '숫자가 아닌 한도 거절' 400 (
    Get-ApiStatus -Method PATCH -Path "/api/admin/users/$Username/quota" -BodyObj @{ video = 'many' } -Token $adminToken
)
Check '빈 요청 거절' 400 (
    Get-ApiStatus -Method PATCH -Path "/api/admin/users/$Username/quota" -BodyObj @{} -Token $adminToken
)

# --- 6. 0 으로 막고, 되돌린다 -------------------------------------------
Write-Stage '한도 0 이면 생성이 막히는지'
$blocked = Invoke-Api -Method PATCH -Path "/api/admin/users/$Username/quota" `
    -BodyObj @{ asset = 0 } -Token $adminToken
Check 'asset 한도 0 적용' 0 ([int]$blocked.quotas.categories.asset.limit)

# 존재하지 않는 modelId 를 함께 보낸다. 한도 게이트가 먼저 걸리면 429,
# 게이트가 없으면 모델 조회 단계에서 400 이 난다. 어느 쪽이든 Bedrock 은 호출되지 않는다.
$imageBody = @{
    prompt   = 'quota gate probe, a quiet alley at night'
    purpose  = 'scene'
    modelId  = 'does-not-exist-quota-probe'
}
Check '이미지 생성이 429 로 막힘' 429 (
    Get-ApiStatus -Method POST -Path '/api/images/generate' -BodyObj $imageBody -Token $token
)
# 콘티도 같은 주머니를 쓴다. 한 곳만 막히고 다른 곳이 열려 있으면 우회가 가능하다.
Check '콘티 생성이 429 로 막힘' 429 (
    Get-ApiStatus -Method POST -Path '/api/storyboards' `
        -BodyObj @{ prompt = 'quota gate probe story for storyboard'; cutCount = 4 } -Token $token
)
# 목록 조회는 막히지 않아야 한다. "보는 것"은 한도와 무관하다.
Check '콘티 목록 조회는 허용' 200 (Get-ApiStatus -Method GET -Path '/api/storyboards' -Token $token)
Check '영상 생성은 별도 주머니라 통과' $true (
    (Get-ApiStatus -Method POST -Path '/api/videos/generate' `
        -BodyObj @{ prompt = 'quota probe'; modelId = 'does-not-exist-quota-probe' } -Token $token) -ne 429
)

# 원래 값으로 되돌린다. 되돌리지 않으면 이 검증이 계정을 잠근 채 끝난다.
Write-Stage '원래 한도로 복구'
$restored = Invoke-Api -Method PATCH -Path "/api/admin/users/$Username/quota" `
    -BodyObj @{ asset = [int]$assetLimit } -Token $adminToken
Check 'asset 한도 복구' ([int]$assetLimit) ([int]$restored.quotas.categories.asset.limit)

# --- 7. 사용량 초기화 ----------------------------------------------------
Write-Stage '사용량 초기화'
$reset = Invoke-Api -Method PATCH -Path "/api/admin/users/$Username/quota" `
    -BodyObj @{ reset = $true } -Token $adminToken
Check '초기화 후 asset used 0' 0 ([int]$reset.quotas.categories.asset.used)
Check '초기화 후 video used 0' 0 ([int]$reset.quotas.categories.video.used)
Check '한도 값은 유지' ([int]$assetLimit) ([int]$reset.quotas.categories.asset.limit)

# --- 8. 무제한 계정은 영향이 없는지 --------------------------------------
Write-Stage '한도 없는 계정'
$plainToken = Get-TestToken -Username 'user1'
$plain = Invoke-Api -Method GET -Path '/api/auth/me' -Token $plainToken
Check 'user1 은 무제한' $false ([bool]$plain.user.quotas.limited)
# 한도가 없으면 게이트가 통과해야 한다(없는 모델이라 400 이 나오는 것이 정상).
Check 'user1 생성은 한도로 막히지 않음' $true (
    (Get-ApiStatus -Method POST -Path '/api/images/generate' -BodyObj $imageBody -Token $plainToken) -ne 429
)

Complete-Verify '생성 한도 검증'
