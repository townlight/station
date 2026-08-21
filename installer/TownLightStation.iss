#ifndef SourceCommit
  #error SourceCommit must be supplied by build-installer.ps1
#endif
#ifndef StationdPath
  #error StationdPath must be supplied by build-installer.ps1
#endif
#ifndef ChannelWorkerPath
  #error ChannelWorkerPath must be supplied by build-installer.ps1
#endif
#ifndef GStreamerInstaller
  #error GStreamerInstaller must be supplied by build-installer.ps1
#endif
#ifndef OutputDirectory
  #error OutputDirectory must be supplied by build-installer.ps1
#endif

#define ProductName "TownLight Station"
#define ProductVersion "0.1.0"
#define ServiceName "TownLightStation"
#define GStreamerFile "gstreamer-1.0-msvc-x86_64-1.28.6.exe"
#define GStreamerSha256 "059251444D1267B486EBA390B18D25FED87E10315E72F757EC6C7E912FA746B5"

[Setup]
AppId={{D70BC1A2-A047-4F3A-87D9-811385248D2C}
AppName={#ProductName}
AppVersion={#ProductVersion}
AppPublisher=TownLight
DefaultDirName={autopf}\TownLight Station
DefaultGroupName=TownLight Station
DisableProgramGroupPage=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.20348
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=
OutputDir={#OutputDirectory}
OutputBaseFilename=TownLight-Station-{#ProductVersion}-x64-setup
Compression=lzma2/fast
SolidCompression=yes
SetupLogging=yes
CloseApplications=yes
RestartApplications=no
UninstallDisplayName=TownLight Station
UninstallDisplayIcon={app}\stationd.exe
VersionInfoVersion=0.1.0.0
VersionInfoCompany=TownLight
VersionInfoDescription=TownLight Station installer
VersionInfoProductName=TownLight Station
VersionInfoProductVersion={#ProductVersion}
WizardStyle=modern

[Dirs]
Name: "{commonappdata}\TownLight Station"; Permissions: system-full admins-full

[Files]
Source: "{#GStreamerInstaller}"; DestName: "{#GStreamerFile}"; Flags: dontcopy noencryption
Source: "{#StationdPath}"; DestName: "stationd.exe"; Flags: dontcopy noencryption
Source: "{#ChannelWorkerPath}"; DestName: "channel-worker.exe"; Flags: dontcopy noencryption
Source: "..\THIRD-PARTY-NOTICES.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion

[UninstallDelete]
Type: files; Name: "{app}\stationd.exe"
Type: files; Name: "{app}\channel-worker.exe"
Type: filesandordirs; Name: "{app}\runtime"
Type: dirifempty; Name: "{app}"

[Code]
const
  StationServiceName = '{#ServiceName}';
  RuntimeSha256 = '{#GStreamerSha256}';
  CandidateCommit = '{#SourceCommit}';
  CandidateVersion = '{#ProductVersion}';

var
  RuntimeWasInstalled: Boolean;
  CandidateActivated: Boolean;
  SetupCompleted: Boolean;

function RunSc(const Arguments: String; var ResultCode: Integer): Boolean;
begin
  Log('sc.exe ' + Arguments);
  Result := Exec(ExpandConstant('{sys}\sc.exe'), Arguments, '', SW_HIDE,
    ewWaitUntilTerminated, ResultCode);
end;

function ServiceExists: Boolean;
var
  ResultCode: Integer;
begin
  Result := RunSc('query ' + StationServiceName, ResultCode) and
    (ResultCode = 0);
end;

procedure StopAndDeleteServiceIfPresent;
var
  Attempt: Integer;
  ResultCode: Integer;
begin
  if not ServiceExists then
    Exit;

  RunSc('stop ' + StationServiceName, ResultCode);
  RunSc('delete TownLightStation', ResultCode);
  for Attempt := 1 to 60 do
  begin
    if not ServiceExists then
      Exit;
    Sleep(250);
  end;
  RaiseException('The existing TownLight Station service did not stop within 15 seconds.');
end;

procedure RollbackService;
begin
  StopAndDeleteServiceIfPresent;
  CandidateActivated := False;
end;

procedure RemovePrivateRuntime;
var
  ResultCode: Integer;
  Uninstaller: String;
begin
  Uninstaller := ExpandConstant('{app}\runtime\gstreamer\unins000.exe');
  if FileExists(Uninstaller) then
  begin
    if (not Exec(Uninstaller, '/VERYSILENT /NORESTART', '', SW_HIDE,
      ewWaitUntilTerminated, ResultCode)) or (ResultCode <> 0) then
      Log('Private runtime removal returned ' + IntToStr(ResultCode));
  end;
  RuntimeWasInstalled := False;
end;

procedure RollbackInstallation;
begin
  RollbackService;
  if RuntimeWasInstalled then
    RemovePrivateRuntime;
  DeleteFile(ExpandConstant(
    '{commonappdata}\TownLight Station\install-receipt.json'));
end;

procedure InstallPrivateRuntime;
var
  ResultCode: Integer;
  RuntimeInstaller: String;
  RuntimeRoot: String;
  Parameters: String;
begin
  ExtractTemporaryFile('{#GStreamerFile}');
  RuntimeInstaller := ExpandConstant('{tmp}\{#GStreamerFile}');
  RuntimeRoot := ExpandConstant('{app}\runtime\gstreamer');
  Parameters := '/VERYSILENT /ALLUSERS /NORESTART /TYPE=runtime /DIR="' +
    RuntimeRoot + '"';
  if (not Exec(RuntimeInstaller, Parameters, '', SW_HIDE,
    ewWaitUntilTerminated, ResultCode)) or (ResultCode <> 0) then
    RaiseException('The bundled media runtime could not be installed (exit ' +
      IntToStr(ResultCode) + ').');
  RuntimeWasInstalled := True;

  if not FileExists(RuntimeRoot + '\bin\gstreamer-1.0-0.dll') then
    RaiseException('The bundled media runtime did not produce its required core library.');
  if not FileExists(RuntimeRoot + '\lib\gstreamer-1.0\gstopenh264.dll') then
    RaiseException('The bundled media runtime did not produce its required H.264 plugin.');
  if not FileExists(RuntimeRoot + '\lib\gstreamer-1.0\gstvoaacenc.dll') then
    RaiseException('The bundled media runtime did not produce its required AAC encoder.');
  if not FileExists(RuntimeRoot + '\lib\gstreamer-1.0\gstaudiorate.dll') then
    RaiseException('The bundled media runtime did not produce its required audio rate adjuster.');
  if not FileExists(RuntimeRoot + '\lib\gstreamer-1.0\gstvideorate.dll') then
    RaiseException('The bundled media runtime did not produce its required video rate adjuster.');
  if not FileExists(RuntimeRoot + '\lib\gstreamer-1.0\gstplayback.dll') then
    RaiseException('The bundled media runtime did not produce its required file playback plugin.');
  if not FileExists(RuntimeRoot + '\lib\gstreamer-1.0\gstmpegtsdemux.dll') then
    RaiseException('The bundled media runtime did not produce its required transport stream demuxer.');
  if not FileExists(RuntimeRoot + '\lib\gstreamer-1.0\gstlibav.dll') then
    RaiseException('The bundled media runtime did not produce its required software decoder bundle.');
end;

procedure StageStationBinaries;
var
  AppDirectory: String;
begin
  ExtractTemporaryFile('stationd.exe');
  ExtractTemporaryFile('channel-worker.exe');
  AppDirectory := ExpandConstant('{app}');
  if not ForceDirectories(AppDirectory) then
    RaiseException('The TownLight Station application directory could not be created.');
  if not CopyFile(ExpandConstant('{tmp}\stationd.exe'),
    AppDirectory + '\stationd.exe', False) then
    RaiseException('The station service binary could not be staged.');
  if not CopyFile(ExpandConstant('{tmp}\channel-worker.exe'),
    AppDirectory + '\channel-worker.exe', False) then
    RaiseException('The channel worker binary could not be staged.');
  if not ForceDirectories(ExpandConstant('{commonappdata}\TownLight Station')) then
    RaiseException('The station data directory could not be created.');
end;

procedure ConfigureServiceEnvironment;
var
  Environment: String;
  Key: String;
begin
  Key := 'SYSTEM\CurrentControlSet\Services\' + StationServiceName;
  Environment :=
    'PATH=' + ExpandConstant('{app}\runtime\gstreamer\bin') + #0 +
    'GST_PLUGIN_PATH_1_0=' +
      ExpandConstant('{app}\runtime\gstreamer\lib\gstreamer-1.0') + #0 +
    'GST_PLUGIN_SYSTEM_PATH_1_0=' +
      ExpandConstant('{app}\runtime\gstreamer\lib\gstreamer-1.0');
  if not RegWriteMultiStringValue(HKLM64, Key, 'Environment', Environment) then
    RaiseException('The station service runtime environment could not be configured.');
end;

procedure CreateAndStartService;
var
  ResultCode: Integer;
  BinaryPath: String;
  Arguments: String;
begin
  StopAndDeleteServiceIfPresent;

  BinaryPath := '\"' + ExpandConstant('{app}\stationd.exe') +
    '\" service --database \"' +
    ExpandConstant('{commonappdata}\TownLight Station\station.db') +
    '\" --address 127.0.0.1:4070';
  Arguments := 'create TownLightStation binPath= "' + BinaryPath +
    '" start= auto DisplayName= "TownLight Station"';
  if (not RunSc(Arguments, ResultCode)) or (ResultCode <> 0) then
    RaiseException('The TownLight Station service could not be registered (exit ' +
      IntToStr(ResultCode) + ').');
  ConfigureServiceEnvironment;
  if (not RunSc('config TownLightStation start= delayed-auto', ResultCode)) or
    (ResultCode <> 0) then
    RaiseException('Delayed automatic service start could not be configured.');
  if (not RunSc('description TownLightStation "TownLight Station local broadcast appliance"', ResultCode)) or
    (ResultCode <> 0) then
    RaiseException('The station service description could not be configured.');
  if (not RunSc('failure TownLightStation reset= 86400 actions= restart/5000/restart/15000/restart/60000', ResultCode)) or
    (ResultCode <> 0) then
    RaiseException('The station service recovery policy could not be configured.');
  if (not RunSc('failureflag TownLightStation 1', ResultCode)) or
    (ResultCode <> 0) then
    RaiseException('The station service non-crash recovery policy could not be configured.');

  if (not RunSc('start TownLightStation', ResultCode)) or
    (ResultCode <> 0) then
    RaiseException('The TownLight Station service could not be started (exit ' +
      IntToStr(ResultCode) + ').');
end;

function WaitForHealthyStation: Boolean;
var
  Attempt: Integer;
  Http: Variant;
  Response: String;
begin
  Result := False;
  for Attempt := 1 to 60 do
  begin
    try
      Http := CreateOleObject('WinHttp.WinHttpRequest.5.1');
      Http.SetTimeouts(1000, 1000, 1000, 1000);
      Http.Open('GET', 'http://127.0.0.1:4070/health', False);
      Http.Send('');
      Response := Http.ResponseText;
      if (Http.Status = 200) and
        (Pos('"status":"ready"', Response) > 0) then
      begin
        Result := True;
        Exit;
      end;
    except
      Log('Health attempt ' + IntToStr(Attempt) + ' did not connect.');
    end;
    Sleep(250);
  end;
end;

procedure WriteInstallReceipt;
var
  Receipt: String;
  ReceiptPath: String;
begin
  ReceiptPath := ExpandConstant(
    '{commonappdata}\TownLight Station\install-receipt.json');
  Receipt := '{' +
    '"product":"TownLight Station",' +
    '"version":"' + CandidateVersion + '",' +
    '"source_commit":"' + CandidateCommit + '",' +
    '"media_runtime_sha256":"' + RuntimeSha256 + '",' +
    '"installed_local_time":"' +
      GetDateTimeString('yyyy-mm-dd hh:nn:ss', '-', ':') + '",' +
    '"health_uri":"http://127.0.0.1:4070/health"' +
    '}';
  if not SaveStringToFile(ReceiptPath, Receipt, False) then
    RaiseException('The immutable installation receipt could not be written.');
end;

procedure RemoveStagedBinaries;
begin
  DeleteFile(ExpandConstant('{app}\channel-worker.exe'));
  DeleteFile(ExpandConstant('{app}\stationd.exe'));
  RemoveDir(ExpandConstant('{app}'));
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then
  begin
    try
      StopAndDeleteServiceIfPresent;
      StageStationBinaries;
      InstallPrivateRuntime;
      CreateAndStartService;
      if not WaitForHealthyStation then
        RaiseException('TownLight Station did not become healthy within 15 seconds.');
      WriteInstallReceipt;
      CandidateActivated := True;
    except
      RollbackInstallation;
      RemoveStagedBinaries;
      RaiseException(GetExceptionMessage);
    end;
  end;
  if CurStep = ssDone then
    SetupCompleted := True;
end;

procedure DeinitializeSetup;
begin
  if CandidateActivated and (not SetupCompleted) then
  begin
    RollbackInstallation;
    RemoveStagedBinaries;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
  begin
    RollbackService;
    RemovePrivateRuntime;
    { Station data is deliberately preserved across uninstall. }
  end;
end;
