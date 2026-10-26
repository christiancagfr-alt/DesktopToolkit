# Desktop Toolkit 1.9.14

## 修复：找不到 MSVCP140.dll（握手/传文件无法运行）
- 原因：系统缺少 **Visual C++ 2015–2022 x64 运行库**，`DesktopToolkit.exe` 启动即失败（跨网传文件/握手也会表现为「无法运行」）
- Windows **安装包**会在缺少 `MSVCP140.dll` 时自动安装 `vc_redist.x64.exe`（可能弹出 UAC）
- 发布页另附 `vc_redist.x64.exe` 与 `install-VC-redist-fix-MSVCP140.bat`，便携版用户可手动双击安装

### 已装旧版、现在就报错的机器
也可直接安装：https://aka.ms/vs/17/release/vc_redist.x64.exe （选 x64）
