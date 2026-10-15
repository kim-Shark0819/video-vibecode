<#
.SYNOPSIS
    프롬프트 두 계층(지시서 작업 5~8)을 검증한다.

.DESCRIPTION
    Bedrock 을 호출하지 않는다. 비용 0으로 확인할 수 있는 것만 본다.

      1. 관리자만 프롬프트 API 에 접근할 수 있는지
      2. 역할 목록에 백엔드 프롬프트가 **누락된 것이 하나도 없는지** (지시서 8-2)
      3. 프론트엔드 프롬프트를 저장·조회·삭제할 수 있는지
         -> 여기가 실제로 막혀 있었다. IRSA 정책이 _config/prompts.json 을 허용하지 않아
            put_object 가 AccessDenied 였다. 정책을 고쳤으니 저장이 되는지 확인한다
      4. 길이 상한을 넘기면 거절하는지
      5. **배포로만 바뀌는 역할(video:wan)에 저장을 시도하면 거절하는지** (지시서 6-2)
      6. 백엔드 프롬프트가 이 API 로 바뀌지 않는지 (저장 전후 원문 비교)
      7. 이력이 쌓이는지 (S3 객체 버전)
      8. 매핑표가 완결인지

    검증이 남긴 값은 마지막에 지운다. 남겨 두면 이후 모든 생성에 검증용 문장이 붙는다.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File AI-POC/deploy/verify/verify-prompts.ps1
