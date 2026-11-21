; ════════════════════════════════════════════════════════════════════════════
; Humanizer Windows Installer Configuration
; Built with Inno Setup 6 (https://jrsoftware.org/isinfo.php)
;
; Build command: iscc "HumanizerSetup.iss"
; ════════════════════════════════════════════════════════════════════════════

#define MyAppName "Humanizer"
#define MyAppDisplayName "Humanizer · 去AI味中文改写工具"
#define MyAppVersion "0.1.0"
#define MyAppPublisher "Humanizer Contributors"
#define MyAppURL "https://github.com/Narcooo/humanizer"
#define MyAppExeName "humanizer-web.exe"
#define MyAppCLIExeName "humanizer.exe"

[Setup]
; ── 基础信息 ─────────────────────────────────────────────────────────────────
AppId={{A3F7C8E1-9B2D-4E5F-A1C3-D6E8F0A2B4C6}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppDisplayName} v{#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}/releases
DefaultDirName={autopf}\Humanizer
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=no
LicenseFile=..\LICENSE
OutputDir=dist\installer
OutputBaseFilename=Humanizer_Setup_{#MyAppVersion}
CreateAppDir=yes
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64
ShowLanguageDialog=no

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "在桌面创建快捷方式"; GroupDescription: "附加选项:"; Flags: checkedonce
Name: "startmenuicon"; Description: "在开始菜单创建快捷方式"; GroupDescription: "附加选项:"; Flags: checkedonce

[Files]
; ── 主程序文件 ────────────────────────────────────────────────────────────────
Source: "..\dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist\{#MyAppCLIExeName}"; DestDir: "{app}"; Flags: ignoreversion
; 复制所有依赖文件（PyInstaller 打包的 DLL 等）
Source: "..\dist\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; 排除不需要的文件类型
Source: "..\dist\*.txt"; DestDir: "{app}"; Flags: exclude
Source: "..\dist\*.log"; DestDir: "{app}"; Flags: exclude
Source: "..\dist\*_locales\*"; DestDir: "{app}\_locales"; Flags: recursesubdirs; Note: Keep Chrome locales

; ── 文档和配置 ────────────────────────────────────────────────────────────────
Source: "..\README.md"; DestDir: "{app}"
Source: "..\LICENSE"; DestDir: "{app}"
Source: "..\.env.example"; DestDir: "{app}"

[Icons]
; ── 开始菜单 ────────────────────────────────────────────────────────────────
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Comment: "启动 Humanizer Web UI"
Name: "{group}\{#MyAppName} (CLI)"; Filename: "{app}\{#MyAppCLIExeName}"; Comment: "命令行工具"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"

; ── 桌面快捷方式 ────────────────────────────────────────────────────────────
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
; ── 安装完成后提示用户 ───────────────────────────────────────────────────────
Filename: "{app}\{#MyAppExeName}"; Description: "启动 Humanizer"; Flags: postinstall nowait skipifsilent; StatusMsg: "正在启动服务..."

[Code]
// ── 安装完成后的自定义页面 ──────────────────────────────────────────────────
procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssDone then
  begin
    // 可选：显示欢迎页面或说明
  end;
end;
