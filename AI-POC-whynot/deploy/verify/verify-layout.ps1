#requires -Version 5.1
<#
  배포된 프론트엔드 자산에 레이아웃 변경이 실제로 반영됐는지 확인한다.
  AWS 비용이 들지 않고 모델을 호출하지 않는다(정적 자산 + /api/health 만 본다).

  확인 항목
    1. index.html 이 서비스되고 4개 화면에 split-layout / split-bar 가 있는지
    2. 분할 바가 키보드로 조작 가능한 ARIA separator 인지
    3. app.js 에 메뉴 접기 · 분할 바 드래그 · 진행 배너 코드가 들어 있는지
    4. styles.css 에서 고정 픽셀 하한(minmax(520px))이 사라졌는지
       -> 이것이 결과 패널 오른쪽이 잘려 보인 직접 원인이었다
    5. 자산 URL 에 버전 쿼리가 붙어 예전 파일이 캐시에서 내려오지 않는지

  드래그 감촉과 실제 시각 배치는 사람이 봐야 한다. 여기서는 구조만 본다.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

. (Join-Path $PSScriptRoot 'common.ps1')

function Get-Utf8Text {
    <# 기본 디코딩은 ISO-8859-1 이라 한글이 깨진다. UTF-8 로 직접 읽는다. #>
    param([string]$Url)
    $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 30
    if ($response.StatusCode -ne 200) { throw "HTTP $($response.StatusCode) $Url" }
    $stream = $response.RawContentStream
    $stream.Position = 0
    $bytes = New-Object byte[] $stream.Length
    [void]$stream.Read($bytes, 0, $bytes.Length)
    return [Text.Encoding]::UTF8.GetString($bytes)
}

Write-Host "대상: $BaseUrl" -ForegroundColor Cyan

# ============================ 1. index.html ============================
Write-Stage 'index.html 구조'
$html = Get-Utf8Text "$BaseUrl/"
Check -Name 'index.html 이 충분한 길이로 내려온다' -Expected $true -Actual ($html.Length -gt 10000)

Check -Name 'split-layout 4개' -Expected 4 -Actual ([regex]::Matches($html, 'split-layout')).Count
Check -Name 'split-bar 4개' -Expected 4 -Actual ([regex]::Matches($html, 'class="split-bar"')).Count
Check -Name 'split-input 3개 (생성 중 접을 입력 패널)' -Expected 3 -Actual ([regex]::Matches($html, 'split-input')).Count

foreach ($name in @('studio', 'videoNew', 'imageStudio', 'projectDetail')) {
    Check -Name "data-split=$name 존재" -Expected $true -Actual ($html -match ('data-split="{0}"' -f $name))
}

