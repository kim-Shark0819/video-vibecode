<#
.SYNOPSIS
    그림체 라우팅과 콘티 이미지 생성을 검증한다.

.DESCRIPTION
    실제 Bedrock을 호출해 이미지를 만든다(장당 5~11초).

    확인 항목
      - 그림체 프리셋/등급/용도 목록 제공
      - 한국어 그림체 지시 -> 프리셋과 모델 등급 판단
      - 용도에 따른 등급 선택(콘티=러프, 키비주얼=고품질)
      - 프리셋 직접 선택이 AI 판단보다 우선
      - 러프 콘티 프리셋의 최소 등급 상향(값싼 모델이 지시를 무시하는 문제)
      - 원작 그림체 참조(Style Guide) 경로
      - 생성 이미지 목록/삭제, 권한 분리
      - 사용량 집계 반영

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File AI-POC/deploy/verify/verify-styles-api.ps1
#>
[CmdletBinding()]
param()

. (Join-Path $PSScriptRoot 'common.ps1')

function Send-Multipart {
    param([string]$Token, [string]$Path, [byte[]]$Bytes, [string]$Name, [string]$ContentType)
    Add-Type -AssemblyName System.Net.Http
    $client = [System.Net.Http.HttpClient]::new()
    $client.Timeout = [TimeSpan]::FromMinutes(3)
    try {
        $client.DefaultRequestHeaders.Authorization =
            [System.Net.Http.Headers.AuthenticationHeaderValue]::new('Bearer', $Token)
        $content = [System.Net.Http.MultipartFormDataContent]::new()
        $part = [System.Net.Http.ByteArrayContent]::new($Bytes)
        $part.Headers.ContentType = [System.Net.Http.Headers.MediaTypeHeaderValue]::Parse($ContentType)
        $content.Add($part, 'file', $Name)
        $response = $client.PostAsync("$BaseUrl$Path", $content).Result
        $body = $response.Content.ReadAsStringAsync().Result
        if ([int]$response.StatusCode -ge 300) { throw "업로드 실패: $([int]$response.StatusCode) $body" }
        return $body | ConvertFrom-Json
    }
    finally { $client.Dispose() }
}

Write-Host "대상: $BaseUrl"
$token = Get-TestToken -Username 'user1'
$other = Get-TestToken -Username 'user2'
$admin = Get-TestToken -Username 'admin'

Write-Stage '그림체 목록'
$styles = Invoke-Api -Method GET -Path '/api/styles' -Token $token
Check '프리셋 10종 이상' $true ($styles.presets.Count -ge 10)
Check '등급 3종' 3 $styles.tiers.Count
Check '용도 3종' 3 $styles.purposes.Count
Check '웹툰 채색 프리셋 존재' $true (@($styles.presets | Where-Object { $_.id -eq 'webtoon-color' }).Count -eq 1)
Check '러프 콘티 프리셋 존재' $true (@($styles.presets | Where-Object { $_.id -eq 'storyboard-rough' }).Count -eq 1)

Write-Stage '카탈로그: 이미지 모델 등급'
$models = (Invoke-Api -Method GET -Path '/api/models' -Token $admin).items
$imageModels = @($models | Where-Object { $_.kind -eq 'image' -and $_.enabled })
Check '활성 이미지 모델 4종' 4 $imageModels.Count
foreach ($tier in @('draft', 'standard', 'premium')) {
    Check "등급 $tier 모델 존재" $true (@($imageModels | Where-Object { $_.tier -eq $tier }).Count -ge 1)
}
Check '원작 그림체 매칭 모델 존재' $true (@($imageModels | Where-Object { $_.capabilities.styleReference }).Count -ge 1)

