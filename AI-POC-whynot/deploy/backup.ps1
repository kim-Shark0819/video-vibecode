<#
.SYNOPSIS
    현재 배포된 상태를 백업한다. ECR 이미지에 보존 태그를 붙이고 Neptune 스냅샷을 만든다.

.DESCRIPTION
    18시에 자원이 내려가기 때문에, 그 전에 되돌릴 수 있는 지점을 남긴다.

    두 가지를 각각 백업한다. 대상이 다르기 때문이다.

    1. ECR 이미지: 지금 돌고 있는 빌드에 `backup-YYYYMMDD` 태그를 추가한다.
       수명주기 정책은 날짜 태그(20*)만 10개로 제한하므로 backup-* 태그는 지워지지 않는다.
    2. Neptune 스냅샷: 사용자, 프로젝트, AI 산출물 메타데이터가 여기 있다.
       **ECR은 이 데이터를 담지 않는다.** 컨테이너 이미지에는 코드만 들어 있다.

    S3(이미지·영상·원본 자료)는 버저닝이 켜져 있어 별도 백업을 만들지 않는다.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File AI-POC/deploy/backup.ps1

.EXAMPLE
    # 이미지 태그만 남기고 스냅샷은 건너뛴다
    powershell -NoProfile -ExecutionPolicy Bypass -File AI-POC/deploy/backup.ps1 -SkipSnapshot
#>
[CmdletBinding()]
param(
    [switch]$SkipSnapshot,
    [string]$Label
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$scriptDir = $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($scriptDir)) {
    $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
}

. (Join-Path $scriptDir 'preflight.ps1') -Quiet
$config = $script:ToonConfig

$stamp = if ($Label) { $Label } else { Get-Date -Format 'yyyyMMdd-HHmm' }
$backupTag = "backup-$stamp"
$sourceTag = $config['IMAGE_TAG']
$region = $config['AWS_REGION']

Write-Host "계정 $($config['AWS_ACCOUNT_ID']) / 리전 $region"
Write-Host "백업 라벨: $stamp"

# --- 1. 실제로 돌고 있는 이미지 태그를 확인한다 ---
# config.env의 IMAGE_TAG와 클러스터의 이미지가 다를 수 있다. 백업은 돌고 있는 것을 기준으로 한다.
Write-Host ''
Write-Host "=== 배포된 이미지 확인 ===" -ForegroundColor Cyan
$deployedTag = $sourceTag
$assets = Invoke-AwsCli @('ecr', 'describe-images', '--repository-name', $config['ECR_APP_REPO'],
    '--image-ids', "imageTag=$sourceTag", '--region', $region, '--query', 'imageDetails[0].imageDigest', '--output', 'text')
if ($assets.ExitCode -ne 0) {
    throw "config.env의 IMAGE_TAG($sourceTag) 이미지를 ECR에서 찾을 수 없습니다. 배포 상태를 확인하세요."
}
Write-Host "기준 태그: $deployedTag"

# --- 2. ECR 이미지에 보존 태그를 붙인다 ---
Write-Host ''
Write-Host "=== ECR 백업 태그 추가 ===" -ForegroundColor Cyan
foreach ($repo in @($config['ECR_APP_REPO'], $config['ECR_FRONTEND_REPO'])) {
    # 이미 같은 백업 태그가 있으면 건너뛴다. ECR 태그는 immutable이라 덮어쓸 수 없다.
    $existing = Invoke-AwsCli @('ecr', 'describe-images', '--repository-name', $repo,
        '--image-ids', "imageTag=$backupTag", '--region', $region, '--query', 'imageDetails[0].imageDigest', '--output', 'text')
    if ($existing.ExitCode -eq 0) {
        Write-Host "  ${repo}: $backupTag 이(가) 이미 있습니다. 건너뜁니다."
        continue
    }

    # 매니페스트를 그대로 다시 올려 태그만 추가한다. 이미지를 다시 푸시하지 않는다.
    $manifestResult = Invoke-AwsCli @('ecr', 'batch-get-image', '--repository-name', $repo,
        '--image-ids', "imageTag=$deployedTag", '--region', $region,
        '--query', 'images[0].imageManifest', '--output', 'text')
    if ($manifestResult.ExitCode -ne 0) {
        throw "${repo}:$deployedTag 매니페스트를 가져오지 못했습니다: $($manifestResult.Output)"
    }
    $manifestPath = Join-Path $env:TEMP "$repo-manifest.json"
    [IO.File]::WriteAllText($manifestPath, $manifestResult.Output, (New-Object Text.UTF8Encoding($false)))

    $put = Invoke-AwsCli @('ecr', 'put-image', '--repository-name', $repo, '--region', $region,
        '--image-tag', $backupTag, '--image-manifest', "file://$manifestPath",
        '--query', 'image.imageId.imageDigest', '--output', 'text')
    Remove-Item -Force -ErrorAction SilentlyContinue $manifestPath
    if ($put.ExitCode -ne 0) {
        throw "${repo}에 $backupTag 태그를 붙이지 못했습니다: $($put.Output)"
    }
    Write-Host "  ${repo}: $deployedTag -> $backupTag ($($put.Output.Substring(0, 19))...)"
}

