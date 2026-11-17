; Per-user Windows installer for the desktop client, compiled by package-studio.py with Inno Setup 6.7.3.
; Client caches and credentials live in %LOCALAPPDATA%\TigerBrowser, outside {app}, and survive updates and removal.

[Setup]
AppId={{237BBC49-C145-428B-853B-F210948C2A7E}
AppName=TigerBrowser
AppVersion={#AppVersion}
AppPublisher=TigerBrowser
AppPublisherURL=https://github.com/zhaotianshi/TigerBrowser
DefaultDirName={localappdata}\Programs\TigerBrowser
DefaultGroupName=TigerBrowser
DisableProgramGroupPage=yes
DisableDirPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#OutputDir}
OutputBaseFilename={#OutputName}
SetupIconFile={#IconFile}
UninstallDisplayIcon={app}\TigerBrowser.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; An update closes a client that is still exiting instead of failing on locked files.
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[InstallDelete]
; Each version replaces the previous program files completely.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userprograms}\TigerBrowser"; Filename: "{app}\TigerBrowser.exe"
Name: "{userdesktop}\TigerBrowser"; Filename: "{app}\TigerBrowser.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; Flags: unchecked

[Run]
Filename: "{app}\TigerBrowser.exe"; Description: "{cm:LaunchProgram,TigerBrowser}"; Flags: postinstall nowait skipifsilent
; A silent run is the client's own update; reopen it once the new files are in place.
Filename: "{app}\TigerBrowser.exe"; Flags: nowait skipifnotsilent
