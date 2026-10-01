# DesktopToolkit 安全审核报告（修复后终版）

| 项 | 内容 |
|---|---|
| 审核日期 | 2026-10-01 |
| 审核对象 | `C:\Users\Alienware\Documents\Codex\DesktopToolkit` |
| 审核范围 | 源码、`requirements.txt`、`.github/workflows`、`cloudflare/`、密钥与 `.gitignore` |
| 未纳入 | `dist/` / `build/` 产物深度审计；本机全局无关包（streamlit / torch 等）仅作噪音说明 |
| 方法 | 结构通读、`pip-audit`、WebSearch（GitHub Advisory / OSV / NVD）、危险模式扫描、修复点冒烟 |
| 状态 | **高危项已修复**；剩余为设计取舍与加固建议 |

---

## 1. 项目概况

| 项 | 内容 |
|---|---|
| 语言 / 框架 | Python 3.12 + PyQt6 桌面应用 |
| 包管理 | `requirements.txt`（无 lock 文件） |
| 直接依赖数量 | 13（含平台条件 `pywin32`，含新增 `cryptography` / 显式 `urllib3`） |
| 网络面 | 局域网 HTTP 文件共享、P2P WebSocket（Cloudflare Worker）、GitHub 自动更新、可选 Google Drive / Cloudflare API |
| 测试资产 | 仓库无正式 `tests/`；对 Zip Slip、更新 URL 白名单、LAN 鉴权、凭据加解密做了冒烟验证 |

### 直接依赖与本机解析版本（修复后）

| 直接依赖 | requirements | 本机实测 | 备注 |
|---|---|---|---|
| PyQt6 / PyQt6-Qt6 | >=6.6 | 6.11.0 | 正常 |
| requests | >=2.32.4 | 2.34.2 | 已升 |
| urllib3 | >=2.6.3 | 2.8.0 | 已升 |
| cryptography | >=42 | 46.0.7 | 凭据加密用 |
| numpy | >=1.24 | 1.26.4 | 正常 |
| opencv-python-headless | >=4.8 | 5.0.0.93 | 官方 OpenCV 5 wheel，非仿冒 |
| mss | >=9.0 | 10.2.0 | 正常 |
| imageio-ffmpeg | >=0.4.9 | 0.6.0 | 正常 |
| Pillow | >=12.3.0 | 12.3.0 | 已升 |
| websockets | >=12.0 | 17.0.1 | 正常 |
| sounddevice | >=0.4.6 | 0.5.5 | 正常 |
| pynput | >=1.7.6 | 1.8.2 | 正常 |
| pywin32 | >=306 (Windows) | 已装 | DPAPI 依赖 |

未发现名字可疑或明显仿冒包。无长期停更的核心直接依赖。

---

## 2. 问题汇总表

| 编号 | 类别 | 严重级别 | 位置 | 状态 |
|---|---|---|---|---|
| D1 | 依赖 | High | Pillow 10.4.0 图像解析 / DoS / OOB 等（修复线 12.3.0） | **已修复** |
| D2 | 依赖 | High | urllib3 1.26.20 解压炸弹 / 重定向相关通告（修复线 2.6.x–2.8.0） | **已修复** |
| D3 | 依赖 | Medium–High | requests 2.31.0 若干通告（修复线 2.32.4+） | **已修复** |
| C1 | 代码 | High | 多处 `ZipFile.extractall`（Zip Slip）：`p2p_transfer.py` / `notebook_sync.py` / `cloudflare_api.py` / `updater.py` | **已修复** |
| C2 | 代码 | Medium | `updater.py` 更新下载未限制域名（SSRF / 供应链风险） | **已修复** |
| C3 | 代码 | Medium | `lan_share.py` 接受 URL `?token=` / `?password=`（凭据进日志/历史） | **已修复** |
| C4 | 代码 | Medium | `.github/workflows/release.yml` `workflow_dispatch` 标签插值进 shell | **已修复** |
| C5 | 代码 | Medium | P2P 房间码过短 / Worker 房间校验偏松 | **已修复** |
| C6 | 代码 | Medium | `state.json` 明文保存局域网密码、OWM Key、CF Token 等 | **已修复** |
| C7 | 代码 | Low–Medium | LAN `Access-Control-Allow-Origin: *` | **建议关注** |
| C8 | 代码 | Low–Medium | Cloudflare 信令：知房间码即可加入 | **建议关注** |
| C9 | CI/CD | Low–Medium | Actions 浮动 tag（`@v4` / `@v2`） | **已修复** |
| K1 | 密钥 | — | 仓库未发现硬编码密钥；敏感文件模式已在 `.gitignore` | **已补充** `.env.example` |

**统计**：Critical 0 · High 已修 3 · Medium 已修 7 · 建议关注 2 · 密钥流程已规范。

---

## 3. 已修复的问题及具体改动

