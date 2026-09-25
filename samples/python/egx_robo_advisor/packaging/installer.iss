; Inno Setup script for EGX Robo-Advisor. Run by packaging/build.py, which
; passes AppVersion, SourceDir, OutputDir and IconFile.
;
; Installs per user (no administrator prompt) into
; %LOCALAPPDATA%\Programs\EGX Robo-Advisor. Settings, the Thndr X sign-in and
; logs live in %LOCALAPPDATA%\EGX Robo-Advisor and are kept on uninstall; API
; keys stay in Windows Credential Manager.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{7B8E0C1A-2F4D-4E5B-9C63-5A1D2E3F4B6C}
AppName=EGX Robo-Advisor
AppVersion={#AppVersion}
AppPublisher=EGX Robo-Advisor
DefaultDirName={autopf}\EGX Robo-Advisor
DefaultGroupName=EGX Robo-Advisor
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir={#OutputDir}
OutputBaseFilename=EGX-Robo-Advisor-Setup-{#AppVersion}
SetupIconFile={#IconFile}
UninstallDisplayIcon={app}\EGX Robo-Advisor.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
; Arabic is an unofficial Inno Setup translation: used when it is installed.
#if FileExists(AddBackslash(CompilerPath) + "Languages\Arabic.isl")
Name: "arabic"; MessagesFile: "compiler:Languages\Arabic.isl"
#endif

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\EGX Robo-Advisor"; Filename: "{app}\EGX Robo-Advisor.exe"
Name: "{autodesktop}\EGX Robo-Advisor"; Filename: "{app}\EGX Robo-Advisor.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\EGX Robo-Advisor.exe"; Description: "{cm:LaunchProgram,EGX Robo-Advisor}"; Flags: nowait postinstall skipifsilent
