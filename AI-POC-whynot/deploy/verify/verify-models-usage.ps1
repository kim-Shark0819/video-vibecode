<#
.SYNOPSIS
    모델 카탈로그 정합성, 사용량 집계, 영상 삭제를 검증한다.

.DESCRIPTION
    확인 항목
      - 카탈로그 정의가 하나도 거부되지 않았는지 (거부되면 그 모델을 쓰는 기능이 죽는다)
      - 카탈로그에 실제 호출 가능한 모델만 활성 상태로 있는지 (LEGACY 모델 없음)
      - 용도(kind)별 기본 모델이 하나씩 있는지, 이미지 등급 3종이 다 있는지
      - 프롬프트로 대체 불가한 계열(style-guide, control-structure)이 살아 있는지
      - LEGACY 모델을 등록/활성화하려는 시도가 거부되는지
      - 모델 리전과 출력 버킷 리전이 어긋난 등록이 거부되는지
      - 사용량 API가 관리자 전용이고 집계가 늘어나는지
      - 영상 삭제 API의 권한 검사와 실제 삭제

    실제 Bedrock 텍스트 호출을 1회 수행해 사용량이 기록되는지 본다(표지 프롬프트).

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File AI-POC/deploy/verify/verify-models-usage.ps1
#>
[CmdletBinding()]
param()

. (Join-Path $PSScriptRoot 'common.ps1')

Write-Host "대상: $BaseUrl"

Write-Stage '로그인'
$admin = Get-TestToken -Username 'admin'
$user1 = Get-TestToken -Username 'user1'
Check '관리자 토큰' $true ([bool]$admin)
Check '일반 사용자 토큰' $true ([bool]$user1)

Write-Stage '카탈로그 구성'
$catalog = Invoke-Api -Method GET -Path '/api/models' -Token $admin
$models = $catalog.items
$enabled = @($models | Where-Object { $_.enabled })
$videoModels = @($enabled | Where-Object { $_.kind -eq 'video' })
$imageModels = @($enabled | Where-Object { $_.kind -eq 'image' })

# 거부된 카탈로그 정의가 있으면 그 모델을 쓰는 기능이 지금 죽어 있다.
#
# 이 검사가 없어서 실제로 오래 헤맸다. ConfigMap 항목에 provider 가 빠져 있어 서버가
# 이미지 모델 여섯 개를 전부 버렸고, 표지 생성은 카탈로그에 없는 폴백 모델을 불러
# AccessDeniedException 으로 실패했다. API 응답은 "모델 목록"만 주고 있었으므로
# 아래 검사들은 전부 통과했다. 버려진 항목을 세지 않으면 카탈로그가 비어 있는 것과
# 정상인 것을 구분할 수 없다.
$rejected = @($catalog.rejected)
if ($rejected.Count -gt 0) {
    foreach ($item in $rejected) {
        Write-Host ("       거부: {0}/{1} - {2}" -f $item.source, $item.id, $item.reason)
    }
}
Check '거부된 카탈로그 정의 없음' 0 $rejected.Count

Check '활성 영상 모델 1개 이상' $true ($videoModels.Count -ge 1)
Check '활성 콘티 이미지 모델 1개 이상' $true ($imageModels.Count -ge 1)
Check '영상 기본 모델 1개' 1 (@($videoModels | Where-Object { $_.isDefault }).Count)
Check '이미지 기본 모델 1개' 1 (@($imageModels | Where-Object { $_.isDefault }).Count)

# 카탈로그가 버려지면 내장 폴백 하나만 남는다. 등급별 모델이 다 있는지로 그것을 잡는다.
# 등급이 하나로 뭉치면 러프 콘티도 최종 포스터도 같은 모델·같은 비용으로 만들어진다.
foreach ($tier in @('draft', 'standard', 'premium')) {
    Check "이미지 $tier 등급 모델 있음" $true (@($imageModels | Where-Object { $_.tier -eq $tier }).Count -ge 1)
}

# 이 두 계열은 프롬프트로 대체할 수 없다. 없으면 그 기능만 조용히 죽는다.
# (관리자 화면에는 "비활성"으로도 보이지 않고 목록에서 사라진다)
$adminModels = @($models)
foreach ($pair in @(
        @{ Provider = 'stability.style-guide'; Feature = '원작 그림체 매칭' },
        @{ Provider = 'stability.control-structure'; Feature = '구도 유지 화풍 교체' })) {
    $found = @($adminModels | Where-Object { $_.enabled -and $_.provider -eq $pair.Provider })
    Check "$($pair.Feature) 모델 있음" $true ($found.Count -ge 1)
}

