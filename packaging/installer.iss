; ============================================================
;  AnonSight 安装脚本（Inno Setup 6）
;
;  编译： ISCC.exe installer.iss
;  产出： packaging\installer\AnonSight-<版本>-Setup-preview.exe
;        （2026-09-19 开源发布时改的名：原来叫「…-安装程序」，中英混排的
;          文件名发给国外用户不友好；加 `preview` 是为了把预期先摆明——
;          这是能用的预览版，不是成品软件）
;
;  为什么要有安装器（主人 2026-09-19 定的）：
;    「做个安装器，打开安装器然后一键安装，选择安装位置那种」。
;    起因是单文件 exe 每次启动要解压 245 MB（实测 21 秒），
;    改用文件夹模式（1-2 秒）后产物是个文件夹，就需要安装器来"装"。
;
;  安装器负责四件运行时做不好的事：
;    ① 让用户**选安装位置**；
;    ② 建**开始菜单 / 桌面快捷方式**；
;    ③ **装的时候就写好 `.ano` 文件关联**（比程序自己运行时注册更规范，
;       而且卸载时能干净地删掉）；
;    ④ 提供**卸载**。
; ============================================================

#define AppName "AnonSight"
#define AppVer "0.1.0"
#define AppPub "liebaojun"
#define SrcDir "dist\AnonSight"

[Setup]
; AppId 一旦定下就**别再改** —— 它是"这是同一个软件"的标识，改了会被当成两个程序、
; 覆盖安装和卸载都会出问题。
AppId={{7E2A4C9B-8D31-4F60-9A57-C2B1E0F34D88}
AppName={#AppName}
AppVersion={#AppVer}
AppPublisher={#AppPub}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=installer
OutputBaseFilename={#AppName}-{#AppVer}-Setup-preview
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
SetupIconFile=..\P0\favicon.ico
UninstallDisplayIcon={app}\favicon.ico
; 装到 Program Files 需要管理员（会弹一次 UAC）。文件关联写的是 HKCU，不需要额外提权。
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64compatible
; 最低 Windows 10（WebView2 是系统自带的）
MinVersion=10.0
; ★ 告诉 Inno「这个安装会改文件关联」—— 装完它会替我们广播
;   SHChangeNotify(SHCNE_ASSOCCHANGED)。
;
;   **少了这一行，`.ano` 的图标在资源管理器里一直是空白的。** 主人 2026-09-19 实测，
;   根因已在同一台机器上复现（脚本 tools/_tmp/notify_test.py）：
;   注册表写得完全正确、`ano-file.ico` 也完全正常（Windows 能解析它全部 6 层），
;   但 shell 把"`.ano` = 未知类型"的结论**缓存在自己进程里**，不通知就永远不刷新
;   —— 实测注册完不通知，等 5 秒仍显示空白图标；补一次通知，**立刻**变成自定义图标。
;
;   所以"换图标工具重新生成 ico""删 IconCache.db 重启资源管理器"那些偏方在这里
;   都不对症：那些治的是另外两种病（ico 文件坏 / 缓存文件脏），不是这一种。
ChangesAssociations=yes

[Languages]
; 中文语言包不是 Inno 自带的，缺了会编译失败；有就用，没有回退英文。
; （Inno 6 官方中文包在 https://jrsoftware.org/files/istrans/ 下载后丢进 Languages\）
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："; Flags: checkedonce
Name: "assocano"; Description: "把 .ano 文件关联到 AnonSight（双击就能打开项目文件）"; GroupDescription: "文件关联："; Flags: checkedonce

[Files]
; 程序本体（文件夹模式打出来的那一坨）
;
; ⚠ `Excludes` 这三项是**2026-09-19 开源发布时补的，必须留着** —— 当时差点出事：
;   `paths.DATA = exe 所在目录`（见 core/paths.py），所以**只要有人跑过一次 dist 里那个
;   打包版**（冒烟测试就会跑），程序就会把 `papers/` 写进 `dist\AnonSight\`。
;   而这一行原本是 `{#SrcDir}\*` 通配 —— 于是**再打一次安装包，就把开发者的论文
;   和 AI 分析成果一起发给别人了**。实测：dist 里的 paper.ano 与开发者库里的
;   wu2020 MD5 一致（9AA0C297…），而它确实早于安装包编译时间。
;   开发者的论文是付费跑出来的个人数据，绝不能随包出去 —— 所以在**这一层**也堵一道，
;   不能只靠"打完记得跑 check-export"这种纪律（纪律会忘，通配符不会）。
Source: "{#SrcDir}\*"; DestDir: "{app}"; Excludes: "papers\*,papers,.keys.json"; Flags: ignoreversion recursesubdirs createallsubdirs
; 两种图标，**别用混**（主人 2026-09-19 一眼看出来的）：
;   · `favicon.ico`   = **软件图标**（只有角色）→ 快捷方式 / 卸载项 / exe 用它
;   · `ano-file.ico`  = **.ano 文件图标**（角色 + 那个大红 A）→ 只给文件关联用
; 之前快捷方式误用了 ano-file.ico，桌面上就成了"文件图标"，不是软件图标。
Source: "..\P0\favicon.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\P0\ano-file.ico"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\AnonSight.exe"; IconFilename: "{app}\favicon.ico"
Name: "{group}\卸载 {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\AnonSight.exe"; IconFilename: "{app}\favicon.ico"; Tasks: desktopicon

[Registry]
; `.ano` 文件关联。写 **HKCU**（当前用户），卸载时用 uninsdeletekey 干净删掉。
; 只在这一项被勾选时写（Tasks: assocano）。
Root: HKCU; Subkey: "Software\Classes\.ano"; ValueType: string; ValueData: "AnonSight.ano"; Flags: uninsdeletevalue; Tasks: assocano
Root: HKCU; Subkey: "Software\Classes\AnonSight.ano"; ValueType: string; ValueData: "Ano 文档（AnonSight 项目文件）"; Flags: uninsdeletekey; Tasks: assocano
Root: HKCU; Subkey: "Software\Classes\AnonSight.ano\DefaultIcon"; ValueType: string; ValueData: "{app}\ano-file.ico,0"; Flags: uninsdeletekey; Tasks: assocano
Root: HKCU; Subkey: "Software\Classes\AnonSight.ano\shell\open\command"; ValueType: string; ValueData: """{app}\AnonSight.exe"" ""%1"""; Flags: uninsdeletekey; Tasks: assocano

[Run]
Filename: "{app}\AnonSight.exe"; Description: "立即启动 {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 用户数据（papers/ 和 .keys.json）**故意不删** —— 那是别人的论文和 API Key，
; 卸载程序没资格替他们做这个决定。要清干净让他自己删。
; （所以这里什么都不写，留个说明。）
