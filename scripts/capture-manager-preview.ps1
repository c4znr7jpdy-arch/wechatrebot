[CmdletBinding()]
param(
    [string]$OutputDirectory
)

$ErrorActionPreference = 'Stop'
$repositoryRoot = Split-Path -Parent $PSScriptRoot
$webRoot = Join-Path $repositoryRoot 'src\wechatrebot_manager\app_webui'
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $repositoryRoot 'output\playwright'
}
$OutputDirectory = [System.IO.Path]::GetFullPath($OutputDirectory)
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null

$python = (Get-Command python -ErrorAction Stop).Source
$npx = (Get-Command npx -ErrorAction Stop).Source
$listener = [System.Net.Sockets.TcpListener]::new(
    [System.Net.IPAddress]::Loopback,
    0
)
$listener.Start()
$port = ([System.Net.IPEndPoint]$listener.LocalEndpoint).Port
$listener.Stop()

$server = $null
$session = "manager-preview-$PID"
function Invoke-PreviewBrowser {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    & $npx --yes --package '@playwright/cli' playwright-cli "-s=$session" @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Playwright command failed: $($Arguments -join ' ')"
    }
}

try {
    $server = Start-Process `
        -FilePath $python `
        -ArgumentList @('-m', 'http.server', "$port", '--bind', '127.0.0.1') `
        -WorkingDirectory $webRoot `
        -WindowStyle Hidden `
        -PassThru

    $previewUrl = "http://127.0.0.1:$port/?preview"
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
            Start-Sleep -Milliseconds 250
        }
    }
    if (-not $ready) {
        throw 'Manager preview server did not become ready.'
    }

    Invoke-PreviewBrowser open $previewUrl '--browser=msedge'
    Invoke-PreviewBrowser resize 1440 960
    Invoke-PreviewBrowser screenshot '--filename' (Join-Path $OutputDirectory 'manager-first-run.png') '--full-page'
    Invoke-PreviewBrowser run-code "async page => { await page.locator('#onboardingRecommended').click(); await page.locator('#onboardingBackdrop').waitFor({ state: 'hidden' }); }"
    Invoke-PreviewBrowser screenshot '--filename' (Join-Path $OutputDirectory 'manager-run-center.png') '--full-page'

    Invoke-PreviewBrowser run-code "async page => { await page.locator('[data-page=groups]').click(); await page.locator('#groupRows .group-row').first().waitFor(); await page.locator('#refreshGroups').click(); await page.locator('#refreshGroups:not([aria-busy=true])').waitFor(); }"
    Invoke-PreviewBrowser screenshot '--filename' (Join-Path $OutputDirectory 'manager-group-directory.png') '--full-page'
    Invoke-PreviewBrowser run-code "async page => { await page.locator('#groupRows .inspect-group').first().click(); await page.locator('#groupPolicyForm').waitFor(); }"
    Invoke-PreviewBrowser screenshot '--filename' (Join-Path $OutputDirectory 'manager-group-policy.png') '--full-page'
    Invoke-PreviewBrowser run-code "async page => { await page.locator('[data-inspector-view=members]').first().click(); await page.getByText('张三｜3栋', {exact:true}).waitFor(); }"
    Invoke-PreviewBrowser screenshot '--filename' (Join-Path $OutputDirectory 'manager-group-members.png') '--full-page'

    Invoke-PreviewBrowser run-code "async page => { await page.locator('[data-page=history]').click(); await page.locator('.conversation-item').first().waitFor(); if(await page.locator('.conversation-item').count()!==5) throw new Error('Missing empty groups in chat directory'); await page.evaluate(()=>{const original=window.pywebview.api.get_messages;window.previewMessagesOriginal=original;window.previewMessagesCalls=0;window.pywebview.api.get_messages=async(...args)=>{const result=await original(...args);window.previewMessagesCalls++;if(window.previewMessagesCalls>=2){const message={id:999,role:'user',sender_id:'wxid_new',sender_name:'新成员',avatar:'',content:'这是一条实时到达的消息',timestamp:Date.now()/1000};if(Array.isArray(result.data))result.data=[...result.data,message];else result.data={...result.data,items:[...(result.data.items||[]),message]};}return result;};}); await page.locator('.conversation-item').first().click(); await page.getByText('这是一条实时到达的消息',{exact:true}).waitFor({timeout:5000}); const atBottom=await page.locator('#messageList').evaluate(node=>node.scrollHeight-node.scrollTop-node.clientHeight<8); if(!atBottom) throw new Error('New messages did not auto-scroll'); await page.evaluate(()=>{window.pywebview.api.get_messages=window.previewMessagesOriginal;}); }"
    Invoke-PreviewBrowser screenshot '--filename' (Join-Path $OutputDirectory 'manager-chat-live.png') '--full-page'
    Invoke-PreviewBrowser resize 430 932
    Invoke-PreviewBrowser run-code "async page => { const width=await page.evaluate(()=>({viewport:innerWidth,document:document.documentElement.scrollWidth})); if(width.document>width.viewport) throw new Error('Group page overflow: '+JSON.stringify(width)); }"
    Invoke-PreviewBrowser screenshot '--filename' (Join-Path $OutputDirectory 'manager-group-directory-mobile.png') '--full-page'
    Invoke-PreviewBrowser resize 1440 960

    Invoke-PreviewBrowser reload
    Invoke-PreviewBrowser run-code "async page => { if (await page.locator('#onboardingBackdrop:not([hidden])').count()) await page.locator('#onboardingRecommended').click(); }"
    Invoke-PreviewBrowser run-code "async page => { await page.locator('[data-page=features]').click(); await page.locator('#featureAudience [data-audience=all]').click(); await page.addStyleTag({ content: 'html,body{height:auto!important;overflow:visible!important}.app-shell{height:auto!important;min-height:100vh}.main-area{grid-template-rows:70px auto!important}.content-scroll{overflow:visible!important}.feature-layout{grid-template-columns:minmax(0,720px)!important}.feature-detail{display:none!important}.feature-catalog,.feature-list{overflow:visible!important;max-height:none!important}.feature-list{flex-shrink:0!important}' }); await page.waitForTimeout(250); }"
    Invoke-PreviewBrowser screenshot '--filename' (Join-Path $OutputDirectory 'manager-all-plugins.png') '--full-page'

    Invoke-PreviewBrowser reload
    Invoke-PreviewBrowser run-code "async page => { if (await page.locator('#onboardingBackdrop:not([hidden])').count()) await page.locator('#onboardingRecommended').click(); }"
    Invoke-PreviewBrowser run-code "async page => { await page.locator('[data-page=features]').click(); await page.getByText('群聊分析', { exact: true }).first().click(); await page.locator('.content-scroll').evaluate(node => node.scrollTo(0, 0)); await page.waitForTimeout(250); }"
    Invoke-PreviewBrowser screenshot '--filename' (Join-Path $OutputDirectory 'manager-group-analysis.png') '--full-page'

    # Exercise the real UI with an isolated bridge fixture; never touches a bot process.
    Invoke-PreviewBrowser run-code "async page => { await page.getByLabel('统计最近几天', {exact:true}).fill('7'); await page.locator('[data-tab=models]').click(); await page.locator('[data-tab=basic]').click(); if (await page.getByLabel('统计最近几天', {exact:true}).inputValue() !== '7') throw new Error('Draft lost across tabs'); await page.locator('[data-page=settings]').click(); await page.locator('#modalCancel').click(); if (!(await page.locator('#page-features').isVisible())) throw new Error('Cancel discarded navigation guard'); await page.locator('#saveFeature').click(); await page.locator('#featureNote').filter({hasText:'确认生效'}).waitFor(); if (await page.getByLabel('统计最近几天', {exact:true}).inputValue() !== '7') throw new Error('Saved value not read back'); }"
    Invoke-PreviewBrowser screenshot '--filename' (Join-Path $OutputDirectory 'manager-hot-config-saved.png') '--full-page'
    Invoke-PreviewBrowser run-code "async page => { await page.getByLabel('统计最近几天', {exact:true}).fill('31'); await page.locator('#saveFeature').click(); if (await page.getByLabel('统计最近几天', {exact:true}).getAttribute('aria-invalid') !== 'true') throw new Error('Missing field validation'); await page.getByLabel('统计最近几天', {exact:true}).fill('8'); await page.evaluate(() => { window.previewSaveOriginal = window.pywebview.api.save_feature; window.pywebview.api.save_feature = async () => ({ok:false,error:{message:'新设置未能生效，已恢复原设置。'}}); }); await page.locator('#saveFeature').click(); await page.locator('#featureNote').filter({hasText:'已恢复原设置'}).waitFor(); if (await page.getByLabel('统计最近几天', {exact:true}).inputValue() !== '8') throw new Error('Input lost after failure'); await page.evaluate(() => { window.pywebview.api.save_feature = window.previewSaveOriginal; }); }"
    Invoke-PreviewBrowser screenshot '--filename' (Join-Path $OutputDirectory 'manager-hot-config-error.png') '--full-page'
    Invoke-PreviewBrowser run-code "async page => { await page.evaluate(() => { const original=window.pywebview.api.save_feature; window.previewSaveOriginal=original; window.previewSaveCount=0; window.pywebview.api.save_feature=(...args)=>new Promise(resolve=>{window.previewSaveCount++;window.finishPreviewSave=async()=>resolve(await original(...args));}); }); await page.locator('#saveFeature').click(); await page.locator('#saveFeature[aria-busy=true]').waitFor(); await page.locator('#saveFeature').evaluate(n=>n.click()); await page.locator('[data-page=settings]').click(); if(!(await page.locator('#page-features').isVisible())) throw new Error('Navigation allowed during save'); if(await page.evaluate(()=>window.previewSaveCount)!==1) throw new Error('Duplicate save'); await page.evaluate(()=>window.finishPreviewSave()); await page.locator('#featureNote').filter({hasText:'确认生效'}).waitFor(); await page.evaluate(()=>{window.pywebview.api.save_feature=window.previewSaveOriginal;}); }"
    Invoke-PreviewBrowser run-code "async page => { await page.locator('#saveFeature').click(); await page.locator('#featureNote').filter({hasText:'确认生效'}).waitFor(); await page.locator('[data-tab=advanced]').click(); await page.locator('[data-tab=basic]').click(); await page.getByLabel('报告样式', {exact:true}).focus(); await page.keyboard.press('Alt+ArrowDown'); await page.keyboard.press('Escape'); const labels=await page.locator('#featureForm .config-input').evaluateAll(nodes=>nodes.every(n=>n.labels && n.labels.length)); if(!labels) throw new Error('Missing form labels'); }"
    Invoke-PreviewBrowser run-code "async page => { await page.evaluate(()=>{window.previewGetOriginal=window.pywebview.api.get_feature;window.pywebview.api.get_feature=async(...args)=>{const result=await window.previewGetOriginal(...args);result.data.advanced_schema.saved_choice={type:'string',description:'保留现有单选',options:['new']};result.data.advanced_schema.saved_choices={type:'list',description:'保留现有多选',options:['new']};result.data.advanced_config.saved_choice='legacy';result.data.advanced_config.saved_choices=['legacy'];return result;};}); await page.locator('#refreshFeature').click(); await page.locator('[data-tab=advanced]').click(); if(await page.getByLabel('保留现有单选',{exact:true}).inputValue()!=='legacy') throw new Error('Unlisted select overwritten'); if(!(await page.getByLabel('legacy（当前值）',{exact:true}).isChecked())) throw new Error('Unlisted checklist lost'); await page.locator('[data-tab=basic]').click(); await page.locator('[data-tab=advanced]').click(); if(await page.getByLabel('保留现有单选',{exact:true}).inputValue()!=='legacy') throw new Error('Unlisted draft lost'); await page.locator('[data-tab=basic]').click(); await page.evaluate(()=>{window.pywebview.api.get_feature=window.previewGetOriginal;}); }"
    Invoke-PreviewBrowser run-code "async page => { for(const width of [320,768,1024,1440]) {await page.setViewportSize({width,height:960}); const bounds=await page.evaluate(()=>{const content=document.querySelector('.content-scroll');return {viewport:innerWidth,document:document.documentElement.scrollWidth,content:content.scrollWidth,available:content.clientWidth};}); if(bounds.document>bounds.viewport || bounds.content>bounds.available) throw new Error('Responsive overflow: '+JSON.stringify(bounds));} }"
    Invoke-PreviewBrowser resize 430 932
    Invoke-PreviewBrowser run-code "async page => { await page.locator('.content-scroll').evaluate(node => node.scrollTo(0, 440)); const width=await page.evaluate(()=>({viewport:innerWidth,content:document.documentElement.scrollWidth})); if(width.content>width.viewport) throw new Error('Narrow feature overflow'); }"
    Invoke-PreviewBrowser screenshot '--filename' (Join-Path $OutputDirectory 'manager-hot-config-mobile.png') '--full-page'
    Invoke-PreviewBrowser resize 1440 960

    Invoke-PreviewBrowser run-code "async page => { await page.evaluate(() => { const get=window.pywebview.api.get_feature; const save=window.pywebview.api.save_feature; window.previewGetOriginal=get; window.previewSaveOriginal=save; window.pywebview.api.get_feature=async(...args)=>{const result=await get(...args);result.data.engine_online=false;return result;}; window.pywebview.api.save_feature=async(...args)=>{const result=await save(...args);result.data.apply_state='pending_start';result.data.message='已保存到本机，尚未确认生效。机器人启动后加载；若仍在运行，请恢复连接后重新保存。';return result;}; }); await page.locator('#refreshFeature').click(); await page.locator('#featureNote').filter({hasText:'暂未连接'}).waitFor(); await page.getByLabel('统计最近几天',{exact:true}).fill('6'); await page.locator('#saveFeature').click(); await page.locator('#featureNote').filter({hasText:'尚未确认生效'}).waitFor(); await page.evaluate(()=>{window.pywebview.api.get_feature=window.previewGetOriginal;window.pywebview.api.save_feature=window.previewSaveOriginal;}); }"
    Invoke-PreviewBrowser screenshot '--filename' (Join-Path $OutputDirectory 'manager-hot-config-offline.png') '--full-page'

    Invoke-PreviewBrowser run-code "async page => { await page.locator('[data-page=settings]').click(); await page.locator('.content-scroll').evaluate(node => node.scrollTo(0, 0)); await page.waitForTimeout(250); }"
    Invoke-PreviewBrowser screenshot '--filename' (Join-Path $OutputDirectory 'manager-grok-settings.png') '--full-page'

    Invoke-PreviewBrowser run-code "async page => { const editors=page.locator('[data-provider-edit]').filter({hasText:'编辑'}); if(await editors.count()>1){const deepseek=editors.nth(1);await deepseek.click();const input=page.locator('#providerForm [name=model]');const before=await input.inputValue();await page.locator('#discoverProviderModels').click();await page.locator('#providerModelHint').filter({hasText:'已读取 6 个模型'}).waitFor();if(await input.inputValue()!==before)throw new Error('Model discovery overwrote the current selection');await input.click();if(await page.locator('#providerModelOptions .model-option').count()!==6)throw new Error('Model discovery did not show the complete list');await page.locator('#deleteProvider').click();await page.locator('#modalConfirm').click();await page.locator('#modalBackdrop').waitFor({state:'hidden'});if(await page.getByText('DeepSeek · v4-flash',{exact:true}).count())throw new Error('Provider delete did not update the list');} }"

    Invoke-PreviewBrowser run-code "async page => { await page.locator('[data-settings-tab=memory]').click(); await page.locator('#memoryForm').scrollIntoViewIfNeeded(); await page.waitForTimeout(250); }"
    Invoke-PreviewBrowser screenshot '--filename' (Join-Path $OutputDirectory 'manager-persona-memory.png') '--full-page'

    Invoke-PreviewBrowser run-code "async page => { await page.locator('[data-page=activity]').click(); await page.locator('#generateResponseTrace').click(); await page.locator('#responseTraceList .trace-gantt-card').first().waitFor(); const bars=await page.locator('.gantt-bar').count(); if(bars!==1) throw new Error('Expected one manual test-send duration bar'); }"
    Invoke-PreviewBrowser screenshot '--filename' (Join-Path $OutputDirectory 'manager-response-trace.png') '--full-page'

    Invoke-PreviewBrowser resize 430 932
    Invoke-PreviewBrowser run-code "async page => { await page.locator('[data-page=settings]').click(); await page.locator('.content-scroll').evaluate(node => node.scrollTo(0, 0)); await page.waitForTimeout(150); }"
    Invoke-PreviewBrowser screenshot '--filename' (Join-Path $OutputDirectory 'manager-grok-settings-mobile.png') '--full-page'
    Invoke-PreviewBrowser run-code "async page => { const width = await page.evaluate(() => ({ viewport: innerWidth, content: document.documentElement.scrollWidth })); if (width.content > width.viewport) throw new Error('Mobile layout has horizontal overflow: ' + JSON.stringify(width)); return width; }"
    Invoke-PreviewBrowser console error

    Get-ChildItem -LiteralPath $OutputDirectory -Filter 'manager-*.png' |
        Sort-Object Name |
        Select-Object Name, Length, LastWriteTime
}
finally {
    try {
        Invoke-PreviewBrowser close
    }
    catch {
        Write-Warning $_
    }
    if ($server -and -not $server.HasExited) {
        Stop-Process -Id $server.Id
    }
}