#>
[CmdletBinding()]
param(
    # 저장 검증에 쓸 역할. 실제 생성 경로에 영향이 가장 적은 것을 고른다.
    [string]$Role = 'cover:prompt'
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

. (Join-Path $PSScriptRoot 'common.ps1')

$adminToken = Get-TestToken -Username $Config['ADMIN_USERNAME']
$memberToken = Get-TestToken -Username 'user1'

# --- 1. 권한 경계 ---------------------------------------------------------
Write-Stage '권한 경계'
Check '관리자 로그인' $true ([bool]$adminToken)
Check '일반 사용자는 프롬프트 목록 차단' 403 (Get-ApiStatus -Method GET -Path '/api/admin/prompts' -Token $memberToken)
Check '일반 사용자는 매핑표 차단' 403 (Get-ApiStatus -Method GET -Path '/api/admin/prompts/mapping' -Token $memberToken)
Check '일반 사용자는 저장 차단' 403 (
    Get-ApiStatus -Method PUT -Path "/api/admin/prompts/$Role" -BodyObj @{ text = 'x' } -Token $memberToken
)

# --- 2. 백엔드 프롬프트 누락 0건 (지시서 8-2) ----------------------------
Write-Stage '백엔드 프롬프트 누락'
$inventory = Invoke-Api -Method GET -Path '/api/admin/prompts' -Token $adminToken
Write-Host ("  역할 {0}개 / 프론트엔드 상한 {1}자" -f @($inventory.items).Count, $inventory.maxFrontendChars) -ForegroundColor DarkGray
Check '역할이 하나 이상' $true (@($inventory.items).Count -gt 0)
Check '백엔드 누락 0건' 0 (@($inventory.missingBackend).Count)

# 백엔드 원문이 실제로 실려 오는지. "(없음: ...)" 이면 참조가 깨진 것이다.
$broken = @()
foreach ($row in $inventory.items) {
    foreach ($block in $row.backend) {
        if ($block.text -like '(없음:*' -or $block.text -like '(참조 실패:*') {
            $broken += "$($row.role) -> $($block.source)"
        }
    }
}
if ($broken.Count -gt 0) { Write-Host ("  깨진 참조: " + ($broken -join ', ')) -ForegroundColor Yellow }
Check '백엔드 참조가 모두 해석됨' 0 $broken.Count

# --- 3. 수정 불가 역할 (지시서 6-2) --------------------------------------
Write-Stage '배포로만 바뀌는 역할'
$locked = @($inventory.items | Where-Object { -not $_.frontendEditable })
Write-Host ("  수정 불가 역할: " + (($locked | ForEach-Object { $_.role }) -join ', ')) -ForegroundColor DarkGray
Check '수정 불가 역할이 하나 이상' $true ($locked.Count -gt 0)
if ($locked.Count -gt 0) {
    $lockedRole = $locked[0].role
    Check "$lockedRole 저장 거절" 400 (
        Get-ApiStatus -Method PUT -Path "/api/admin/prompts/$lockedRole" -BodyObj @{ text = 'should be refused' } -Token $adminToken
    )
}

# --- 4. 없는 역할 --------------------------------------------------------
Check '없는 역할은 404' 404 (Get-ApiStatus -Method GET -Path '/api/admin/prompts/no-such-role' -Token $adminToken)

# --- 5. 저장·조회 (IRSA 정책이 고쳐졌는지 확인하는 지점) ----------------
Write-Stage '프론트엔드 프롬프트 저장'
$before = Invoke-Api -Method GET -Path "/api/admin/prompts/$Role" -Token $adminToken
$originalText = [string]$before.frontend
Write-Host ("  기존 값 {0}자" -f $originalText.Length) -ForegroundColor DarkGray

$marker = "verify-prompts probe $(Get-Date -Format 'yyyyMMdd-HHmmss')"
$saved = Invoke-Api -Method PUT -Path "/api/admin/prompts/$Role" -BodyObj @{ text = $marker } -Token $adminToken
Check '저장 성공' $marker ([string]$saved.frontend)
Check '저장자 기록' $Config['ADMIN_USERNAME'] ([string]$saved.updatedBy)

$reread = Invoke-Api -Method GET -Path "/api/admin/prompts/$Role" -Token $adminToken
Check '다시 읽어도 같은 값' $marker ([string]$reread.frontend)

# --- 6. 길이 상한 -------------------------------------------------------
Write-Stage '길이 상한'
$tooLong = 'x' * ([int]$inventory.maxFrontendChars + 1)
Check '상한 초과 거절' 400 (
    Get-ApiStatus -Method PUT -Path "/api/admin/prompts/$Role" -BodyObj @{ text = $tooLong } -Token $adminToken
)
Check 'text 누락 거절' 400 (
    Get-ApiStatus -Method PUT -Path "/api/admin/prompts/$Role" -BodyObj @{} -Token $adminToken
)

# --- 7. 백엔드가 바뀌지 않았는지 (지시서 6-3) ---------------------------
Write-Stage '백엔드 불변'
$after = Invoke-Api -Method GET -Path '/api/admin/prompts' -Token $adminToken
$beforeRow = $inventory.items | Where-Object { $_.role -eq $Role }
$afterRow = $after.items | Where-Object { $_.role -eq $Role }
$beforeBackend = (($beforeRow.backend | ForEach-Object { $_.text }) -join '|')
$afterBackend = (($afterRow.backend | ForEach-Object { $_.text }) -join '|')
Check '백엔드 원문이 그대로' $beforeBackend $afterBackend
Check '프론트엔드만 바뀜' $marker ([string]$afterRow.frontend)

# --- 8. 이력 -----------------------------------------------------------
Write-Stage '수정 이력'
$history = Invoke-Api -Method GET -Path "/api/admin/prompts/$Role" -Token $adminToken
Write-Host ("  이력 {0}건" -f @($history.history).Count) -ForegroundColor DarkGray
# 버킷 버저닝이 이력이다. 방금 저장했으니 최소 1건은 있어야 한다.
Check '이력이 쌓임' $true (@($history.history).Count -ge 1)

# --- 9. 매핑표 ---------------------------------------------------------
Write-Stage '매핑표 (지시서 8-2)'
$mapping = Invoke-Api -Method GET -Path '/api/admin/prompts/mapping' -Token $adminToken
Write-Host ("  매핑 {0}행" -f @($mapping.items).Count) -ForegroundColor DarkGray
Check '매핑표 완결' $true ([bool]$mapping.complete)
Check '매핑 행 수가 역할 수와 같음' (@($inventory.items).Count) (@($mapping.items).Count)
$noModel = @($mapping.items | Where-Object { -not $_.modelId })
Check '모든 행에 모델이 있음' 0 $noModel.Count

# --- 10. 원래대로 되돌린다 --------------------------------------------
Write-Stage '검증 값 정리'
$restored = Invoke-Api -Method PUT -Path "/api/admin/prompts/$Role" -BodyObj @{ text = $originalText } -Token $adminToken
Check '원래 값으로 복구' $originalText ([string]$restored.frontend)

Complete-Verify '프롬프트 두 계층 검증'
