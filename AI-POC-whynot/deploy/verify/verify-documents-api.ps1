<#
.SYNOPSIS
    진입점 1·2의 AI 산출물 생성을 검증한다.

.DESCRIPTION
    실제 Bedrock을 호출하므로 시간이 걸린다(산출물 1건당 1~2분).
    기본은 진입점 1의 분석 1건만 생성하고, -Full을 주면 4종을 모두 생성한다.

    확인 항목
      - 진입점별 생성 가능 목록(allowedKinds)
      - 원본 자료 없이 생성 시도 거부
      - 진입점에 맞지 않는 종류 거부
      - 생성 시작 202 -> 폴링 -> ready 전환
      - 결과가 스키마의 필수 필드를 채우는지
      - 생성 중 재요청 거부(409)
      - 산출물 직접 수정(PUT) 저장
      - 권한 분리, 삭제 시 S3 정리

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File AI-POC/deploy/verify/verify-documents-api.ps1

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File AI-POC/deploy/verify/verify-documents-api.ps1 -Full
#>
[CmdletBinding()]
param(
    [switch]$Full,
    # 특정 종류만 검증할 때 사용한다. 예: -Kinds treatment
    [string[]]$Kinds,
    [int]$TimeoutMinutes = 8
)

. (Join-Path $PSScriptRoot 'common.ps1')

function Wait-Document {
    <# ready 또는 failed가 될 때까지 기다린다. #>
    param([string]$Token, [string]$ProjectId, [string]$Kind, [int]$Minutes)
    $deadline = (Get-Date).AddMinutes($Minutes)
    $last = $null
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 10
        $payload = Invoke-Api -Method GET -Path "/api/projects/$ProjectId/documents" -Token $Token
        $last = $payload.items | Where-Object { $_.kind -eq $Kind }
        if ($last -and $last.status -ne 'generating') { return $last }
        Write-Host ("    ... {0} 대기 중 ({1})" -f $Kind, $last.status)
    }
    return $last
}

$fixtureSource = @"
[1화] 향 도둑
2035년 서울. 기억은 향으로 저장된다. 사람들은 첫사랑이나 어머니의 손맛 같은 순간을 유리병에 담아 보관한다.
기억 거래는 반합법이다. 국가 기억관리국이 유통을 관리하지만 암시장이 더 크다.
유하(19)는 무면허 기억 브로커다. 낮에는 편의점에서 일하고 밤에는 남의 기억을 훔쳐 판다.
유하에게는 일곱 살 이전의 기억이 없다. 통째로 비어 있다.

[2화] 의뢰
기억관리국 수사관 정겸(34)이 찾아온다. 잡으러 온 것이 아니라 의뢰를 하러 왔다.
죽은 어머니의 마지막 기억을 사고 싶다고 한다. 그 기억을 훔친 사람이 유하다.

[3화] 얼굴
유하가 그 기억을 열어본다. 정겸의 어머니가 마지막으로 본 얼굴은 일곱 살의 유하다.
어머니는 유하의 손을 잡고 있었다. 유하는 자기가 왜 그 자리에 있었는지 모른다.
"@

Write-Host "대상: $BaseUrl"
Write-Host ("모드: {0}" -f ($(if ($Full) { '전체 4종 생성' } else { '분석 1종만 생성' })))

Write-Stage '준비'
$t1 = Get-TestToken -Username 'user1'
$t2 = Get-TestToken -Username 'user2'

# 종류가 추가될 때마다 개수 단정이 깨지지 않게 존재 여부로 확인한다.
$kindList = Invoke-Api -Method GET -Path '/api/document-kinds' -Token $t1
$kindNames = @($kindList.items | ForEach-Object { $_.kind })
foreach ($expected in @('analysis', 'adaptation', 'logline', 'characters', 'treatment')) {
    Check "산출물 종류 등록: $expected" $true ($kindNames -contains $expected)
}

$project = (Invoke-Api -Method POST -Path '/api/projects' -Token $t1 -BodyObj @{
        title = '향 도둑 (검증)'
        story = '기억을 향으로 저장하는 세계에서, 자기 기억을 잃은 소녀가 남의 기억을 훔쳐 판다.'
        visibility = 'private'
        inputType = 'webtoon'
    }).item
