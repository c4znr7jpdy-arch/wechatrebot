// Display only: the manager owns validation, handoff, installation and recovery.
function SetTimer(hWnd: HWND; nIDEvent: UINT_PTR; uElapse: UINT; lpTimerFunc: NativeInt): UINT_PTR;
external 'SetTimer@user32.dll stdcall';
function KillTimer(hWnd: HWND; nIDEvent: UINT_PTR): Boolean;
external 'KillTimer@user32.dll stdcall';
function GetTickCount64: Int64;
external 'GetTickCount64@kernel32.dll stdcall';
function OpenProcess(Access: DWORD; Inherit: Boolean; ProcessId: DWORD): THandle;
external 'OpenProcess@kernel32.dll stdcall';
function WaitForSingleObject(Handle: THandle; Milliseconds: DWORD): DWORD;
external 'WaitForSingleObject@kernel32.dll stdcall';
function CloseHandle(Handle: THandle): Boolean;
external 'CloseHandle@kernel32.dll stdcall';

var
  UpdatePage: TWizardPage;
  UpdateStatus, UpdateDetails, UpdateElapsed: TNewStaticText;
  UpdateGauge: TNewProgressBar;
  UpdateTimer: UINT_PTR;
  OwnerHandle: THandle;
  StatusPath, SessionId, PreviousPhase: String;
  StartedAt, PhaseStartedAt: Int64;
  Ready, Finished: Boolean;

procedure UpdateFailed(const MessageText: String);
begin
  Finished := True;
  WizardForm.PageNameLabel.Caption := '更新未完成';
  WizardForm.PageDescriptionLabel.Caption := '本次更新未完成，请查看下方处理结果。';
  UpdateStatus.Caption := MessageText;
  UpdateDetails.Caption := '请按提示处理。关闭此窗口不会再次安装。';
  UpdateGauge.Style := npbstNormal;
  UpdateGauge.State := npbsError;
  WizardForm.CancelButton.Caption := '关闭';
  WizardForm.CancelButton.Enabled := True;
end;

procedure PollUpdate(Arg1: HWND; Arg2: UINT; Arg3: UINT_PTR; Arg4: DWORD);
var
  Content: AnsiString;
  Lines: TStringList;
  Percent: Integer;
  NowTicks: Int64;
begin
  if Finished then Exit;
  if WizardForm.CurPageID <> UpdatePage.ID then
  begin
    if WaitForSingleObject(OwnerHandle, 0) = 0 then
    begin
      Finished := True;
      WizardForm.CancelButton.Enabled := True;
      WizardForm.CancelButton.OnClick(WizardForm.CancelButton);
    end
    else if WizardForm.CurPageID = wpWelcome then
      WizardForm.NextButton.OnClick(WizardForm.NextButton);
    Exit;
  end;
  if not Ready then
  begin
    if not WizardForm.Visible then Exit;
    Ready := SaveStringToFile(StatusPath + '.ready', SessionId, False);
    if not Ready then
    begin
      UpdateFailed('无法连接更新任务，请关闭窗口后重试。');
      Exit;
    end;
  end;
  NowTicks := GetTickCount64;
  if LoadStringFromFile(StatusPath, Content) then
  begin
    Lines := TStringList.Create;
    try
      Lines.Text := Utf8Decode(Content);
      if (Lines.Count >= 5) and (Lines[0] = SessionId) then
      begin
        if (Lines[1] = 'success') or (Lines[1] = 'cancelled') then
        begin
          Finished := True;
          WizardForm.CancelButton.Enabled := True;
          WizardForm.CancelButton.OnClick(WizardForm.CancelButton);
          Exit;
        end;
        if Lines[1] = 'failed' then
        begin
          UpdateFailed(Lines[3]);
          Exit;
        end;
        if Lines[1] = 'running' then
        begin
          if PreviousPhase <> Lines[3] then
          begin
            PreviousPhase := Lines[3];
            PhaseStartedAt := NowTicks;
          end;
          UpdateStatus.Caption := Lines[3];
          UpdateDetails.Caption := Lines[4];
          Percent := StrToIntDef(Lines[2], -1);
          if (Percent >= 0) and (Percent <= 100) then
          begin
            UpdateGauge.Style := npbstNormal;
            UpdateGauge.Position := Percent;
          end
          else
            UpdateGauge.Style := npbstMarquee;
        end;
      end;
    finally
      Lines.Free;
    end;
  end;
  UpdateElapsed.Caption := Format('本阶段已用 %d 秒 · 总计 %d 秒', [(NowTicks - PhaseStartedAt) div 1000,
    (NowTicks - StartedAt) div 1000]);
  if WaitForSingleObject(OwnerHandle, 0) = 0 then
    UpdateFailed('更新任务已退出，尚未确认完成。请重新打开客户端查看恢复结果。');
