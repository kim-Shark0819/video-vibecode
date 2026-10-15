<#
  배포되는 소스의 신원(커밋 SHA + dirty 여부)을 만든다. F-24.

  ## 왜 필요한가

  이 파이프라인은 GitLab 을 거치지 않고 **로컬 워킹 디렉터리를 그대로** tar 로 묶어 배포한다.
  편리하지만 배포된 이미지에 어떤 소스가 들어갔는지 태그만으로는 알 수 없다.

  영상 품질을 A/B 하려면 결과마다 코드 버전이 붙어야 한다. "이 설정이 더 좋았다"는 결론이
  어느 코드에서 나온 것인지 모르면 되짚을 수 없고, 같은 실험을 반복할 수도 없다.

  ## 왜 막지 않는가

  dirty 상태를 막으면 지금 개발 흐름에서 아무것도 배포할 수 없다. 그래서 **기록하고 알린다.**
  `-dirty` 접미사가 붙은 결과는 "커밋으로 되짚을 수 없는 결과"라는 뜻이고, 그 사실 자체가
  기록해야 할 정보다. 감추면 나중에 신뢰할 수 없는 수치를 신뢰하게 된다.

  ## 왜 두 배포 스크립트가 공유하는가

  deploy.ps1(앱)과 deploy-video-gen.ps1(추론 서버)이 같은 판정을 해야 한다. 각자 구현하면
  한쪽만 고쳐서 두 이미지의 스탬프가 어긋나고, 그러면 "어느 조합이었나"를 알 수 없다.
#>

function Get-ArchiveStamp {
    <#
      .SYNOPSIS
        배포 아카이브의 SHA256 으로 소스 신원을 만든다. **이것이 기준값이다.**

      .DESCRIPTION
        커밋 SHA 로는 부족하다. 이 파이프라인은 로컬 워킹 디렉터리를 배포하므로
        `eca71d0-dirty` 같은 값이 나오는데, 그것은 "42건이 바뀌었다"까지만 말하고
        무엇이 바뀌었는지는 말하지 않는다. 같은 커밋에서 서로 다른 두 배포가 같은 스탬프를 갖는다.

        아카이브 해시는 **실제로 빌드에 들어간 바이트**를 가리킨다. 두 실험 결과의 스탬프가
        같으면 같은 소스이고, 다르면 다른 소스다. 재현성 기록에 필요한 성질이 이것이다.

        한 가지 성질은 알고 있어야 한다. `tar -czf` 는 파일 수정 시각을 담으므로
        **내용이 같아도 다시 묶으면 해시가 달라진다.** 즉 이 값은 "같은 내용"이 아니라
        "같은 아카이브"를 뜻한다. 유일하게 가리키는 목적에는 충분하고, 오히려
        "같은 소스를 다시 빌드했다"를 구분해 주므로 실험 기록에는 유리하다.
        내용 기준 canonical 해시가 필요해지면 tar 를 결정적으로 만들어야 한다
        (--sort=name --mtime 고정 등). 지금은 필요하지 않다.

      .OUTPUTS
        @{ Stamp='archive:1a2b...'; Sha256='<64자>'; Short='1a2b...'; Key='_build/...' }
    #>
    param(
        [Parameter(Mandatory = $true)][string]$ArchivePath,
        [Parameter(Mandatory = $true)][string]$S3Key
    )
    if (-not (Test-Path -LiteralPath $ArchivePath)) {
        throw "아카이브를 찾을 수 없습니다: $ArchivePath"
    }
    $hash = (Get-FileHash -LiteralPath $ArchivePath -Algorithm SHA256).Hash.ToLower()
    $short = $hash.Substring(0, 16)
    return @{
        Stamp  = "archive:$short"
        Sha256 = $hash
        Short  = $short
        Key    = $S3Key
    }
}


function Get-SourceStamp {
    <#
      .SYNOPSIS
        저장소 상태를 읽어 스탬프와 상세를 돌려준다.
      .PARAMETER RepoRoot
        git 저장소 경로. 하위 디렉터리를 줘도 git 이 위로 찾아간다.
      .OUTPUTS
        @{ Stamp = 'eca71d0-dirty'; Sha = 'eca71d0'; Dirty = $true; DirtyCount = 42 }
    #>
    param([Parameter(Mandatory = $true)][string]$RepoRoot)

    $sha = 'unknown'
    $dirty = $true
    $count = 0
    try {
        $found = (& git -C $RepoRoot rev-parse --short HEAD 2>$null)
        if ($LASTEXITCODE -eq 0 -and $found) { $sha = $found.Trim() }
        # --porcelain 은 변경이 없으면 아무것도 내지 않는다. 종료 코드로는 구분되지 않아
        # 줄 수를 센다. 미추적 파일도 포함된다(그것도 배포에 들어가므로 세는 것이 맞다).
        $porcelain = @(& git -C $RepoRoot status --porcelain 2>$null)
        $count = $porcelain.Count
        $dirty = $count -gt 0
    }
    catch {
        # git 이 없거나 저장소가 아닌 경우. 배포를 막지 않고 unknown 으로 남긴다.
        $sha = 'unknown'
        $dirty = $true
    }

    return @{
        Stamp      = if ($dirty) { "$sha-dirty" } else { $sha }
        Sha        = $sha
        Dirty      = $dirty
        DirtyCount = $count
    }
}

function Write-SourceStamp {
    <# 스탬프를 사람이 읽게 출력한다. 두 배포 스크립트가 같은 문구를 쓴다. #>
    param(
        [Parameter(Mandatory = $true)]$Info,
        [switch]$AllowDirty
    )
    Write-Host "  커밋: $($Info.Sha)"
    if (-not $Info.Dirty) {
        Write-Host '  워킹 디렉터리가 깨끗합니다.'
        return
    }
    Write-Host "  커밋되지 않은 변경 $($Info.DirtyCount) 건이 이 배포에 포함됩니다." -ForegroundColor Yellow
    Write-Host '  배포된 이미지를 커밋으로 되짚을 수 없습니다. 품질 실험 기록에는 커밋 SHA 가 필요합니다.' -ForegroundColor Yellow
    if (-not $AllowDirty) {
        Write-Host '  계속합니다. 이 경고를 숨기려면 -AllowDirty 를 주세요.' -ForegroundColor DarkGray
    }
}
