#ifndef SourceCommit
  #error SourceCommit must be supplied by build-installer.ps1
#endif
#ifndef StationdPath
  #error StationdPath must be supplied by build-installer.ps1
#endif
#ifndef ChannelWorkerPath
  #error ChannelWorkerPath must be supplied by build-installer.ps1
#endif
#ifndef StationUpdaterPath
  #error StationUpdaterPath must be supplied by build-installer.ps1
#endif
#ifndef StationdSha256
  #error StationdSha256 must be supplied by build-installer.ps1
#endif
#ifndef ChannelWorkerSha256
  #error ChannelWorkerSha256 must be supplied by build-installer.ps1
#endif
#ifndef StationUpdaterSha256
  #error StationUpdaterSha256 must be supplied by build-installer.ps1
#endif
#ifndef StationdSize
  #error StationdSize must be supplied by build-installer.ps1
#endif
#ifndef ChannelWorkerSize
  #error ChannelWorkerSize must be supplied by build-installer.ps1
#endif
#ifndef StationUpdaterSize
  #error StationUpdaterSize must be supplied by build-installer.ps1
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
#define UpdaterServiceName "TownLightStationUpdater"
#define GStreamerFile "gstreamer-1.0-msvc-x86_64-1.28.6.exe"
#define GStreamerSha256 "059251444d1267b486eba390b18d25fed87e10315e72f757ec6c7e912fa746b5"

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
Source: "{#StationUpdaterPath}"; DestName: "station-updater.exe"; Flags: dontcopy noencryption
Source: "..\THIRD-PARTY-NOTICES.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion

[UninstallDelete]
Type: files; Name: "{app}\stationd.exe"
Type: files; Name: "{app}\channel-worker.exe"
Type: files; Name: "{app}\station-updater.exe"
Type: filesandordirs; Name: "{app}\runtime"
Type: dirifempty; Name: "{app}"

[Code]
const
  StationServiceName = '{#ServiceName}';
  UpdaterServiceName = '{#UpdaterServiceName}';
  RuntimeSha256 = '{#GStreamerSha256}';
  CandidateCommit = '{#SourceCommit}';
  CandidateVersion = '{#ProductVersion}';
  StationdSha256 = '{#StationdSha256}';
  ChannelWorkerSha256 = '{#ChannelWorkerSha256}';
  StationUpdaterSha256 = '{#StationUpdaterSha256}';
  StationdSize = '{#StationdSize}';
  ChannelWorkerSize = '{#ChannelWorkerSize}';
  StationUpdaterSize = '{#StationUpdaterSize}';

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

function ServiceExists(const Name: String): Boolean;
var
  ResultCode: Integer;
begin
  Result := RunSc('query ' + Name, ResultCode) and
    (ResultCode = 0);
end;

procedure StopAndDeleteServiceIfPresent(const Name: String);
var
  Attempt: Integer;
  ResultCode: Integer;
begin
  if not ServiceExists(Name) then
    Exit;

  RunSc('stop ' + Name, ResultCode);
  RunSc('delete ' + Name, ResultCode);
  for Attempt := 1 to 300 do
  begin
    if not ServiceExists(Name) then
      Exit;
    Sleep(250);
  end;
  RaiseException('The existing TownLight Station service did not stop within 15 seconds.');
end;

procedure RollbackService;
begin
  StopAndDeleteServiceIfPresent(UpdaterServiceName);
  StopAndDeleteServiceIfPresent(StationServiceName);
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
  DeleteFile(ExpandConstant(
    '{commonappdata}\TownLight Station\active-slot.json'));
end;

