[CmdletBinding()]
param(
    [string]$OutputDirectory
)

$ErrorActionPreference = 'Stop'
$designRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$prototypeRoot = Join-Path $designRoot 'prototype'
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $designRoot 'screens'
}
$OutputDirectory = [System.IO.Path]::GetFullPath($OutputDirectory)
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null

$pythonPath = (Get-Command python -ErrorAction Stop).Source
$npxPath = (Get-Command npx -ErrorAction Stop).Source
$listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
$listener.Start()
$port = ([System.Net.IPEndPoint]$listener.LocalEndpoint).Port
$listener.Stop()

$screens = @(
    @{ screen = 'design-system'; file = '00-视觉规范与组件.png'; width = 1600; height = 1000 },
    @{ screen = 'run-running'; file = '01-运行中心-机器人可用.png'; width = 1600; height = 1000 },
    @{ screen = 'run-stopped'; file = '02-运行中心-已停止.png'; width = 1600; height = 1000 },
    @{ screen = 'run-environment-error'; file = '03-运行中心-微信环境异常.png'; width = 1600; height = 1000 },
    @{ screen = 'feature-overview'; file = '04-功能中心-功能概览.png'; width = 1600; height = 1000 },
    @{ screen = 'feature-config'; file = '05-功能中心-常用设置.png'; width = 1600; height = 1000 },
    @{ screen = 'feature-advanced'; file = '06-功能中心-专业设置.png'; width = 1600; height = 1000 },
    @{ screen = 'groups'; file = '07-群聊管理-列表.png'; width = 1600; height = 1000 },
    @{ screen = 'groups-policy'; file = '08-群聊管理-群规则.png'; width = 1600; height = 1000 },
    @{ screen = 'groups-members'; file = '09-群聊管理-群成员.png'; width = 1600; height = 1000 },
    @{ screen = 'history'; file = '10-聊天记录-三栏详情.png'; width = 1600; height = 1000 },
    @{ screen = 'history-empty'; file = '11-聊天记录-空状态.png'; width = 1600; height = 1000 },
    @{ screen = 'schedules'; file = '12-定时任务.png'; width = 1600; height = 1000 },
    @{ screen = 'activity'; file = '13-运行记录.png'; width = 1600; height = 1000 },
    @{ screen = 'settings-model'; file = '14-设置-模型服务.png'; width = 1600; height = 1000 },
    @{ screen = 'settings-persona'; file = '15-设置-人格与回复.png'; width = 1600; height = 1000 },
    @{ screen = 'settings-memory'; file = '16-设置-记忆与情感.png'; width = 1600; height = 1000 },
    @{ screen = 'settings-system'; file = '17-设置-身份与维护.png'; width = 1600; height = 1000 },
    @{ screen = 'modal-onboarding'; file = '18-弹窗-首次使用引导.png'; width = 1600; height = 1000 },
    @{ screen = 'modal-unsaved'; file = '19-弹窗-未保存设置.png'; width = 1600; height = 1000 },
    @{ screen = 'modal-group-picker'; file = '20-弹窗-选择群聊.png'; width = 1600; height = 1000 },
    @{ screen = 'modal-history-delete'; file = '21-弹窗-删除聊天记录.png'; width = 1600; height = 1000 },
    @{ screen = 'modal-schedule-edit'; file = '22-弹窗-修改定时任务.png'; width = 1600; height = 1000 },
    @{ screen = 'modal-schedule-delete'; file = '23-弹窗-删除定时任务.png'; width = 1600; height = 1000 },
    @{ screen = 'modal-provider-delete'; file = '24-弹窗-删除模型服务.png'; width = 1600; height = 1000 },
    @{ screen = 'modal-backup-create'; file = '25-弹窗-创建备份.png'; width = 1600; height = 1000 },
    @{ screen = 'modal-backup-restore'; file = '26-弹窗-恢复备份.png'; width = 1600; height = 1000 },
    @{ screen = 'modal-history-clean'; file = '27-弹窗-清理聊天记录.png'; width = 1600; height = 1000 },
    @{ screen = 'modal-update-available'; file = '28-弹窗-发现新版本.png'; width = 1600; height = 1000 },
    @{ screen = 'modal-update-progress'; file = '29-弹窗-更新进度.png'; width = 1600; height = 1000 },
    @{ screen = 'dropdown-service-type'; file = '30-下拉-模型服务类型.png'; width = 1600; height = 1000 },
    @{ screen = 'dropdown-model'; file = '31-下拉-模型名称.png'; width = 1600; height = 1000 },
    @{ screen = 'dropdown-reply-mode'; file = '32-下拉-群聊回复方式.png'; width = 1600; height = 1000 },
    @{ screen = 'dropdown-persona-tone'; file = '33-下拉-人格语气.png'; width = 1600; height = 1000 },
    @{ screen = 'dropdown-feature-option'; file = '34-下拉-功能选项.png'; width = 1600; height = 1000 },
    @{ screen = 'dropdown-date'; file = '35-日期选择器.png'; width = 1600; height = 1000 },
    @{ screen = 'feedback-toasts'; file = '36-反馈-提示消息.png'; width = 1600; height = 1000 },
    @{ screen = 'run-running'; file = '37-适配-1080x680-运行中心.png'; width = 1080; height = 680 },
    @{ screen = 'feature-config'; file = '38-适配-1080x680-功能中心.png'; width = 1080; height = 680 }
)

