# Desktop Toolkit 1.9.15

## 修复：对端电脑安装 VC++ 后仍提示找不到 MSVCP140.dll
- **原因**：Windows 优先在程序目录加载 `MSVCP140.dll`；PyQt 自带的副本在 `PyQt6\Qt6\bin\` 子目录，不在默认搜索路径。对端若系统目录也没有该 DLL，就会继续报错。
- **修复**：打包时把 `MSVCP140.dll` / `MSVCP140_1.dll` / `MSVCP140_2.dll` 等复制到 `DesktopToolkit.exe` 同目录和 `_internal\`，不再强依赖系统已装 VC++。
- 仍保留安装包内的 `vc_redist` 检测安装作为双保险。
- 关闭 UPX，避免压缩破坏原生 DLL。

### 对端请
安装/覆盖 **1.9.15** Windows setup 或 portable zip 后再试握手传文件。