Write-Stage '입력 검증'
Check '짧은 장면 설명 거부' 400 (Get-ApiStatus -Method POST -Path '/api/images/generate' -Token $token -BodyObj @{ prompt = 'a' })
Check '잘못된 용도 거부' 400 (Get-ApiStatus -Method POST -Path '/api/images/generate' -Token $token -BodyObj @{ prompt = '비 내리는 골목에 서 있는 소녀'; purpose = 'unknown' })
Check '타인 이미지 참조 거부' 403 (Get-ApiStatus -Method POST -Path '/api/images/generate' -Token $token -BodyObj @{
        prompt = '비 내리는 골목에 서 있는 소녀'; referenceKey = 'users/user2/uploads/2026/01/01/x.png'
    })

$scene = '비 내리는 좁은 골목에서 후드를 쓴 소녀가 유리병을 들어 올려 바라본다. 네온 간판이 젖은 바닥에 번진다.'

Write-Stage 'AI 그림체 판단: 실사 키비주얼'
$keyart = Invoke-Api -Method POST -Path '/api/images/generate' -Token $token -BodyObj @{
    prompt = $scene
    stylePrompt = '실제 배우가 촬영한 넷플릭스 드라마 스틸처럼, 포스터에 쓸 최고 품질로'
    purpose = 'keyart'
    aspectRatio = '16:9'
}
Check '키비주얼 생성 성공' $true ([bool]$keyart.item.url)
Check '실사 프리셋 선택' 'cinematic-real' $keyart.style.presetId
Check '고품질 등급 선택' 'premium' $keyart.style.tier
Check 'AI가 판단' 'ai' $keyart.style.source
Check '판단 이유 제공' $true ([bool]$keyart.style.reason)
Check '고품질 모델 사용' 'premium' $keyart.model.tier
Check '프롬프트가 매체로 시작' $true ($keyart.prompt -like 'A photorealistic cinematic film still of*')
Write-Host "  판단: $($keyart.style.presetLabel) / $($keyart.style.tier) / 모델 $($keyart.model.label)"

Write-Stage 'AI 그림체 판단: 웹툰 장면'
$webtoon = Invoke-Api -Method POST -Path '/api/images/generate' -Token $token -BodyObj @{
    prompt = $scene
    stylePrompt = '웹툰 원작 느낌 그대로 깔끔한 선과 밝은 채색으로'
    purpose = 'scene'
}
Check '웹툰 프리셋 선택' 'webtoon-color' $webtoon.style.presetId
Check '표준 등급 선택' 'standard' $webtoon.style.tier
Check '표준 모델 사용' 'standard' $webtoon.model.tier

Write-Stage '콘티 그림체: 연필과 마커 구분 + 최소 등급 상향'
# 콘티 프리셋이 둘이다. 연필은 윤곽선만(명암 0단계), 마커는 선 + 명암 3단계.
# 두 요청이 서로 다른 프리셋으로 가야 한다. 하나로 합치면 지시가 상쇄되어
# 어중간한 그림이 나온다.
$pencil = Invoke-Api -Method POST -Path '/api/images/generate' -Token $token -BodyObj @{
    prompt = $scene
    stylePrompt = '연필로 사람과 물체의 형태만 보이면 충분해요'
    purpose = 'storyboard'
}
Check '연필 콘티 프리셋 선택' 'storyboard-pencil' $pencil.style.presetId
# 용도 기본값은 draft지만, 값싼 모델이 '형태만'을 무시하므로 standard로 올라가야 한다.
Check '연필: 등급이 표준으로 상향' 'standard' $pencil.style.tier
Check '연필: 상향 이유 안내' $true ($pencil.style.reason -like '*표준 등급으로 올렸습니다*')

$marker = Invoke-Api -Method POST -Path '/api/images/generate' -Token $token -BodyObj @{
    prompt = $scene
    stylePrompt = '마커로 그린 러프 콘티, 명암도 조금 넣어주세요'
    purpose = 'storyboard'
}
Check '마커 러프 콘티 프리셋 선택' 'storyboard-rough' $marker.style.presetId
Check '마커: 등급이 표준으로 상향' 'standard' $marker.style.tier