$imageDefault = @($imageModels | Where-Object { $_.isDefault })[0]
if ($imageDefault) {
    # 폴백으로 내려갔는지 확인한다. 내장 항목은 id 가 builtin-* 이다.
    Check '이미지 기본 모델이 내장 폴백이 아님' $false ($imageDefault.id -like 'builtin-*')
    Check '이미지 기본 모델 리전 us-west-2' 'us-west-2' $imageDefault.region
}

# LEGACY 모델이 활성 상태로 남아 있으면 사용자가 생성 버튼을 누른 뒤에야 실패를 본다.
$legacyIds = @('amazon.nova-reel-v1:0', 'amazon.nova-reel-v1:1', 'amazon.nova-canvas-v1:0')
$activeLegacy = @($enabled | Where-Object { $legacyIds -contains $_.modelId })
Check 'LEGACY 모델이 활성 목록에 없음' 0 $activeLegacy.Count

$luma = $videoModels | Where-Object { $_.modelId -eq 'luma.ray-v2:0' } | Select-Object -First 1
Check '영상 모델은 luma.ray-v2:0' $true ($null -ne $luma)
if ($luma) {
    Check '영상 모델 리전 us-west-2' 'us-west-2' $luma.region
    # 이미지 입력을 못 쓰면 업로드한 장면이 버려진다. 실제로 그 버그가 있었다.
    Check '영상 모델 이미지 입력 지원' $true $luma.capabilities.imageInput
    Check '영상 모델 최대 길이 9초' 9 $luma.capabilities.maxDurationSeconds
}

Write-Stage '등록 검증 규칙'
# LEGACY 모델을 활성 상태로 등록하려는 시도는 막혀야 한다.
$legacyEntry = @{
    id = 'verify-legacy'; label = 'LEGACY 등록 시도'; kind = 'video'
    provider = 'amazon.nova-reel'; modelId = 'amazon.nova-reel-v1:0'; region = 'us-east-1'
    invocation = 'async'; enabled = $true; outputBucket = 'mtx-test-ai-change-toon-usw2'
}
Check 'LEGACY 모델 등록 거부' 400 (Get-ApiStatus -Method POST -Path '/api/admin/models' -Token $admin -BodyObj $legacyEntry)

# 리전이 어긋난 출력 버킷도 막아야 한다(us-east-1 모델 + us-west-2 버킷).
$mismatch = @{
    id = 'verify-mismatch'; label = '리전 불일치 시도'; kind = 'video'
    provider = 'template'; modelId = 'luma.ray-v2:0'; region = 'us-east-1'
    invocation = 'async'; enabled = $true; outputBucket = 'mtx-test-ai-change-toon-usw2'
    request = @{ template = @{ prompt = '{{prompt}}' } }
}
Check '출력 버킷 리전 불일치 거부' 400 (Get-ApiStatus -Method POST -Path '/api/admin/models' -Token $admin -BodyObj $mismatch)

Check '일반 사용자는 모델 등록 불가' 403 (Get-ApiStatus -Method POST -Path '/api/admin/models' -Token $user1 -BodyObj $legacyEntry)

Write-Stage '사용량 API'
Check '일반 사용자는 사용량 조회 불가' 403 (Get-ApiStatus -Method GET -Path '/api/admin/usage' -Token $user1)
Check '잘못된 기간 거부' 400 (Get-ApiStatus -Method GET -Path '/api/admin/usage?days=0' -Token $admin)

$before = Invoke-Api -Method GET -Path '/api/admin/usage?days=30' -Token $admin
Check '집계 응답 구조' $true ($null -ne $before.totals)

# 실제 모델 호출을 한 번 만든다. 표지 생성은 텍스트 + 이미지 모델을 둘 다 부른다.
$project = (Invoke-Api -Method POST -Path '/api/projects' -Token $user1 -BodyObj @{
        title = '사용량 검증 프로젝트'
        story = '기억을 향으로 저장하는 세계에서 기억을 훔치는 소녀가 자기 과거를 추적한다.'
        visibility = 'private'
        inputType = 'webtoon'
    }).item
$projId = $project.id
Write-Host "프로젝트 $projId"

$coverOk = $true
try {
    Invoke-Api -Method POST -Path "/api/projects/$projId/cover/generate" -Token $user1 | Out-Null
}
catch {
    $coverOk = $false
    # 서버 본문을 그대로 찍는다. 어떤 모델이 어느 리전에서 거부됐는지가 여기 들어 있다.
    Write-Host ("       표지 생성 실패: {0}" -f (Get-ApiError -ErrorRecord $_))
}
Check 'AI 표지 생성 성공' $true $coverOk