end;

function InitializeSetup: Boolean;
var
  OwnerPid: Integer;
begin
  StatusPath := ExpandConstant('{param:STATUS|}');
  SessionId := ExpandConstant('{param:SESSION|}');
  OwnerPid := StrToIntDef(ExpandConstant('{param:OWNERPID|0}'), 0);
  Result := (StatusPath <> '') and (Length(SessionId) = 32) and (OwnerPid > 0);
  if Result then
  begin
    OwnerHandle := OpenProcess($00100000, False, OwnerPid);
    Result := OwnerHandle <> 0;
  end;
end;

procedure InitializeWizard;
begin
  WizardForm.Caption := '微信机器人 · 自动更新';
  UpdatePage := CreateCustomPage(wpWelcome, '正在自动更新',
    '更新完成后将自动打开新版客户端，请保持此窗口打开。');
  UpdateStatus := TNewStaticText.Create(WizardForm);
  UpdateStatus.Parent := UpdatePage.Surface;
  UpdateStatus.SetBounds(0, ScaleY(12), UpdatePage.SurfaceWidth, ScaleY(65));
  UpdateStatus.AutoSize := False;
  UpdateStatus.WordWrap := True;
  UpdateStatus.Caption := '正在连接更新任务…';
  UpdateGauge := TNewProgressBar.Create(WizardForm);
  UpdateGauge.Parent := UpdatePage.Surface;
  UpdateGauge.SetBounds(0, ScaleY(85), UpdatePage.SurfaceWidth, ScaleY(20));
  UpdateGauge.Max := 100;
  UpdateGauge.Style := npbstMarquee;
  UpdateDetails := TNewStaticText.Create(WizardForm);
  UpdateDetails.Parent := UpdatePage.Surface;
  UpdateDetails.SetBounds(0, ScaleY(120), UpdatePage.SurfaceWidth, ScaleY(50));
  UpdateDetails.AutoSize := False;
  UpdateDetails.WordWrap := True;
  UpdateElapsed := TNewStaticText.Create(WizardForm);
  UpdateElapsed.Parent := UpdatePage.Surface;
  UpdateElapsed.SetBounds(0, ScaleY(180), UpdatePage.SurfaceWidth, ScaleY(24));
  StartedAt := GetTickCount64;
  PhaseStartedAt := StartedAt;
  UpdateTimer := SetTimer(0, 0, 200, CreateCallback(@PollUpdate));
  if UpdateTimer = 0 then RaiseException('无法显示更新进度，请重试。');
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  WizardForm.NextButton.Visible := False;
  WizardForm.BackButton.Visible := False;
  WizardForm.CancelButton.Enabled := Finished;
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := PageID <> UpdatePage.ID;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := CurPageID <> UpdatePage.ID;
end;

procedure CancelButtonClick(CurPageID: Integer; var Cancel, Confirm: Boolean);
begin
  Cancel := Finished;
  Confirm := False;
end;

procedure DeinitializeSetup;
begin
  if UpdateTimer <> 0 then KillTimer(0, UpdateTimer);
  if OwnerHandle <> 0 then CloseHandle(OwnerHandle);
end;
