# Desktop Toolkit 1.9.10

## 修复（macOS / Linux）
- **全局快捷键**：主窗口 / 截图快捷键改为系统级热键（pynput）；其它应用在前台时也可触发
- macOS 若仍无效：请在「系统设置 → 隐私与安全 → 辅助功能 / 输入监控」中允许本应用
- **主界面抢前台**：停止 macOS/Linux 上悬浮窗定时 `raise_()`；字幕提示也不再激活整个应用，避免主窗口频繁盖住其它软件

## 发布包
- Windows：安装包 + 便携 zip
- macOS：dmg + zip
- Linux：x86_64 便携 zip
