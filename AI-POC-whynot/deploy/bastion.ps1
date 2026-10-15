<#
.SYNOPSIS
    배스천에서 셸 스크립트를 실행하고 출력을 돌려준다.

.DESCRIPTION
    EKS API가 프라이빗 전용이라 kubectl은 배스천에서만 실행할 수 있다.
    deploy.ps1이 쓰는 SSM 경로와 같은 방식이지만, 배포가 아닌 단발 명령용이다.

    SSM --parameters에 인라인 JSON을 넘기면 PowerShell에서 인용부호가 깨진다.
    스크립트를 base64로 감싸고 파라미터는 BOM 없는 UTF-8 파일로 넘긴다(BOM이 있으면 CLI가 거부).

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File AI-POC/deploy/bastion.ps1 -Script AI-POC/infra/status-check.sh

.EXAMPLE
    powershell ... -File AI-POC/deploy/bastion.ps1 -Command "kubectl get pods -A"

.EXAMPLE
    # 스크립트에 값을 넘긴다. 로컬 셸의 환경변수는 SSM으로 전달되지 않으므로 이 방법을 쓴다.
    powershell ... -File AI-POC/deploy/bastion.ps1 `
        -Script AI-POC/infra/status-check.sh -Variables NS=ai-poc
#>
[CmdletBinding()]
param(
    [string]$Script,
    [string]$Command,
    # 원격 스크립트에 넘길 환경변수. 로컬 $env: 값은 SSM 경로로 전달되지 않는다.
    # 형식은 'KEY=VALUE'. 해시테이블은 `powershell -File` 경로에서 문자열로 눌려 쓸 수 없다.
    [string[]]$Variables,
    [int]$TimeoutMinutes = 20
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$scriptDir = $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($scriptDir)) {
    $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
}
. (Join-Path $scriptDir 'preflight.ps1') -Quiet
$config = $script:ToonConfig
$region = $config['AWS_REGION']
$instanceId = $config['BASTION_INSTANCE_ID']

if ($Script) {
    # -Encoding UTF8 필수: 기본 ANSI 해석은 스크립트의 한글 주석을 깨뜨린다.
    $body = (Get-Content -Raw -Encoding UTF8 -LiteralPath $Script) -replace "`r`n", "`n"
}
elseif ($Command) {
    $body = "#!/bin/bash`nexport HOME=/root KUBECONFIG=/root/.kube/config AWS_DEFAULT_REGION=$region`n" +
            "aws eks update-kubeconfig --region $region --name $($config['EKS_CLUSTER_NAME']) --kubeconfig `"`$KUBECONFIG`" >/dev/null 2>&1`n" +
            ($Command -replace "`r`n", "`n")
}
else {
    throw '-Script 또는 -Command 중 하나가 필요합니다.'
}

if ($Variables -and $Variables.Count -gt 0) {
    # shebang 다음 줄에 export를 끼운다. 스크립트 본문의 `set -u`보다 앞서야 한다.
    # 값은 단일 인용으로 감싸고 내부의 단일 인용만 이스케이프한다(셸 주입 방지).
    $exports = foreach ($pair in $Variables) {
        $split = $pair.IndexOf('=')
        if ($split -lt 1) { throw "Variables 항목은 'KEY=VALUE' 형식이어야 합니다: $pair" }
        $name = $pair.Substring(0, $split)
        $value = $pair.Substring($split + 1)
        if ($name -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') {
            throw "환경변수 이름이 올바르지 않습니다: $name"
        }
        "export $name='" + $value.Replace("'", "'\''") + "'"
    }
    $lines = $body -split "`n", 2
    if ($lines[0] -like '#!*') {
        $body = $lines[0] + "`n" + ($exports -join "`n") + "`n" + $lines[1]
    }
    else {
        $body = ($exports -join "`n") + "`n" + $body
    }
}

$encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($body))
$parameters = @{ commands = @("echo $encoded | base64 -d > /tmp/bastion-run.sh", 'bash /tmp/bastion-run.sh') } |
    ConvertTo-Json -Depth 3 -Compress
$parametersPath = Join-Path $env:TEMP 'ssm-bastion.json'
[IO.File]::WriteAllText($parametersPath, $parameters, (New-Object Text.UTF8Encoding($false)))

$send = Invoke-AwsCli @('ssm', 'send-command', '--instance-ids', $instanceId, '--document-name', 'AWS-RunShellScript',
    '--parameters', "file://$parametersPath", '--timeout-seconds', '3600', '--region', $region,
    '--query', 'Command.CommandId', '--output', 'text')
Remove-Item -Force -ErrorAction SilentlyContinue $parametersPath
if ($send.ExitCode -ne 0) { throw "SSM 명령 전송에 실패했습니다: $($send.Output)" }
$commandId = $send.Output.Trim()

$deadline = (Get-Date).AddMinutes($TimeoutMinutes)
$status = 'Pending'
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 10
    $poll = Invoke-AwsCli @('ssm', 'get-command-invocation', '--command-id', $commandId, '--instance-id', $instanceId, '--region', $region, '--query', 'Status', '--output', 'text')
    if ($poll.ExitCode -ne 0) { continue }
    $status = $poll.Output.Trim()
    if ($status -notin @('Pending', 'InProgress', 'Delayed')) { break }
}

$stdout = Invoke-AwsCli @('ssm', 'get-command-invocation', '--command-id', $commandId, '--instance-id', $instanceId, '--region', $region, '--query', 'StandardOutputContent', '--output', 'text')
Write-Host $stdout.Output

if ($status -ne 'Success') {
    $stderr = Invoke-AwsCli @('ssm', 'get-command-invocation', '--command-id', $commandId, '--instance-id', $instanceId, '--region', $region, '--query', 'StandardErrorContent', '--output', 'text')
    Write-Host ''
    Write-Host "=== 실패 (상태 $status) ===" -ForegroundColor Red
    if (-not [string]::IsNullOrWhiteSpace($stderr.Output)) { Write-Host $stderr.Output }
    exit 1
}
exit 0