# 보강 단계가 두 요청 모두에서 동작했는지. 짧은 지시를 자세하게 다시 써야 한다.
Check '연필: 그림체 설명 보강됨' $true ([bool]$pencil.boost.style)
Check '연필: 장면 설명 보강됨' $true ([bool]$pencil.boost.scene)
$storyboard = $pencil

Write-Stage '프리셋 직접 선택이 우선'
$forced = Invoke-Api -Method POST -Path '/api/images/generate' -Token $token -BodyObj @{
    prompt = $scene
    stylePrompt = '실사 드라마처럼'
    presetId = 'ink-brush'
    purpose = 'scene'
}
Check '직접 선택한 프리셋 적용' 'ink-brush' $forced.style.presetId
Check '직접 선택으로 표시' 'manual' $forced.style.source
Check '수묵 매체가 프롬프트 앞에' $true ($forced.prompt -like 'An east asian ink brush painting of*')

Write-Stage '원작 그림체 참조 (Style Guide)'
$referenceBytes = (Invoke-WebRequest -Uri $webtoon.item.url -UseBasicParsing).Content
$uploaded = Send-Multipart -Token $token -Path '/api/assets' -Bytes $referenceBytes -Name 'reference.png' -ContentType 'image/png'
Check '참조 이미지 업로드' $true ([bool]$uploaded.item.key)

$matched = Invoke-Api -Method POST -Path '/api/images/generate' -Token $token -BodyObj @{
    prompt = '해가 지는 옥상에서 두 인물이 마주 서 있다'
    stylePrompt = '원작 그림체 그대로'
    purpose = 'scene'
    referenceKey = $uploaded.item.key
}
Check '참조 기반 생성 성공' $true ([bool]$matched.item.url)
Check '참조 사용 표시' $true $matched.style.usedStyleReference
Check '스타일 매칭 모델 사용' $true ($matched.model.capabilities.styleReference -eq $true)

Write-Stage '이미지 목록과 삭제'
$images = (Invoke-Api -Method GET -Path '/api/images' -Token $token).items
Check '생성 이미지 5장 이상' $true ($images.Count -ge 5)
Check '타인 이미지 삭제 거부' 403 (Get-ApiStatus -Method DELETE -Path '/api/images?key=users/user2/images/2026/01/01/x.png' -Token $token)
Check '키 없이 삭제 거부' 400 (Get-ApiStatus -Method DELETE -Path '/api/images' -Token $token)

$target = $images[0]
Invoke-Api -Method DELETE -Path "/api/images?key=$([uri]::EscapeDataString($target.key))" -Token $token | Out-Null
Start-Sleep -Seconds 2
Check '삭제 후 S3에서 제거' $true (Test-S3ObjectMissing -Key $target.key)
$afterList = (Invoke-Api -Method GET -Path '/api/images' -Token $token).items
Check '삭제 후 목록에서 제거' 0 (@($afterList | Where-Object { $_.key -eq $target.key }).Count)
Check '타인은 내 이미지를 못 봄' 0 (@((Invoke-Api -Method GET -Path '/api/images' -Token $other).items | Where-Object { $_.key -like 'users/user1/*' }).Count)

Write-Stage '사용량 집계'
$usage = Invoke-Api -Method GET -Path '/api/admin/usage?days=1' -Token $admin
Check '이미지 생성 집계됨' $true ($usage.totals.images -ge 5)
$routerRow = $usage.byModel | Where-Object { $_.kind -eq 'text' } | Select-Object -First 1
Check '그림체 판단도 집계됨' $true ($null -ne $routerRow)

Write-Stage '정리'
foreach ($image in $afterList) {
    Invoke-Api -Method DELETE -Path "/api/images?key=$([uri]::EscapeDataString($image.key))" -Token $token | Out-Null
}
Check '생성 이미지 정리' 0 (Invoke-Api -Method GET -Path '/api/images' -Token $token).items.Count

Complete-Verify -Name '그림체 라우팅 검증'