$projId = $project.id
Write-Host "프로젝트 $projId"

Invoke-Api -Method PUT -Path "/api/projects/$projId/direction" -Token $t1 -BodyObj @{
    targetPlatform = '넷플릭스'; episodeCount = 6; genres = 'SF, 스릴러, 멜로'
    tone = $Fixtures.tone; audience = '20~30대'
} | Out-Null

Write-Stage '진입점별 생성 가능 목록'
$docs = Invoke-Api -Method GET -Path "/api/projects/$projId/documents" -Token $t1
foreach ($expected in @('analysis', 'adaptation', 'characters', 'treatment')) {
    Check "웹툰 진입점 허용: $expected" $true ($docs.allowedKinds -contains $expected)
}
Check '웹툰에 로그라인 구체화 제외' $false ($docs.allowedKinds -contains 'logline')
Check '초기 산출물 없음' 0 $docs.items.Count

Write-Stage '사전 조건 검증'
# 원본 자료가 없으면 AI가 읽을 것이 없다.
Check '자료 없이 생성 거부' 400 (Get-ApiStatus -Method POST -Path "/api/projects/$projId/documents/analysis/generate" -Token $t1)
Check '진입점 불일치 거부' 400 (Get-ApiStatus -Method POST -Path "/api/projects/$projId/documents/logline/generate" -Token $t1)
Check '없는 종류 거부' 400 (Get-ApiStatus -Method POST -Path "/api/projects/$projId/documents/unknown/generate" -Token $t1)

Invoke-Api -Method POST -Path "/api/projects/$projId/source" -Token $t1 -BodyObj @{
    text = $fixtureSource; name = 'webtoon.txt'
} | Out-Null
Check '자료 저장 후 조회' $true ((Invoke-Api -Method GET -Path "/api/projects/$projId/source" -Token $t1).text.Length -gt 100)

Write-Stage '권한 분리'
Check '타인이 생성 시도 거부' 403 (Get-ApiStatus -Method POST -Path "/api/projects/$projId/documents/analysis/generate" -Token $t2)
Check '타인이 목록 조회 거부' 403 (Get-ApiStatus -Method GET -Path "/api/projects/$projId/documents" -Token $t2)

# @()로 감싸는 이유: PowerShell은 원소가 하나인 배열을 스칼라로 풀어버린다.
# 그대로 두면 $targets가 문자열이 되어 $targets[0]이 첫 글자('t')가 된다.
$targets = @(if ($Kinds) { $Kinds } elseif ($Full) { 'analysis', 'adaptation', 'characters', 'treatment' } else { 'analysis' })
# 트리트먼트 생성 옵션. 변수명은 파라미터($Full/$Kinds/$TimeoutMinutes)와 철자가 겹치지 않게 짓는다.
# PowerShell 변수는 대소문자를 구분하지 않아 겹치면 유효성 검사 오류가 난다.
$treatOptions = @{ pages = 3; detail = 'normal'; details = '주인공의 직업은 그대로 두고, 결말은 열린 결말로 남긴다.' }

Write-Stage '트리트먼트 생성 옵션'
$docKindItems = (Invoke-Api -Method GET -Path '/api/document-kinds' -Token $t1).items
$treatMeta = ($docKindItems | Where-Object { $_.kind -eq 'treatment' }).options
Check '옵션 메타 노출' $true ($null -ne $treatMeta)
Check '최대 장수 10' 10 $treatMeta.maxPages
Check 'A4 장수 선택지 존재' $true ($treatMeta.pageChoices.Count -ge 2)
Check '상세도 3단계' 3 $treatMeta.details.Count
Check '본문 섹션 5개' 5 $treatMeta.sections.Count
Check '섹션 순서' 'intro,characterIntro,mainEvents,climax,ending' (($treatMeta.sections | ForEach-Object { $_.id }) -join ',')
Check '페이지당 글자 수 노출' $true ($treatMeta.charsPerPage -gt 0)
# 잘못된 값은 생성이 시작되기 전에 거부되어야 한다. 통과하면 비용만 쓰고 실패한다.
Check '장수 초과 거부' 400 (Get-ApiStatus -Method POST -Path "/api/projects/$projId/documents/treatment/generate" -Token $t1 -BodyObj @{ options = @{ pages = 99 } })
Check '장수 0 거부' 400 (Get-ApiStatus -Method POST -Path "/api/projects/$projId/documents/treatment/generate" -Token $t1 -BodyObj @{ options = @{ pages = 0 } })
Check '장수 문자 거부' 400 (Get-ApiStatus -Method POST -Path "/api/projects/$projId/documents/treatment/generate" -Token $t1 -BodyObj @{ options = @{ pages = 'many' } })
Check '상세도 오탈자 거부' 400 (Get-ApiStatus -Method POST -Path "/api/projects/$projId/documents/treatment/generate" -Token $t1 -BodyObj @{ options = @{ detail = 'verbose' } })
Check '세부사항 길이 초과 거부' 400 (Get-ApiStatus -Method POST -Path "/api/projects/$projId/documents/treatment/generate" -Token $t1 -BodyObj @{ options = @{ details = ('가' * 2100) } })