1. **依赖升级（`requirements.txt`）**  
   - `Pillow>=12.3.0`（本机 12.3.0）  
   - `urllib3>=2.6.3`（本机 2.8.0）  
   - `requests>=2.32.4`（本机 2.34.2）  
   - 新增 `cryptography>=42`  
   **原因**：消除公开 High/Medium 通告；凭据模块需要加密库。

2. **Zip Slip（新建 `zip_safe.py`）**  
   - `safe_extractall` 拒绝 `../` / 绝对路径逃逸  
   - 已替换：`p2p_transfer.py`、`notebook_sync.py`、`cloudflare_api.py`、`updater.py`  
   - `DesktopToolkit.spec` 增加 `zip_safe` / `secret_store` hiddenimport  
   **原因**：P2P 收到的 zip、云端备份 zip、Node 安装包、更新包若含穿越路径可写到任意目录。

3. **更新下载域名白名单（`updater.py`）**  
   - 仅 `https` + `github.com` / `*.githubusercontent.com`  
   **原因**：防止被篡改的发布元数据指向恶意下载地址。

4. **LAN 鉴权（`lan_share.py`）**  
   - 取消 query 传密；仅 `Authorization: Bearer` / `X-Share-*`  
   **原因**：URL 中的密码会进入代理日志与浏览器历史。

5. **CI 脚本注入（`release.yml`）**  
   - tag 经 `env` 传入 + 严格 semver 正则  
   **原因**：`workflow_dispatch` 恶意 tag 可注入 shell。

6. **P2P 房间（`p2p_transfer.py` + `signaling-worker.js`）**  
   - 默认房间码 8 位；Worker 限制 4–32 位 `A-Z0-9`  
   **原因**：降低公开 Worker 上房间码被猜中的概率。

7. **凭据落盘（新建 `secret_store.py`，改 `storage.py`）**  
   - 写盘加密、读入解密  
   - Windows：DPAPI（绑定当前用户）  
   - 其他平台：本地 Fernet 密钥文件（尽量 `0600`）  
   - 覆盖 `password` / `*_password` / `*_api_token` / `*_api_key` 等字段  
   - 旧明文下次保存自动升级  
   **原因**：本机其他进程/备份可读明文 `state.json`。

8. **Actions 钉 SHA（`release.yml`）**  
   - `actions/checkout@11bd719…`  
   - `actions/setup-python@4237552…`  
   - `actions/upload-artifact@ea165f8…`  
   - `actions/download-artifact@95815c3…`  
   - `softprops/action-gh-release@c95fe14…`  
   **原因**：降低浮动 tag 被供应链替换的风险。

9. **`.env.example`**  
   - 仅变量名，提醒勿提交真实密钥。

**验证**：Zip Slip 拦截、更新 URL 白名单、房间码长度、LAN Header 鉴权、凭据加解密往返 — 冒烟通过。

---

## 4. 需要开发者决定 / 已知说明

当前**无阻塞性「待开发者决定」项**。以下为可选后续：

| 议题 | 可选方案 | 建议 |
|---|---|---|
| Google OAuth `token*.json` 仍为独立明文文件（已 gitignore） | A. 同样 DPAPI 包装　B. 维持现状 | 若多用户共用同一 Windows 配置目录，选 A |
| 增加 `requirements.lock` | A. 增加 lock　B. 维持下限版本 | 建议 A，保证 CI/打包可复现 |
| 客户端强制校验发布包 `.sha256` | A. 更新器强制核对　B. 仅域名白名单 | 建议下一版做 A |
| 本机全局 `moviepy`/`streamlit` 与 Pillow 12 冲突 | 使用项目独立 venv / CI 干净环境 | 不影响本项目打包 |

---

## 5. 剩余风险与后续建议

1. **LAN CORS `*`（C7）**：局域网工具场景可接受；若以后要给浏览器页用，可改为反射白名单来源。  
2. **P2P 房间即密钥（C8）**：勿把房间码发到公开频道；可再加一次性口令。  
3. **LAN 弱密码 + `0.0.0.0`**：同网段可尝试连接；可加失败限速或仅绑定所选网卡。  
4. **自动更新**：已限域名；建议下一版强制核对 GitHub 附带的 `.sha256`。  
5. **远程控制源码仍在树内、发布 UI 已隐藏**：再启用前需单独威胁建模。  
6. **密钥轮换提醒**：若历史上曾外传 Cloudflare / OWM / Google OAuth 客户端密钥，**必须在对应平台作废并重建**；只改代码不够。本次仓库扫描**未见硬编码密钥**。  
7. **发版**：上述修复尚未随正式版本号发出；建议 commit 后打 1.9.5（或你指定版本）再打包。

---

## 6. 审核结论

在约定范围内：**Critical / High 问题均已修复**；Medium 级代码与 CI 问题已落地；剩余为局域网/P2P 产品设计层面的可接受风险与后续加固项。  
完整材料见本文件；相关改动目前仍为工作区未提交状态（`git status` 可见）。