function PrivateRuntimeIsComplete(const RuntimeRoot: String): Boolean;
begin
  Result :=
    FileExists(RuntimeRoot + '\bin\gstreamer-1.0-0.dll') and
    FileExists(RuntimeRoot + '\lib\gstreamer-1.0\gstopenh264.dll') and
    FileExists(RuntimeRoot + '\lib\gstreamer-1.0\gstvoaacenc.dll') and
    FileExists(RuntimeRoot + '\lib\gstreamer-1.0\gstaudiorate.dll') and
    FileExists(RuntimeRoot + '\lib\gstreamer-1.0\gstvideorate.dll') and
    FileExists(RuntimeRoot + '\lib\gstreamer-1.0\gstrsaudiofx.dll') and
    FileExists(RuntimeRoot + '\lib\gstreamer-1.0\gstplayback.dll') and
    FileExists(RuntimeRoot + '\lib\gstreamer-1.0\gstmpegtsdemux.dll') and
    FileExists(RuntimeRoot + '\lib\gstreamer-1.0\gstlibav.dll') and
    FileExists(RuntimeRoot + '\lib\gstreamer-1.0\gstsrt.dll') and
    FileExists(RuntimeRoot + '\lib\gstreamer-1.0\gstgdkpixbuf.dll') and
    FileExists(RuntimeRoot + '\lib\gstreamer-1.0\gstpng.dll') and
    FileExists(RuntimeRoot + '\lib\gstreamer-1.0\gstjpeg.dll') and
    FileExists(RuntimeRoot + '\bin\srt.dll');
end;

procedure InstallPrivateRuntime;
var
  ResultCode: Integer;
  RuntimeInstaller: String;
  RuntimeRoot: String;
  Parameters: String;
begin
  RuntimeRoot := ExpandConstant('{app}\runtime\gstreamer');
  if PrivateRuntimeIsComplete(RuntimeRoot) then
  begin
    Log('Reusing the complete private media runtime already installed at ' +
      RuntimeRoot);
    RuntimeWasInstalled := False;
    Exit;
  end;

  if FileExists(RuntimeRoot + '\unins000.exe') then
    RemovePrivateRuntime;

  ExtractTemporaryFile('{#GStreamerFile}');
  RuntimeInstaller := ExpandConstant('{tmp}\{#GStreamerFile}');
  Parameters := '/VERYSILENT /ALLUSERS /NORESTART /TYPE=runtime /DIR="' +
    RuntimeRoot + '"';
  if (not Exec(RuntimeInstaller, Parameters, '', SW_HIDE,
    ewWaitUntilTerminated, ResultCode)) or (ResultCode <> 0) then
    RaiseException('The bundled media runtime could not be installed (exit ' +
      IntToStr(ResultCode) + ').');
  RuntimeWasInstalled := True;

  if not PrivateRuntimeIsComplete(RuntimeRoot) then
    RaiseException('The bundled media runtime did not produce every required library and plugin.');
end;

procedure StageStationBinaries;
var
  AppDirectory: String;
begin
  ExtractTemporaryFile('stationd.exe');
  ExtractTemporaryFile('channel-worker.exe');
  ExtractTemporaryFile('station-updater.exe');
  AppDirectory := ExpandConstant('{app}');
  if not ForceDirectories(AppDirectory) then
    RaiseException('The TownLight Station application directory could not be created.');
  if not CopyFile(ExpandConstant('{tmp}\stationd.exe'),
    AppDirectory + '\stationd.exe', False) then
    RaiseException('The station service binary could not be staged.');
  if not CopyFile(ExpandConstant('{tmp}\channel-worker.exe'),
    AppDirectory + '\channel-worker.exe', False) then
    RaiseException('The channel worker binary could not be staged.');
  if not CopyFile(ExpandConstant('{tmp}\station-updater.exe'),
    AppDirectory + '\station-updater.exe', False) then
    RaiseException('The maintenance updater binary could not be staged.');
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
  StopAndDeleteServiceIfPresent(UpdaterServiceName);
  StopAndDeleteServiceIfPresent(StationServiceName);

  BinaryPath := '\"' + ExpandConstant('{app}\stationd.exe') +
    '\" service --database \"' +
    ExpandConstant('{commonappdata}\TownLight Station\station.db') +
    '\" --address 127.0.0.1:4070';
  Arguments := 'create TownLightStation binPath= "' + BinaryPath +
    '" start= auto obj= LocalSystem DisplayName= "TownLight Station"';
  if (not RunSc(Arguments, ResultCode)) or (ResultCode <> 0) then
    RaiseException('The TownLight Station service could not be registered (exit ' +
      IntToStr(ResultCode) + ').');
  ConfigureServiceEnvironment;
  if (not RunSc('config TownLightStation start= auto', ResultCode)) or
    (ResultCode <> 0) then
    RaiseException('Immediate automatic service start could not be configured.');
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

