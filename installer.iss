; Market installer
#define MyAppName "Market"
#define MyAppVersion "1.0.0"
#define MyAppExeName "Market.exe"

[Setup]
AppId={{9C4D5E1F-5F8B-4C43-9F1C-6B4E2F5E3A11}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
DefaultDirName={autopf}\Market
DefaultGroupName=Market
OutputDir=installer
OutputBaseFilename=MarketSetup
Compression=lzma
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
UninstallDisplayIcon={app}\Market.exe

[Files]
Source: "dist\Market\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autodesktop}\Market"; Filename: "{app}\Market.exe"; WorkingDir: "{app}"
Name: "{group}\Market"; Filename: "{app}\Market.exe"; WorkingDir: "{app}"
Name: "{group}\Market'i Kaldır"; Filename: "{uninstallexe}"

[Dirs]
Name: "{app}\market_data"
Name: "{app}\market_data\photos"

[Run]
Filename: "{app}\Market.exe"; Description: "Market'i şimdi çalıştır"; Flags: nowait postinstall skipifsilent
