$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$publisher = Join-Path $PSScriptRoot "publish_github_release.py"
$publishExitCode = 1

$interactive = $args.Count -eq 0
if ($interactive) {
    Write-Host "GitHub 更新发布"
    Write-Host "1. 发布管理器更新（构建、签名、上传、验证）"
    Write-Host "2. 仅生成管理器更新包（稍后手动上传）"
    Write-Host "3. 继续上次发布 / 验证已手动上传的文件"
    Write-Host "4. 回滚线上稳定清单"
    Write-Host "0. 退出"
    $choice = Read-Host "请选择"
    switch ($choice) {
        { $_ -in "1", "2" } {
            $releaseTitle = Read-Host "更新标题"
            $releaseNote = Read-Host "给用户看的更新说明"
            $args = @("--manager-only", "--title", $releaseTitle, "--note", $releaseNote)
            if ($choice -eq "2") { $args += "--dry-run" }
        }
        "3" {
            $candidateDirectory = Read-Host "粘贴上次输出的产物目录（含 publish-candidate.json）"
            $args = @("--resume", "--output", $candidateDirectory.Trim('"'))
        }
        "4" {
            $rollbackVersion = Read-Host "输入要恢复的版本号"
            $args = @("--rollback", $rollbackVersion)
        }
        default { exit 0 }
    }
}

Push-Location -LiteralPath $projectRoot
try {
    & py -3.13 $publisher @args
    $publishExitCode = $LASTEXITCODE
}
finally {
    Pop-Location
    if ($interactive) {
        Read-Host "请保存上方产物目录和结果，按 Enter 关闭" | Out-Null
    }
}

exit $publishExitCode