# ============================ 2. 분할 바 접근성 ============================
Write-Stage '분할 바 접근성'
Check -Name 'role=separator 4개' -Expected 4 -Actual ([regex]::Matches($html, 'role="separator"')).Count
Check -Name 'tabindex=0 (키보드 초점) 4개' -Expected 4 `
    -Actual ([regex]::Matches($html, 'class="split-bar" role="separator" tabindex="0"')).Count
Check -Name 'aria-orientation=vertical 4개' -Expected 4 -Actual ([regex]::Matches($html, 'aria-orientation="vertical"')).Count
Check -Name 'aria-valuenow 4개' -Expected 4 -Actual ([regex]::Matches($html, 'aria-valuenow=')).Count

Write-Stage '메뉴 접기 버튼'
Check -Name '사이드바 안 접기 버튼 #navCollapse' -Expected $true -Actual ($html -match 'id="navCollapse"')
Check -Name '상단바 토글에 aria-expanded' -Expected $true -Actual ($html -match 'id="menuButton"[^>]*aria-expanded')
Check -Name '토글이 sidebar 를 가리킨다 (aria-controls)' -Expected 2 -Actual ([regex]::Matches($html, 'aria-controls="sidebar"')).Count

# ============================ 3. app.js ============================
Write-Stage 'app.js 코드'
$jsMatch = [regex]::Match($html, 'src="(/app\.js[^"]*)"')
Check -Name 'HTML 이 app.js 를 참조' -Expected $true -Actual $jsMatch.Success
$jsPath = if ($jsMatch.Success) { $jsMatch.Groups[1].Value } else { '/app.js' }
$js = Get-Utf8Text "$BaseUrl$jsPath"
Check -Name 'app.js 가 충분한 길이로 내려온다' -Expected $true -Actual ($js.Length -gt 100000)

$needles = [ordered]@{
    'SPLIT_DEFAULTS (기본 비율표)'      = 'const SPLIT_DEFAULTS'
    'SPLIT_MIN_PX (픽셀 하한)'          = 'SPLIT_MIN_PX'
    'RUN_VIEWS (생성 배너 대상)'        = 'const RUN_VIEWS'
    'toggleNav 배선'                    = '$("#menuButton").addEventListener("click", toggleNav)'
    'navCollapse 배선'                  = '$("#navCollapse").addEventListener("click", toggleNav)'
    'initLayout 호출'                   = 'initLayout();'
    'wireSplitBar (포인터 드래그)'      = 'function wireSplitBar'
    'setPointerCapture (드래그 유지)'   = 'setPointerCapture'
    '키보드 조작 (ArrowLeft)'           = 'ArrowLeft'
    '비율 저장 (splitStorageKey)'       = 'splitStorageKey'
    '뷰 전환 후 재계산 (reclampSplits)' = 'reclampSplits'
    'startRun'                          = 'function startRun'
    'updateRun'                         = 'function updateRun'
    'endRun'                            = 'function endRun'
    '입력창 접기 토글'                  = 'function toggleRunCompact'
}
foreach ($key in $needles.Keys) {
    Check -Name "app.js: $key" -Expected $true -Actual ($js.Contains($needles[$key]))
}

Write-Stage '화면마다 startRun / endRun 짝'
foreach ($view in @('studio', 'videoNew', 'imageStudio', 'projectDetail')) {
    $starts = ([regex]::Matches($js, ('startRun\("{0}"' -f $view))).Count
    $ends = ([regex]::Matches($js, ('endRun\("{0}"' -f $view))).Count
    # endRun 이 없으면 배너가 영구히 "생성 중"으로 남는다.
    Check -Name "[$view] startRun 1회 이상" -Expected $true -Actual ($starts -ge 1)
    Check -Name "[$view] endRun 1회 이상" -Expected $true -Actual ($ends -ge 1)
}

# ============================ 4. styles.css ============================
Write-Stage 'styles.css 규칙'
$cssMatch = [regex]::Match($html, 'href="(/styles\.css[^"]*)"')
Check -Name 'HTML 이 styles.css 를 참조' -Expected $true -Actual $cssMatch.Success
$cssPath = if ($cssMatch.Success) { $cssMatch.Groups[1].Value } else { '/styles.css' }
$css = Get-Utf8Text "$BaseUrl$cssPath"
Check -Name 'styles.css 가 충분한 길이로 내려온다' -Expected $true -Actual ($css.Length -gt 50000)

# 주석에는 옛 값을 근거로 남겨 두었다. 주석을 걷어내고 본다.
$cssCode = [regex]::Replace($css, '/\*.*?\*/', '', 'Singleline')
Check -Name '고정 픽셀 하한 minmax(520px) 제거' -Expected $false -Actual ($cssCode.Contains('minmax(520px'))
Check -Name '.split-layout 이 minmax(0, var(--split)) 3트랙' -Expected $true `
    -Actual ($cssCode -match '\.split-layout\s*\{[^}]*grid-template-columns:\s*minmax\(0,\s*var\(--split')
Check -Name '.split-bar cursor: col-resize' -Expected $true -Actual ($cssCode -match '\.split-bar\s*\{[^}]*cursor:\s*col-resize')
Check -Name '.app-shell.nav-collapsed 규칙' -Expected $true -Actual ($cssCode.Contains('.app-shell.nav-collapsed'))
Check -Name '.menu-button 이 넓은 화면에서 보인다' -Expected $true -Actual ($cssCode -match '\.menu-button\s*\{[^}]*display:\s*flex')
Check -Name '.run-banner position: sticky' -Expected $true -Actual ($cssCode -match '\.run-banner\s*\{[^}]*position:\s*sticky')
Check -Name '생성 중 입력창 접기 규칙' -Expected $true -Actual ($cssCode.Contains('.view.run-compact .split-layout > .split-input'))
Check -Name '드래그 중 텍스트 선택 방지' -Expected $true -Actual ($cssCode.Contains('body.split-dragging'))

# ============================ 5. 캐시 무효화 ============================
Write-Stage '자산 캐시 무효화'
# ASSET_VERSION build-arg 가 빠지면 브라우저가 예전 파일을 그대로 쓴다.
Check -Name 'app.js URL 에 버전 쿼리' -Expected $true -Actual ($jsPath -match '\?v=')
Check -Name 'styles.css URL 에 버전 쿼리' -Expected $true -Actual ($cssPath -match '\?v=')
Check -Name '두 자산의 버전이 같다' -Expected $true `
    -Actual ((($jsPath -split '\?v=')[1]) -eq (($cssPath -split '\?v=')[1]))

# ============================ 6. API 회귀 ============================
Write-Stage 'API 회귀 (프론트만 바꿨어도 확인)'
$health = (Invoke-Api -Method GET -Path '/api/health')
Check -Name '/api/health ok' -Expected $true -Actual ($health.ok -eq $true)
Check -Name '계정 가드 통과' -Expected $true -Actual ($health.account.allowed -eq $true)
Check -Name '그래프 도달 가능' -Expected $true -Actual ($health.graph.reachable -eq $true)

Complete-Verify -Name '레이아웃 검증'
