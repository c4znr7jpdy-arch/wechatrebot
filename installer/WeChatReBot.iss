#ifndef AppVersion
  #error AppVersion is required
#endif

#ifndef ManagerExe
  #error ManagerExe is required
#endif

#ifdef PayloadUrl
  #ifndef PayloadSHA256
    #error PayloadSHA256 is required for an online installer
  #endif
  #define OnlinePayload
#else
  #ifndef PayloadArchive
    #error PayloadArchive is required for an offline installer
  #endif
#endif

#define ProductName "WeChat ReBot"
#define ProductPublisher "WeChat ReBot"
#ifdef SmokeInstaller
  #define ProductAppIdRegistry "{374E927D-9F43-44D6-81C5-E89E7668F7B2}_is1"
#else
  #define ProductAppIdRegistry "{1C4802AA-38EB-4B98-9A1A-8D14A6C7DC31}_is1"
#endif

[Setup]
#ifdef SmokeInstaller
AppId={{374E927D-9F43-44D6-81C5-E89E7668F7B2}
#else
AppId={{1C4802AA-38EB-4B98-9A1A-8D14A6C7DC31}
#endif
AppName={#ProductName}
AppVersion={#AppVersion}
AppVerName={#ProductName} {#AppVersion}
AppPublisher={#ProductPublisher}
AppPublisherURL=https://ss.xiuxianjyj.xin/
AppSupportURL=https://ss.xiuxianjyj.xin/
AppUpdatesURL=https://ss.xiuxianjyj.xin/wechatrebot-updates/
VersionInfoVersion={#AppVersion}
VersionInfoCompany={#ProductPublisher}
VersionInfoDescription={#ProductName} 标准安装程序
VersionInfoProductName={#ProductName}
DefaultDirName={code:DefaultInstallDirectory}
DefaultGroupName={#ProductName}
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
UsePreviousPrivileges=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
SetupArchitecture=x64
MinVersion=10.0.17763
WizardStyle=modern
DisableWelcomePage=no
DisableReadyPage=no
DisableProgramGroupPage=auto
AllowNoIcons=yes
AllowCancelDuringInstall=no
CloseApplications=yes
RestartApplications=no
SetupMutex=WeChatReBotSetupMutex
AppMutex=WeChatReBotManagerMutex
SetupIconFile=..\src\wechatrebot_manager\app_webui\assets\app-icon.ico
UninstallDisplayIcon={app}\manager\WeChatReBot.exe
Compression=lzma2/ultra64
SolidCompression=yes
ArchiveExtraction=full
OutputBaseFilename=WeChatReBot-Setup-{#AppVersion}
CreateUninstallRegKey=yes
Uninstallable=yes
UninstallDisplayName={#ProductName}
ChangesEnvironment=no

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加选项："; Flags: unchecked

[Files]
Source: "{#ManagerExe}"; DestDir: "{tmp}"; DestName: "WeChatReBot-bootstrap.exe"; Flags: dontcopy noencryption
#ifndef OnlinePayload
Source: "{#PayloadArchive}"; DestDir: "{tmp}"; DestName: "payload.zip"; Flags: dontcopy noencryption
#endif

[Icons]
Name: "{autoprograms}\{#ProductName}"; Filename: "{app}\启动机器人.exe"; WorkingDir: "{app}"; IconFilename: "{app}\manager\WeChatReBot.exe"
Name: "{autodesktop}\{#ProductName}"; Filename: "{app}\启动机器人.exe"; WorkingDir: "{app}"; IconFilename: "{app}\manager\WeChatReBot.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\启动机器人.exe"; Description: "启动 {#ProductName}"; Flags: postinstall nowait skipifsilent runasoriginaluser

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

[Code]
const
  LegacyUninstallKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\WeChatReBot';
  NativeUninstallKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{#ProductAppIdRegistry}';

var
  DownloadPage: TDownloadWizardPage;
  ExtractionPage: TExtractionWizardPage;
  InstallPage: TOutputMarqueeProgressWizardPage;
  PayloadArchivePath: String;
  PayloadStagingPath: String;

function RegistryInstallLocation(Root: HKEY; const Key: String): String;
begin
  Result := '';
  RegQueryStringValue(Root, Key, 'InstallLocation', Result);
end;
function DefaultInstallDirectory(Param: String): String;
begin
  Result := RegistryInstallLocation(HKEY_CURRENT_USER, NativeUninstallKey);
  if Result = '' then
    Result := RegistryInstallLocation(HKEY_LOCAL_MACHINE_64, NativeUninstallKey);
  if Result = '' then
    Result := RegistryInstallLocation(HKEY_CURRENT_USER, LegacyUninstallKey);
  if Result = '' then
    Result := RegistryInstallLocation(HKEY_LOCAL_MACHINE_64, LegacyUninstallKey);
  if Result = '' then
    Result := ExpandConstant('{autopf}\WeChatReBot');
end;

function RegistryVersion(Root: HKEY; const Key: String; var Version: Int64): Boolean;
var
  VersionText: String;
begin
  Result := RegQueryStringValue(Root, Key, 'DisplayVersion', VersionText) and
    StrToVersion(VersionText, Version);
end;

procedure ConsiderInstalledVersion(Root: HKEY; const Key: String; var Found: Boolean;
  var Highest: Int64);
var
  Candidate: Int64;
begin
  if RegistryVersion(Root, Key, Candidate) then
  begin
    if (not Found) or (ComparePackedVersion(Candidate, Highest) > 0) then
      Highest := Candidate;
    Found := True;
  end;
end;

function HighestInstalledVersion(var Version: Int64): Boolean;
begin
  Result := False;
  Version := 0;
  ConsiderInstalledVersion(HKEY_CURRENT_USER, NativeUninstallKey, Result, Version);
  ConsiderInstalledVersion(HKEY_LOCAL_MACHINE_64, NativeUninstallKey, Result, Version);
  ConsiderInstalledVersion(HKEY_CURRENT_USER, LegacyUninstallKey, Result, Version);
  ConsiderInstalledVersion(HKEY_LOCAL_MACHINE_64, LegacyUninstallKey, Result, Version);
end;

function InitializeSetup: Boolean;
var
  InstalledVersion: Int64;
  SetupVersion: Int64;
begin
  Result := True;
  if not StrToVersion('{#AppVersion}', SetupVersion) then
  begin
    SuppressibleMsgBox('安装程序版本号无效，无法继续。', mbCriticalError, MB_OK, IDOK);
    Result := False;
    Exit;
  end;

  if HighestInstalledVersion(InstalledVersion) and
    (ComparePackedVersion(InstalledVersion, SetupVersion) > 0) then
  begin
    SuppressibleMsgBox(
      '电脑中已安装更高版本（' + VersionToStr(InstalledVersion) + '）。' + #13#10 +
      '为避免配置和数据不兼容，本安装程序不会执行降级。请先卸载当前版本，或使用更新版本的安装程序。',
      mbError, MB_OK, IDOK);
    Result := False;
  end;
end;

procedure InitializeWizard;
begin
  DownloadPage := CreateDownloadPage(
    '正在下载安装文件',
    '请保持网络连接。安装程序会校验文件完整性，校验失败不会写入软件目录。',
    nil);
  DownloadPage.ShowBaseNameInsteadOfUrl := True;
  ExtractionPage := CreateExtractionPage(
    '正在准备安装文件',
    '正在解压经过校验的安装载荷，请稍候。',
    nil);
  ExtractionPage.ShowArchiveInsteadOfFile := True;
  InstallPage := CreateOutputMarqueeProgressPage(
    '正在安装 {#ProductName}',
    '正在校验并写入程序、运行环境和默认配置。');
end;

procedure AcquirePayload;
begin
  PayloadArchivePath := ExpandConstant('{tmp}\payload.zip');
#ifdef OnlinePayload
  DownloadPage.Clear;
  DownloadPage.Add('{#PayloadUrl}', 'payload.zip', '{#PayloadSHA256}');
  DownloadPage.Show;
  try
    DownloadPage.Download;
  finally
    DownloadPage.Hide;
  end;
#else
  ExtractTemporaryFile('payload.zip');
#endif
end;

procedure ExtractPayload;
begin
  PayloadStagingPath := ExpandConstant('{tmp}\wechatrebot-payload');
  DelTree(PayloadStagingPath, True, True, True);
  ForceDirectories(PayloadStagingPath);
  ExtractionPage.Clear;
  ExtractionPage.Add(PayloadArchivePath, PayloadStagingPath, True);
  ExtractionPage.Show;
  try
    ExtractionPage.Extract;
  finally
    ExtractionPage.Hide;
  end;
end;

function InstallVerifiedPayload: Boolean;
var
  ExitCode: Integer;
  Params: String;
  Status: AnsiString;
  StatusFile: String;
begin
  ExtractTemporaryFile('WeChatReBot-bootstrap.exe');
  StatusFile := ExpandConstant('{tmp}\native-install-status.json');
  Params := 'install-from-native-setup --payload "' + PayloadStagingPath +
    '" --install-root "' + RemoveBackslashUnlessRoot(WizardDirValue) +
    '" --status-file "' + StatusFile + '"';
  InstallPage.Show;
  InstallPage.Animate;
  ExitCode := -1;
  try
    Result := Exec(
      ExpandConstant('{tmp}\WeChatReBot-bootstrap.exe'),
      Params,
      ExpandConstant('{tmp}'),
      SW_HIDE,
      ewWaitUntilTerminated,
      ExitCode) and (ExitCode = 0);
  finally
    InstallPage.Hide;
  end;
  Log(Format('Native install launched=%d exit=%d', [Ord(Result), ExitCode]));
  if LoadStringFromFile(StatusFile, Status) then
    Log('Native install status: ' + Utf8Decode(Status));
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  try
    AcquirePayload;
    ExtractPayload;
    if not InstallVerifiedPayload then
      Result := '安装程序未能完成文件校验和部署。现有安装与用户数据未被删除，请查看安装日志后重试。';
  except
    Result := '安装准备失败：' + GetExceptionMessage;
  end;
end;

function InitializeUninstall: Boolean;
var
  ExitCode: Integer;
  Params: String;
  Executable: String;
  Root: String;
  Status: AnsiString;
  StatusFile: String;
begin
  Root := RemoveBackslashUnlessRoot(ExpandConstant('{app}'));
  StatusFile := ExpandConstant('{tmp}\native-uninstall-status.json');
  Params := 'prepare-native-uninstall --install-root "' + Root +
    '" --status-file "' + StatusFile + '"';
  Executable := Root + '\manager\WeChatReBot.exe';
  ExitCode := -1;
  Log('Native uninstall precheck: ' + Executable + ' ' + Params);
  Result := Exec(
    Executable,
    Params,
    Root,
    SW_HIDE,
    ewWaitUntilTerminated,
    ExitCode);
  Log(Format('Native uninstall precheck launched=%d exit=%d', [Ord(Result), ExitCode]));
  if LoadStringFromFile(StatusFile, Status) then
    Log('Native uninstall precheck status: ' + Utf8Decode(Status));
  Result := Result and (ExitCode = 0);
  if not Result then
    SuppressibleMsgBox('卸载前检查失败，为保护现有数据，已停止卸载。', mbError, MB_OK, IDOK);
end;