foreach ($kind in $targets) {
    Write-Stage "생성: $kind"
    # 옵션을 받는 종류만 본문을 실어 보낸다(지금은 트리트먼트).
    $started = if ($kind -eq 'treatment') {
        Invoke-Api -Method POST -Path "/api/projects/$projId/documents/$kind/generate" -Token $t1 -BodyObj @{ options = $treatOptions }
    } else {
        Invoke-Api -Method POST -Path "/api/projects/$projId/documents/$kind/generate" -Token $t1
    }
    Check "$kind 생성 시작 상태" 'generating' $started.item.status
    if ($kind -eq 'treatment') {
        # 고른 옵션이 정점에 남아야 화면이 마지막 선택을 다시 보여줄 수 있다.
        Check '트리트먼트: 옵션 기록' 3 $started.item.options.pages
    }
    Check "$kind 모델 기록" $true ([bool]$started.item.model)
    # 같은 종류를 동시에 두 번 만들면 결과가 덮어써진다.
    Check "$kind 중복 생성 거부" 409 (Get-ApiStatus -Method POST -Path "/api/projects/$projId/documents/$kind/generate" -Token $t1)

    $final = Wait-Document -Token $t1 -ProjectId $projId -Kind $kind -Minutes $TimeoutMinutes
    Check "$kind 완료 상태" 'ready' $final.status
    if ($final.status -ne 'ready') {
        Write-Host ("       오류: {0}" -f $final.error)
        continue
    }
    Check "$kind 제목 생성" $true ([bool]$final.title)
    Check "$kind 분량 기록" $true ($final.charCount -gt 500)

    # 변수명 주의: PowerShell 변수는 대소문자를 구분하지 않아 $full은 [switch]$Full 파라미터와 같은 변수다.
    $fetched = Invoke-Api -Method GET -Path "/api/projects/$projId/documents/$kind" -Token $t1
    $content = $fetched.content
    Check "$kind 본문 반환" $true ($null -ne $content)
    switch ($kind) {
        'analysis' {
            Check '분석: verdict' $true ([bool]$content.verdict)
            Check '분석: score 범위' $true ($content.score -ge 0 -and $content.score -le 100)
            Check '분석: 강점 3개 이상' $true ($content.strengths.Count -ge 3)
            Check '분석: 위험 3개 이상' $true ($content.risks.Count -ge 3)
            Check '분석: 추천 형식' $true ([bool]$content.formatRecommendation.format)
            # 기획 방향(6부작)이 반영되었는지 본다.
            Check '분석: 화수 지시 반영' $true ($content.formatRecommendation.format -match '6')
        }
        'adaptation' {
            Check '각색: 선택지 2개 이상' $true ($content.options.Count -ge 2)
            Check '각색: 추천 지정' $true ([bool]$content.recommended)
            Check '각색: 추천이 선택지에 존재' $true (@($content.options | ForEach-Object { $_.name }) -contains $content.recommended)
            Check '각색: 톤앤매너' $true ([bool]$content.toneAndManner)
        }
        'treatment' {
            # 기획 방향의 화수(6)와 실제 생성된 화수가 같아야 한다. 배치 생성이 빠지면 여기서 걸린다.
            Check '트리트먼트: 6화 생성' 6 $content.episodes.Count
            Check '트리트먼트: 화 번호 순서' '1,2,3,4,5,6' (($content.episodes | ForEach-Object { $_.number }) -join ',')
            Check '트리트먼트: 시놉시스 분량' $true ($content.episodes[0].synopsis.Length -gt 100)
            Check '트리트먼트: 핵심 장면' $true ($content.episodes[0].keyScenes.Count -ge 2)
            Check '트리트먼트: 다음화 훅' $true ([bool]$content.episodes[0].hook)
            Check '트리트먼트: 3막 구조' $true ([bool]$content.structure.act1 -and [bool]$content.structure.act3)
            Check '트리트먼트: 주요 인물' $true ($content.mainCharacters.Count -ge 3)
            Check '트리트먼트: 미완성 표시 없음' $false ([bool]$content.incompleteNote)

            # 본문 5개 섹션. 기획자가 인쇄해서 읽는 부분이라 하나라도 비면 안 된다.
            $narr = $content.narrative
            Check '트리트먼트: 본문 존재' $true ($null -ne $narr)
            foreach ($sectionId in @('intro', 'characterIntro', 'mainEvents', 'climax', 'ending')) {
                Check "트리트먼트: 본문 $sectionId 작성" $true ($narr.$sectionId.Length -gt 150)
            }
            Check '트리트먼트: 섹션 순서 반환' 'intro,characterIntro,mainEvents,climax,ending' (($content.narrativeSections | ForEach-Object { $_.id }) -join ',')
            # 상세도 normal 이면 섹션마다 핵심 장면이 붙는다.
            Check '트리트먼트: 핵심 장면 블록' $true ($content.narrativeBeats.mainEvents.Count -ge 2)

            # 요청한 A4 장수가 실제 분량으로 반영되는지. 옵션이 무시되면 여기서 걸린다.
            $est = $content.pageEstimate
            Check '트리트먼트: 분량 정보' $true ($null -ne $est)
            Check '트리트먼트: 요청 장수 기록' 3 $est.requestedPages
            Check '트리트먼트: 상세도 기록' 'normal' $est.detail
            # 목표의 60~160% 범위. 모델 편차를 감안한 폭이고, 옵션이 무시되면 이 범위를 벗어난다.
            $lowMark = 3 * $est.charsPerPage * 0.6
            $highMark = 3 * $est.charsPerPage * 1.6
            Check '트리트먼트: 분량이 요청에 비례' $true ($est.bodyChars -ge $lowMark -and $est.bodyChars -le $highMark)
            # JSON이 아니라 평문으로 받으므로 대사 인용부호가 본문을 깨뜨리지 않아야 한다.
            Check '트리트먼트: 구분자 누출 없음' $false ($narr.intro -match '@@SECTION|@@BEATS')
        }
        'characters' {
            Check '인물: 3명 이상' $true ($content.characters.Count -ge 3)
            Check '인물: 말투 작성' $true ([bool]$content.characters[0].speechStyle)
            Check '인물: 배경 작성' $true ([bool]$content.characters[0].background)
            # 원작에 있는 이름을 그대로 써야 한다.
            $names = ($content.characters | ForEach-Object { $_.name }) -join ' '
            Check '인물: 원작 인물명 사용' $true ($names -match '유하')
        }
    }
    Check "$kind 타인 조회 거부" 403 (Get-ApiStatus -Method GET -Path "/api/projects/$projId/documents/$kind" -Token $t2)
}