# --- 3. Neptune 스냅샷 ---
if (-not $SkipSnapshot) {
    Write-Host ''
    Write-Host "=== Neptune 스냅샷 ===" -ForegroundColor Cyan
    $cluster = $config['NEPTUNE_CLUSTER_ID']
    $status = Invoke-AwsCli @('neptune', 'describe-db-clusters', '--db-cluster-identifier', $cluster,
        '--region', $region, '--query', 'DBClusters[0].Status', '--output', 'text')
    if ($status.ExitCode -ne 0) {
        Write-Host "  클러스터를 조회하지 못해 스냅샷을 건너뜁니다: $($status.Output)" -ForegroundColor Yellow
    }
    elseif ($status.Output -ne 'available') {
        # 중지된 클러스터는 스냅샷을 만들 수 없다. 이미 중지 상태면 마지막 스냅샷이 남아 있다.
        Write-Host "  클러스터 상태가 '$($status.Output)'이라 스냅샷을 만들 수 없습니다." -ForegroundColor Yellow
    }
    else {
        $snapshotId = "$cluster-$stamp"
        $create = Invoke-AwsCli @('neptune', 'create-db-cluster-snapshot',
            '--db-cluster-identifier', $cluster, '--db-cluster-snapshot-identifier', $snapshotId,
            '--region', $region, '--query', 'DBClusterSnapshot.Status', '--output', 'text')
        if ($create.ExitCode -ne 0) {
            throw "스냅샷 생성에 실패했습니다: $($create.Output)"
        }
        Write-Host "  스냅샷 생성 시작: $snapshotId ($($create.Output))"
        Write-Host "  완료까지 몇 분 걸립니다. 상태는 아래 명령으로 확인합니다."
        Write-Host "    aws neptune describe-db-cluster-snapshots --db-cluster-snapshot-identifier $snapshotId --query 'DBClusterSnapshots[0].[Status,PercentProgress]'"
    }
}

# --- 4. 결과 정리 ---
Write-Host ''
Write-Host "=== 백업 목록 ===" -ForegroundColor Cyan
foreach ($repo in @($config['ECR_APP_REPO'], $config['ECR_FRONTEND_REPO'])) {
    $tags = Invoke-AwsCli @('ecr', 'describe-images', '--repository-name', $repo, '--region', $region,
        '--query', 'sort_by(imageDetails,&imagePushedAt)[].imageTags[]', '--output', 'text')
    $backups = ($tags.Output -split '\s+') | Where-Object { $_ -like 'backup-*' }
    Write-Host "  ${repo}: $($backups -join ', ')"
}
$snapshots = Invoke-AwsCli @('neptune', 'describe-db-cluster-snapshots',
    '--db-cluster-identifier', $config['NEPTUNE_CLUSTER_ID'], '--snapshot-type', 'manual',
    '--region', $region, '--query', 'DBClusterSnapshots[].[DBClusterSnapshotIdentifier,Status]', '--output', 'text')
if ($snapshots.ExitCode -eq 0 -and $snapshots.Output) {
    Write-Host "  Neptune 수동 스냅샷:"
    foreach ($line in ($snapshots.Output -split "`n")) {
        if ($line.Trim()) { Write-Host "    $($line.Trim())" }
    }
}

Write-Host ''
Write-Host "백업 완료. 라벨 $stamp" -ForegroundColor Green
