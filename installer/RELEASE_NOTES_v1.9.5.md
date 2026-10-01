# Desktop Toolkit 1.9.5

## 安全加固
- 依赖升级：Pillow 12.3 / urllib3 2.8 / requests 2.34；增加 `requirements.lock`
- Zip Slip 防护（P2P / 笔记本同步 / 更新包 / Node 解压）
- 自动更新强制校验 GitHub 发布的 `.sha256`
- 局域网共享：取消 URL 传密；失败次数过多临时锁定
- `state.json` 与 Google Drive token 敏感字段本地加密（Windows DPAPI）
- CI：tag 防注入；Actions 钉死 commit SHA
- P2P 房间码加长；Worker 房间字符集校验

## 功能（延续 1.9.4）
- 侧栏按首页分类；文件整理入侧栏；悬浮菜单图标；远程入口已隐藏

## 发布包
- Windows：安装包 + 便携 zip
- macOS：dmg + zip
- Linux：x86_64 便携 zip