if ($Full) {
    Write-Stage '진입점 2: 로그라인 구체화'
    $logProject = (Invoke-Api -Method POST -Path '/api/projects' -Token $t1 -BodyObj @{
            title = '향 도둑 (기획 단계 검증)'
            story = '기억을 향으로 저장하는 세계의 기억 브로커 이야기.'
            visibility = 'private'
            inputType = 'logline'
        }).item
    $logId = $logProject.id
    Invoke-Api -Method POST -Path "/api/projects/$logId/source" -Token $t1 -BodyObj @{
        text = $Fixtures.sourceText; name = 'logline.txt'
    } | Out-Null
    Invoke-Api -Method PUT -Path "/api/projects/$logId/direction" -Token $t1 -BodyObj @{
        targetPlatform = '티빙'; episodeCount = 8; genres = 'SF, 멜로'
    } | Out-Null

    $logDocs = Invoke-Api -Method GET -Path "/api/projects/$logId/documents" -Token $t1
    Check '로그라인 진입점에 구체화 포함' $true ($logDocs.allowedKinds -contains 'logline')
    Check '로그라인 진입점에 트리트먼트 포함' $true ($logDocs.allowedKinds -contains 'treatment')
    Check '로그라인 진입점에 분석 제외' $false ($logDocs.allowedKinds -contains 'analysis')
    Check '로그라인 진입점에서 분석 거부' 400 (Get-ApiStatus -Method POST -Path "/api/projects/$logId/documents/analysis/generate" -Token $t1)

    Invoke-Api -Method POST -Path "/api/projects/$logId/documents/logline/generate" -Token $t1 | Out-Null
    $logFinal = Wait-Document -Token $t1 -ProjectId $logId -Kind 'logline' -Minutes $TimeoutMinutes
    Check '로그라인 구체화 완료' 'ready' $logFinal.status
    if ($logFinal.status -eq 'ready') {
        $logContent = (Invoke-Api -Method GET -Path "/api/projects/$logId/documents/logline" -Token $t1).content
        Check '구체화: 로그라인 문장' $true ([bool]$logContent.logline)
        Check '구체화: 프리미스' $true ($logContent.premise.Length -gt 50)
        Check '구체화: 세계관 규칙' $true ($logContent.worldSetting.rules.Count -ge 2)
        Check '구체화: 갈등' $true ([bool]$logContent.conflict.external)
        Check '구체화: 훅 3개 이상' $true ($logContent.hooks.Count -ge 3)
        Check '구체화: 시즌 흐름' $true ([bool]$logContent.seasonArc)
    }
    Invoke-Api -Method DELETE -Path "/api/projects/$logId" -Token $t1 | Out-Null
    Start-Sleep -Seconds 2
    Check '로그라인 프로젝트 정리' 0 (Get-S3ObjectCount -Prefix "users/user1/documents/$logId/")
}

