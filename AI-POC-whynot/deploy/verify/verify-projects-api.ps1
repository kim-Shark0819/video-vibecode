<#
.SYNOPSIS
    프로젝트 진입점 타입 / 기획 방향 / 원본 자료 API를 검증한다.

.DESCRIPTION
    배포된 서비스에 실제 요청을 보내 다음을 확인한다.

      - 진입점 타입(webtoon/logline/treatment/script) 저장과 잘못된 값 거부
      - 기획 방향의 기본값, 정규화(장르 문자열 분해), 범위 검증, 부분 저장 병합
      - 원본 자료의 S3 저장 / 한글 왕복 / 교체 시 이전 객체 삭제 / 권한 분리
      - 프로젝트 삭제 시 S3 객체까지 정리되는지

    테스트가 만든 프로젝트는 마지막에 삭제한다. 실패로 중단되면 남을 수 있다.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File AI-POC/deploy/verify/verify-projects-api.ps1
#>
[CmdletBinding()]
param()

. (Join-Path $PSScriptRoot 'common.ps1')

Write-Host "대상: $BaseUrl"

Write-Stage '로그인'
$t1 = Get-TestToken -Username 'user1'
$t2 = Get-TestToken -Username 'user2'
Check '토큰 발급 user1' $true ([bool]$t1)
Check '토큰 발급 user2' $true ([bool]$t2)

Write-Stage '진입점 타입으로 프로젝트 생성'
$created = Invoke-Api -Method POST -Path '/api/projects' -Token $t1 -BodyObj @{
    title = $Fixtures.title; story = $Fixtures.story; visibility = 'private'; inputType = 'logline'
}
$projId = $created.item.id
Check '진입점 타입 저장' 'logline' $created.item.inputType
Check '기획 방향 기본 화수' 4 $created.item.direction.episodeCount
Check '기획 방향 기본 플랫폼' 'OTT' $created.item.direction.targetPlatform
Check '원본 자료 초기 분량' 0 $created.item.source.charCount
Check '잘못된 진입점 타입 거부' 400 (Get-ApiStatus -Method POST -Path '/api/projects' -Token $t1 -BodyObj @{ title = 'x'; visibility = 'private'; inputType = 'novel' })

Write-Stage '기획 방향 저장'
$dir = Invoke-Api -Method PUT -Path "/api/projects/$projId/direction" -Token $t1 -BodyObj @{
    targetPlatform = 'Netflix'; episodeCount = 6; genres = $Fixtures.genres
    audience = $Fixtures.audience; tone = $Fixtures.tone; freeDirection = $Fixtures.freeDirection
}
Check '플랫폼' 'Netflix' $dir.item.direction.targetPlatform
Check '화수' 6 $dir.item.direction.episodeCount
Check '장르 문자열 분해' 3 $dir.item.direction.genres.Count
Check '장르 공백 제거' $true ($dir.item.direction.genres -contains 'SF')
Check '톤앤매너 한글 왕복' $Fixtures.tone $dir.item.direction.tone
Check '추가 지시 한글 왕복' $Fixtures.freeDirection $dir.item.direction.freeDirection
Check '주 시청층 한글 왕복' $Fixtures.audience $dir.item.direction.audience

Write-Stage '기획 방향 검증 규칙'
# 0은 falsy라서 기본값으로 조용히 대체될 수 있는 값이다. 반드시 거부되어야 한다.
Check '화수 0 거부' 400 (Get-ApiStatus -Method PUT -Path "/api/projects/$projId/direction" -Token $t1 -BodyObj @{ episodeCount = 0 })
Check '화수 30 거부' 400 (Get-ApiStatus -Method PUT -Path "/api/projects/$projId/direction" -Token $t1 -BodyObj @{ episodeCount = 30 })
Check '화수 문자열 거부' 400 (Get-ApiStatus -Method PUT -Path "/api/projects/$projId/direction" -Token $t1 -BodyObj @{ episodeCount = 'many' })