$server = $null
$session = "wechatrebot-design-$PID"
function Invoke-DesignBrowser {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    & $npxPath --yes --package '@playwright/cli' playwright-cli "-s=$session" @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Playwright command failed: $($Arguments -join ' ')"
    }
}

try {
    $server = Start-Process `
        -FilePath $pythonPath `
        -ArgumentList @('-m', 'http.server', "$port", '--bind', '127.0.0.1') `
        -WorkingDirectory $prototypeRoot `
        -WindowStyle Hidden `
        -PassThru

    $previewUrl = "http://127.0.0.1:$port/"
    $ready = $false
    foreach ($attempt in 1..20) {
        try {
            $response = Invoke-WebRequest -Uri $previewUrl -UseBasicParsing -TimeoutSec 2
            if ($response.StatusCode -eq 200) {
                $ready = $true
                break
            }
        }
        catch {
            Start-Sleep -Milliseconds 200
        }
    }
    if (-not $ready) {
        throw 'Design preview server did not become ready.'
    }

    Invoke-DesignBrowser open "${previewUrl}?screen=design-system" '--browser=msedge'
    foreach ($item in $screens) {
        $targetUrl = "${previewUrl}?screen=$([uri]::EscapeDataString($item.screen))"
        $targetPath = (Join-Path $OutputDirectory $item.file).Replace('\', '/')
        $urlJson = ConvertTo-Json $targetUrl -Compress
        $pathJson = ConvertTo-Json $targetPath -Compress
        $captureCode = "async page => { await page.setViewportSize({ width: $($item.width), height: $($item.height) }); await page.goto($urlJson); await page.waitForLoadState('networkidle'); await page.locator('body').ariaSnapshot(); await page.screenshot({ path: $pathJson, animations: 'disabled', scale: 'css' }); }"
        Invoke-DesignBrowser run-code $captureCode | Out-Null
    }
    Invoke-DesignBrowser console error

    $files = Get-ChildItem -LiteralPath $OutputDirectory -Filter '*.png' | Sort-Object Name
    if ($files.Count -ne $screens.Count) {
        throw "Expected $($screens.Count) screenshots, found $($files.Count)."
    }
    $files | Select-Object Name, Length, LastWriteTime
}
finally {
    try {
        Invoke-DesignBrowser close
    }
    catch {
        Write-Warning $_
    }
    if ($server -and -not $server.HasExited) {
        Stop-Process -Id $server.Id
    }
}