Write-Stage '직접 수정'
# 생성한 종류 중 첫 번째로 검증한다. -Kinds로 일부만 돌릴 때도 동작해야 한다.
$editKind = $targets[0]
$existing = Invoke-Api -Method GET -Path "/api/projects/$projId/documents/$editKind" -Token $t1
$edited = $existing.content
$marker = '기획자가 직접 고친 내용입니다.'
if ($null -eq $edited.PSObject.Properties['overview']) {
    $edited | Add-Member -NotePropertyName 'plannerNote' -NotePropertyValue $marker -Force
    $editedField = 'plannerNote'
}
else {
    $edited.overview = $marker
    $editedField = 'overview'
}
$saved = Invoke-Api -Method PUT -Path "/api/projects/$projId/documents/$editKind" -Token $t1 -BodyObj @{ content = $edited }
Check '수정 저장 origin' 'manual' $saved.item.origin
Check '수정 내용 반영' $marker (Invoke-Api -Method GET -Path "/api/projects/$projId/documents/$editKind" -Token $t1).content.$editedField
Check '빈 content 거부' 400 (Get-ApiStatus -Method PUT -Path "/api/projects/$projId/documents/$editKind" -Token $t1 -BodyObj @{ content = @{} })
Check '타인이 수정 거부' 403 (Get-ApiStatus -Method PUT -Path "/api/projects/$projId/documents/$editKind" -Token $t2 -BodyObj @{ content = $edited })

Write-Stage '삭제와 정리'
$docKeys = @(
    (Invoke-Api -Method GET -Path "/api/projects/$projId/documents" -Token $t1).items |
        ForEach-Object { $_.s3Key } | Where-Object { $_ }
)
Check '산출물 S3 키 존재' $true ($docKeys.Count -ge 1)
Invoke-Api -Method DELETE -Path "/api/projects/$projId/documents/$editKind" -Token $t1 | Out-Null
Check '삭제 후 조회 404' 404 (Get-ApiStatus -Method GET -Path "/api/projects/$projId/documents/$editKind" -Token $t1)

Invoke-Api -Method DELETE -Path "/api/projects/$projId" -Token $t1 | Out-Null
Start-Sleep -Seconds 3
Check '프로젝트 삭제 후 산출물 정리' 0 (Get-S3ObjectCount -Prefix "users/user1/documents/$projId/")

Complete-Verify -Name 'AI 산출물 검증'