Write-Stage '기획 방향 부분 저장'
$partial = Invoke-Api -Method PUT -Path "/api/projects/$projId/direction" -Token $t1 -BodyObj @{ tone = 'temporary tone' }
Check '부분 저장이 화수를 유지' 6 $partial.item.direction.episodeCount
Check '부분 저장이 장르를 유지' 3 $partial.item.direction.genres.Count
Check '부분 저장이 톤을 갱신' 'temporary tone' $partial.item.direction.tone
$cleared = Invoke-Api -Method PUT -Path "/api/projects/$projId/direction" -Token $t1 -BodyObj @{ tone = '' }
Check '빈 문자열은 지움으로 처리' '' $cleared.item.direction.tone
Check '지워도 플랫폼은 유지' 'Netflix' $cleared.item.direction.targetPlatform
$restored = Invoke-Api -Method PUT -Path "/api/projects/$projId/direction" -Token $t1 -BodyObj @{ tone = $Fixtures.tone }
Check '톤 복원' $Fixtures.tone $restored.item.direction.tone

Write-Stage '원본 자료 저장'
Check '20자 미만 거부' 400 (Get-ApiStatus -Method POST -Path "/api/projects/$projId/source" -Token $t1 -BodyObj @{ text = 'too short' })
$src = Invoke-Api -Method POST -Path "/api/projects/$projId/source" -Token $t1 -BodyObj @{ text = $Fixtures.sourceText; name = 'logline.txt' }
$key1 = $src.item.source.key
Check '분량 기록' $Fixtures.sourceText.Trim().Length $src.charCount
Check '파일명 기록' 'logline.txt' $src.item.source.name
# IRSA 정책이 users/* 로 제한되어 있어 이 prefix를 벗어나면 저장 자체가 실패한다.
Check 'S3 키 prefix' $true ($key1 -like "users/user1/sources/$projId/*")

$got = Invoke-Api -Method GET -Path "/api/projects/$projId/source" -Token $t1
Check '본문 한글 왕복(S3 경유)' $Fixtures.sourceText.Trim() $got.text

Write-Stage '원본 자료 교체'
$src2 = Invoke-Api -Method POST -Path "/api/projects/$projId/source" -Token $t1 -BodyObj @{ text = $Fixtures.sourceTextReplaced; name = 'logline-v2.txt' }
$key2 = $src2.item.source.key
Check '새 키 발급' $true ($key1 -ne $key2)
Check '분량 갱신' $Fixtures.sourceTextReplaced.Trim().Length $src2.item.source.charCount
Check '이전 자료 S3에서 삭제' $true (Test-S3ObjectMissing -Key $key1)
Check '새 자료 S3에 존재' $false (Test-S3ObjectMissing -Key $key2)

Write-Stage '권한 분리'
Check '타인의 개인 프로젝트 자료 조회 거부' 403 (Get-ApiStatus -Method GET -Path "/api/projects/$projId/source" -Token $t2)
Check '타인의 개인 프로젝트 자료 저장 거부' 403 (Get-ApiStatus -Method POST -Path "/api/projects/$projId/source" -Token $t2 -BodyObj @{ text = $Fixtures.sourceText })
Check '타인의 기획 방향 저장 거부' 403 (Get-ApiStatus -Method PUT -Path "/api/projects/$projId/direction" -Token $t2 -BodyObj @{ episodeCount = 2 })

Write-Stage '진입점 타입 변경'
$patched = Invoke-Api -Method PATCH -Path "/api/projects/$projId" -Token $t1 -BodyObj @{ inputType = 'script' }
Check '타입 변경' 'script' $patched.item.inputType
Check '변경 후 기획 방향 유지' 6 $patched.item.direction.episodeCount
Check '변경 후 원본 자료 유지' $key2 $patched.item.source.key
Check '잘못된 타입 변경 거부' 400 (Get-ApiStatus -Method PATCH -Path "/api/projects/$projId" -Token $t1 -BodyObj @{ inputType = 'comic' })

Write-Stage '목록 응답'
$list = Invoke-Api -Method GET -Path '/api/projects' -Token $t1
$mine = $list.private | Where-Object { $_.id -eq $projId }
Check '목록에 진입점 타입 포함' 'script' $mine.inputType
Check '목록에 기획 방향 포함' 6 $mine.direction.episodeCount

Write-Stage '삭제와 정리'
$deleted = Invoke-Api -Method DELETE -Path "/api/projects/$projId" -Token $t1
Check '삭제 응답' $projId $deleted.deleted
Check '삭제 후 조회 404' 404 (Get-ApiStatus -Method GET -Path "/api/projects/$projId" -Token $t1)
Start-Sleep -Seconds 2
Check '삭제 후 원본 자료 정리' 0 (Get-S3ObjectCount -Prefix "users/user1/sources/$projId/")

Complete-Verify -Name '프로젝트 API 검증'
