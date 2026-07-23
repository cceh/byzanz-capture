; Inno Setup script for the Byzanz Capture Windows installer.
; Compiled by the `installer` phase of scripts/vm-win-setup.sh (which is
; also what CI runs) — it passes the version via -DAppVersion and expects
; the PyInstaller onedir bundle in dist/byzanz-capture (build phase).
;
; Per-user install (PrivilegesRequired=lowest): lands in
; %LOCALAPPDATA%\Programs\Byzanz Capture, no admin rights needed —
; right for lab machines where users are not administrators.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
; Fixed AppId so a newer installer upgrades an existing install in place
; instead of creating a second entry.
AppId={{8C2E4A1D-6F3B-4E0A-9C7D-B15A2E9D4F60}
AppName=Byzanz Capture
AppVersion={#AppVersion}
AppPublisher=Cologne Center for eHumanities (CCeH)
DefaultDirName={autopf}\Byzanz Capture
DefaultGroupName=Byzanz Capture
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist\installer
OutputBaseFilename=byzanz-capture-setup-{#AppVersion}
SetupIconFile=..\ui\icon\app_icon.ico
UninstallDisplayIcon={app}\byzanz-capture.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Languages]
Name: "german"; MessagesFile: "compiler:Languages\German.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\byzanz-capture\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\Byzanz Capture"; Filename: "{app}\byzanz-capture.exe"
Name: "{autodesktop}\Byzanz Capture"; Filename: "{app}\byzanz-capture.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\byzanz-capture.exe"; Description: "{cm:LaunchProgram,Byzanz Capture}"; Flags: nowait postinstall skipifsilent