Start-Sleep -Seconds 3
$after = Invoke-Api -Method GET -Path '/api/admin/usage?days=30' -Token $admin
Check '호출 수 증가' $true ($after.totals.calls -gt $before.totals.calls)
Check '입력 토큰 증가' $true ($after.totals.inputTokens -gt $before.totals.inputTokens)
Check '이미지 생성 집계 증가' $true ($after.totals.images -gt $before.totals.images)

$userRow = $after.byUser | Where-Object { $_.username -eq 'user1' } | Select-Object -First 1
Check '사용자별 집계에 user1 존재' $true ($null -ne $userRow)
if ($userRow) {
    Check 'user1 호출 기록 있음' $true ($userRow.calls -ge 1)
    Check 'user1 모델 목록 있음' $true ($userRow.models.Count -ge 1)
}
$textRow = $after.byModel | Where-Object { $_.kind -eq 'text' } | Select-Object -First 1
Check '모델별 집계에 텍스트 모델 존재' $true ($null -ne $textRow)
$imageRow = $after.byModel | Where-Object { $_.kind -eq 'image' } | Select-Object -First 1
Check '모델별 집계에 이미지 모델 존재' $true ($null -ne $imageRow)

Write-Stage '영상 삭제 권한'
Check '키 없이 삭제 거부' 400 (Get-ApiStatus -Method DELETE -Path '/api/videos' -Token $user1)
Check '타인 키 삭제 거부' 403 (Get-ApiStatus -Method DELETE -Path '/api/videos?key=users/user2/videos/2026/01/01/x.mp4' -Token $user1)
Check '영상 아닌 키 거부' 400 (Get-ApiStatus -Method DELETE -Path '/api/videos?key=users/user1/videos/2026/01/01/x.txt' -Token $user1)
Check '없는 영상 삭제 404' 404 (Get-ApiStatus -Method DELETE -Path '/api/videos?key=users/user1/videos/2026/01/01/none.mp4' -Token $user1)

# 실제 삭제는 파일을 하나 올려서 확인한다.
$tempVideo = Join-Path $env:TEMP 'verify-delete.mp4'
[IO.File]::WriteAllBytes($tempVideo, [byte[]](0x00, 0x00, 0x00, 0x18, 0x66, 0x74, 0x79, 0x70, 0x69, 0x73, 0x6F, 0x6D))
Add-Type -AssemblyName System.Net.Http
$client = [System.Net.Http.HttpClient]::new()
try {
    $client.DefaultRequestHeaders.Authorization =
        [System.Net.Http.Headers.AuthenticationHeaderValue]::new('Bearer', $user1)
    $content = [System.Net.Http.MultipartFormDataContent]::new()
    $part = [System.Net.Http.ByteArrayContent]::new([IO.File]::ReadAllBytes($tempVideo))
    $part.Headers.ContentType = [System.Net.Http.Headers.MediaTypeHeaderValue]::Parse('video/mp4')
    $content.Add($part, 'file', 'verify-delete.mp4')
    $response = $client.PostAsync("$BaseUrl/api/videos", $content).Result
    $uploaded = ($response.Content.ReadAsStringAsync().Result | ConvertFrom-Json)
}
finally {
    $client.Dispose()
    Remove-Item -Force -ErrorAction SilentlyContinue $tempVideo
}
Check '영상 업로드 성공' $true ([bool]$uploaded.item.key)

$videoKey = $uploaded.item.key
$listed = (Invoke-Api -Method GET -Path '/api/videos' -Token $user1).items
Check '목록에 업로드 영상 포함' $true (@($listed | Where-Object { $_.key -eq $videoKey }).Count -eq 1)
Check '목록 항목에 버킷 정보 포함' $true ([bool](@($listed | Where-Object { $_.key -eq $videoKey })[0].bucket))

$deleted = Invoke-Api -Method DELETE -Path "/api/videos?key=$([uri]::EscapeDataString($videoKey))" -Token $user1
Check '삭제 응답 키 일치' $videoKey $deleted.deleted
Start-Sleep -Seconds 2
Check '삭제 후 S3에서 제거' $true (Test-S3ObjectMissing -Key $videoKey)
$listedAfter = (Invoke-Api -Method GET -Path '/api/videos' -Token $user1).items
Check '삭제 후 목록에서 제거' 0 (@($listedAfter | Where-Object { $_.key -eq $videoKey }).Count)

Write-Stage '정리'
Invoke-Api -Method DELETE -Path "/api/projects/$projId" -Token $user1 | Out-Null
Check '검증 프로젝트 삭제' 404 (Get-ApiStatus -Method GET -Path "/api/projects/$projId" -Token $user1)

Complete-Verify -Name '모델·사용량·영상 삭제 검증'
