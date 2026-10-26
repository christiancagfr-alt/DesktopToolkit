; Desktop Toolkit 閳?Windows installer (Inno Setup 6)
; Builds a double-click Setup.exe from dist\DesktopToolkit\

#define MyAppName "Desktop Toolkit"
#define MyAppVersion "1.9.16"
#define MyAppPublisher "Desktop Toolkit"
#define MyAppURL "https://github.com/christiancagfr-alt/DesktopToolkit"
#define MyAppExeName "DesktopToolkit.exe"
; Repo root relative to this .iss file
#define MySourceDir "..\dist\DesktopToolkit"

[Setup]
AppId={{A8C3E2F1-4B7D-4E9A-9C2F-DesktopToolkit}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={autopf}\DesktopToolkit
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
LicenseFile=
OutputDir=..\dist\release
OutputBaseFilename=DesktopToolkit-{#MyAppVersion}-windows-setup
SetupIconFile=..\logo.ico
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}
VersionInfoVersion={#MyAppVersion}.0
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription={#MyAppName} Installer
VersionInfoProductName={#MyAppName}
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"; Flags: unchecked
Name: "autostart"; Description: "Start with Windows"; GroupDescription: "Startup:"; Flags: unchecked
Name: "shredmenu"; Description: "Add Explorer right-click: Shred with Desktop Toolkit"; GroupDescription: "Shell integration:"; Flags: checkedonce

[Files]
; Full onedir PyInstaller tree
Source: "{#MySourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; Microsoft Visual C++ 2015-2022 x64 redistributable (provides MSVCP140.dll)
; Downloaded by CI into installer\ before ISCC; required at compile time.
Source: "vc_redist.x64.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Registry]
; Optional run at login (current user)
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
  ValueType: string; ValueName: "DesktopToolkit"; ValueData: """{app}\{#MyAppExeName}"""; \
  Flags: uninsdeletevalue; Tasks: autostart
; Explorer context menu — shred files (per-user, no admin)
Root: HKCU; Subkey: "Software\Classes\*\shell\DesktopToolkitShred"; \
  ValueType: string; ValueName: ""; ValueData: "用 Desktop Toolkit 粉碎"; \
  Flags: uninsdeletekey; Tasks: shredmenu
Root: HKCU; Subkey: "Software\Classes\*\shell\DesktopToolkitShred"; \
  ValueType: string; ValueName: "Icon"; ValueData: "{app}\{#MyAppExeName}"; \
  Tasks: shredmenu
Root: HKCU; Subkey: "Software\Classes\*\shell\DesktopToolkitShred"; \
  ValueType: string; ValueName: "Position"; ValueData: "Top"; \
  Tasks: shredmenu
Root: HKCU; Subkey: "Software\Classes\*\shell\DesktopToolkitShred\command"; \
  ValueType: string; ValueName: ""; ValueData: """{app}\{#MyAppExeName}"" --shred ""%1"""; \
  Tasks: shredmenu
Root: HKCU; Subkey: "Software\Classes\Directory\shell\DesktopToolkitShred"; \
  ValueType: string; ValueName: ""; ValueData: "用 Desktop Toolkit 粉碎"; \
  Flags: uninsdeletekey; Tasks: shredmenu
Root: HKCU; Subkey: "Software\Classes\Directory\shell\DesktopToolkitShred"; \
  ValueType: string; ValueName: "Icon"; ValueData: "{app}\{#MyAppExeName}"; \
  Tasks: shredmenu
Root: HKCU; Subkey: "Software\Classes\Directory\shell\DesktopToolkitShred"; \
  ValueType: string; ValueName: "Position"; ValueData: "Top"; \
  Tasks: shredmenu
Root: HKCU; Subkey: "Software\Classes\Directory\shell\DesktopToolkitShred\command"; \
  ValueType: string; ValueName: ""; ValueData: """{app}\{#MyAppExeName}"" --shred ""%1"""; \
  Tasks: shredmenu

[Run]
; Install VC++ runtime when MSVCP140.dll is missing (fixes "找不到 MSVCP140.dll")
; Verb:runas needs shellexec (Inno requirement); triggers UAC under PrivilegesRequired=lowest
Filename: "{tmp}\vc_redist.x64.exe"; \
  Parameters: "/install /passive /norestart"; \
  StatusMsg: "Installing Visual C++ runtime (MSVCP140.dll)..."; \
  Flags: shellexec waituntilterminated; \
  Check: NeedsVCRedist; \
  Verb: runas
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName} now"; Flags: nowait postinstall skipifsilent

[Code]
function NeedsVCRedist: Boolean;
begin
  { System32 on 64-bit install when ArchitecturesInstallIn64BitMode is set }
  Result := not FileExists(ExpandConstant('{sys}\MSVCP140.dll'));
  if Result then
    Log('MSVCP140.dll missing — will run vc_redist.x64.exe')
  else
    Log('MSVCP140.dll present — skip VC redist');
end;