procedure CreateAndStartUpdaterService;
var
  ResultCode: Integer;
  BinaryPath: String;
  Arguments: String;
begin
  BinaryPath := '\"' + ExpandConstant('{app}\station-updater.exe') +
    '\" service --database \"' +
    ExpandConstant('{commonappdata}\TownLight Station\station.db') +
    '\" --application-root \"' + ExpandConstant('{app}') + '\"';
  Arguments := 'create ' + UpdaterServiceName + ' binPath= "' + BinaryPath +
    '" start= auto obj= LocalSystem DisplayName= "TownLight Station Maintenance"';
  if (not RunSc(Arguments, ResultCode)) or (ResultCode <> 0) then
    RaiseException('The maintenance updater service could not be registered (exit ' +
      IntToStr(ResultCode) + ').');
  if (not RunSc('config ' + UpdaterServiceName + ' start= auto', ResultCode)) or
    (ResultCode <> 0) then
    RaiseException('Maintenance updater automatic start could not be configured.');
  if (not RunSc('description ' + UpdaterServiceName +
    ' "TownLight Station transactional update and rollback watchdog"', ResultCode)) or
    (ResultCode <> 0) then
    RaiseException('The maintenance updater description could not be configured.');
  if (not RunSc('failure ' + UpdaterServiceName +
    ' reset= 86400 actions= restart/5000/restart/15000/restart/60000', ResultCode)) or
    (ResultCode <> 0) then
    RaiseException('The maintenance updater recovery policy could not be configured.');
  if (not RunSc('failureflag ' + UpdaterServiceName + ' 1', ResultCode)) or
    (ResultCode <> 0) then
    RaiseException('The maintenance updater non-crash recovery policy could not be configured.');
  if (not RunSc('start ' + UpdaterServiceName, ResultCode)) or
    (ResultCode <> 0) then
    RaiseException('The maintenance updater service could not be started (exit ' +
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

procedure WriteActiveSlotReceipt;
var
  Receipt: String;
  ReceiptPath: String;
begin
  ReceiptPath := ExpandConstant(
    '{commonappdata}\TownLight Station\active-slot.json');
  Receipt := '{' +
    '"schema_version":1,' +
    '"generation":1,' +
    '"activated_at_unix_ms":0,' +
    '"transaction_id":"installer-bootstrap",' +
    '"active":{' +
      '"relative_directory":".",' +
      '"package_manifest":{' +
        '"schema_version":2,' +
        '"product":"TownLight Station",' +
        '"version":"' + CandidateVersion + '",' +
        '"source_commit":"' + CandidateCommit + '",' +
        '"files":[' +
          '{"path":"stationd.exe","size_bytes":' + StationdSize +
            ',"sha256":"' + StationdSha256 + '"},' +
          '{"path":"channel-worker.exe","size_bytes":' + ChannelWorkerSize +
            ',"sha256":"' + ChannelWorkerSha256 + '"}' +
        ']' +
      '}' +
    '},' +
    '"previous":null' +
    '}';
  if not SaveStringToFile(ReceiptPath, Receipt, False) then
    RaiseException('The active application receipt could not be written.');
end;

procedure RemoveStagedBinaries;
begin
  DeleteFile(ExpandConstant('{app}\station-updater.exe'));
  DeleteFile(ExpandConstant('{app}\channel-worker.exe'));
  DeleteFile(ExpandConstant('{app}\stationd.exe'));
  RemoveDir(ExpandConstant('{app}'));
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then
  begin
    try
      StopAndDeleteServiceIfPresent(UpdaterServiceName);
      StopAndDeleteServiceIfPresent(StationServiceName);
      StageStationBinaries;
      InstallPrivateRuntime;
      CreateAndStartService;
      if not WaitForHealthyStation then
        RaiseException('TownLight Station did not become healthy within 75 seconds.');
      WriteInstallReceipt;
      WriteActiveSlotReceipt;
      CreateAndStartUpdaterService;
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
