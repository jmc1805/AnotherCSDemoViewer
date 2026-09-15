; installer/cs2viewer.iss - Windows installer for the desktop build.
;
; Built by tools/build_desktop.py, which passes /DAppVersion, /DSourceDir
; (dist\CS2Viewer) and /DWebView2Bootstrapper. Per-user install: no admin
; prompt, and the app never writes to its own folder (its data lives in
; %LOCALAPPDATA%\CS2Viewer - see paths.py), so uninstalling leaves the user's
; matches, settings and extracted icons alone.
;
; WebView2 is what draws the window. Windows 10/11 almost always has it; when
; it doesn't, Microsoft's bootstrapper installs it (it downloads the runtime).
; If that fails anyway, desktop.py falls back to the default browser.

#ifndef AppVersion
  #define AppVersion "dev"
#endif

[Setup]
AppId={{6B1F3C52-8E0D-4B8A-9C43-2D7E5A1F9B60}
AppName=CS2 Demo Viewer
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\CS2 Demo Viewer
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputBaseFilename=CS2Viewer-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\CS2Viewer.exe

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#WebView2Bootstrapper}"; DestDir: "{tmp}"; Flags: deleteafterinstall; Check: NeedsWebView2

[Icons]
Name: "{userprograms}\CS2 Demo Viewer"; Filename: "{app}\CS2Viewer.exe"
Name: "{userdesktop}\CS2 Demo Viewer"; Filename: "{app}\CS2Viewer.exe"; Tasks: desktopicon

[Run]
Filename: "{tmp}\MicrosoftEdgeWebview2Setup.exe"; Parameters: "/silent /install"; \
    StatusMsg: "Installing the Microsoft Edge WebView2 Runtime..."; \
    Check: NeedsWebView2; Flags: waituntilterminated
Filename: "{app}\CS2Viewer.exe"; Description: "Launch CS2 Demo Viewer"; \
    Flags: nowait postinstall skipifsilent

[Code]
const
  WebView2Key = 'Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';

function HasWebView2At(RootKey: Integer; SubKey: String): Boolean;
var
  V: String;
begin
  Result := RegQueryStringValue(RootKey, SubKey, 'pv', V) and (V <> '') and (V <> '0.0.0.0');
end;

// Microsoft's documented detection: a non-empty, non-zero 'pv' under the
// runtime's client key, per-machine (either registry view) or per-user.
function NeedsWebView2: Boolean;
begin
  Result := not (HasWebView2At(HKLM, 'SOFTWARE\WOW6432Node\' + WebView2Key)
              or HasWebView2At(HKLM, 'SOFTWARE\' + WebView2Key)
              or HasWebView2At(HKCU, 'Software\' + WebView2Key));
end;
