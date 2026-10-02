# DesktopToolkit 安全审核报表（基于最新已发布代码）

| 项 | 内容 |
|---|---|
| 审核日期 | 2026-10-01 |
| 审核对象 | `C:\Users\Alienware\Documents\Codex\DesktopToolkit` |
| 代码基准 | 提交 `3b4e5d7`（`release: 1.9.5 security hardening`）· 版本 **1.9.5** |
| 发布页 | https://github.com/secure-artifacts/DesktopToolkit/releases/tag/v1.9.5 |
| 审核范围 | 源码、`requirements.txt` / `requirements.lock`、`.github/workflows`、`cloudflare/`、密钥与 `.gitignore`、关键安全模块冒烟 |
| 未纳入 | `dist/` / `build/` 二进制逆向；本机全局无关包仅作环境噪音说明 |
| 方法 | 结构通读、危险模式扫描、`pip-audit -r requirements.txt`、修复点冒烟、对照已发布 1.9.5 改动 |
| 结论摘要 | **Critical / High：0 未修复**；Medium 级代码与 CI 项已落地并随 1.9.5 发布；剩余为产品设计取舍与低优先级一致性问题 |

---

## 1. 项目概况

| 项 | 内容 |
|---|---|
| 语言 / 框架 | Python 3.12 + PyQt6 桌面应用 |
| 包管理 | `requirements.txt`（下限版本）+ **`requirements.lock`（可复现）** |
| 直接依赖 | 14（含条件依赖 `pywin32`；含 `cryptography` / 显式 `urllib3`） |
| 网络面 | 局域网 HTTP 文件共享、P2P WebSocket（Cloudflare Worker）、GitHub 自动更新、可选 Google Drive / Cloudflare API |
| 本地敏感数据 | `%LOCALAPPDATA%/DesktopToolkit/state.json`（字段级加密）、可选 `gdrive_token.json`（整文件加密） |
| 测试资产 | 无正式 `tests/`；本次对 Zip Slip、更新 URL 白名单、凭据加解密、房间码长度做了冒烟 |

### 直接依赖与本机解析版本（审核时）

| 直接依赖 | requirements | 本机 / lock | 备注 |
|---|---|---|---|
| PyQt6 / PyQt6-Qt6 | >=6.6 | 6.11.0 | 正常 |
| requests | >=2.32.4 | 2.34.2 | 已升 |
| urllib3 | >=2.6.3 | 2.8.0 | 已升 |
| cryptography | >=42 | 46.0.7 | 凭据加密 |
| Pillow | >=12.3.0 | 12.3.0 | 覆盖 2026 年多条 Pillow CVE 修复线 |
| numpy | >=1.24 | 1.26.4 | 正常 |
| opencv-python-headless | >=4.8 | 5.0.0.93 | 官方 OpenCV 5 wheel |
| mss | >=9.0 | 10.2.0 | 正常 |
| imageio-ffmpeg | >=0.4.9 | 0.6.0 | 正常 |
| websockets | >=12.0 | 17.0.1 | 正常 |
| sounddevice | >=0.4.6 | 0.5.5 | 正常 |
| pynput | >=1.7.6 | 1.8.2 | 正常 |
| pywin32 | >=306 (Windows) | 312 | DPAPI |

**`pip-audit -r requirements.txt`**：`No known vulnerabilities found`（项目依赖范围内）。

未发现名字可疑或明显仿冒包。

---

## 2. 问题汇总表（相对当前树）

| 编号 | 类别 | 严重级别 | 位置 / 描述 | 状态 |
|---|---|---|---|---|
| D1 | 依赖 | High | Pillow &lt;12.3.0 图像解析 / DoS / OOB 等（修复线 12.3.0） | **已修复并发布** |
| D2 | 依赖 | High | urllib3 旧线解压炸弹 / 重定向相关通告 | **已修复并发布** |
| D3 | 依赖 | Medium–High | requests &lt;2.32.4 若干通告 | **已修复并发布** |
| C1 | 代码 | High | 多处 `ZipFile.extractall`（Zip Slip） | **已修复并发布**（统一 `zip_safe.safe_extractall`） |
| C2 | 代码 | Medium | `updater.py` 更新下载未限制域名 | **已修复并发布**（HTTPS + GitHub 主机白名单） |
| C3 | 代码 | Medium | `lan_share.py` 接受 URL `?token=` / `?password=` | **已修复并发布**（仅 Header） |
| C4 | 代码 | Medium | `release.yml` `workflow_dispatch` 标签插值进 shell | **已修复并发布**（env + semver 正则） |
| C5 | 代码 | Medium | P2P 房间码过短 / Worker 校验偏松 | **已修复并发布**（默认 8；Worker `4–32` `A-Z0-9`） |
| C6 | 代码 | Medium | `state.json` 明文保存局域网密码、OWM Key、CF Token 等 | **已修复并发布**（`secret_store`） |
| C9 | CI/CD | Low–Medium | Actions 浮动 tag（`@v4` / `@v2`） | **已修复并发布**（钉 commit SHA） |
| C10 | 代码 | Medium | 更新包未强制校验 `.sha256` | **已修复并发布**（`require_sha256=True`） |
| C11 | 供应链 | Low–Medium | 无 lock 文件，CI/打包难复现 | **已修复并发布**（`requirements.lock`） |
| K1 | 密钥 | — | 仓库硬编码密钥 / `.gitignore` 覆盖 | **合规**：未见硬编码；已有 `.env.example` |
| C7 | 代码 | Low–Medium | LAN `Access-Control-Allow-Origin: *` | **建议关注**（设计取舍） |
| C8 | 代码 | Low–Medium | Cloudflare 信令：知房间码即可加入 | **建议关注**（产品模型） |
| C12 | 一致性 | Low | `p2p_ui._gen_room` 仍调用 `make_room_code(6)`，占位文案写「6 位」 | **新建发现** |
| C13 | 代码 | Low | `secret_store.protect` 异常时回退明文写入 | **新建发现**（失败敞开） |
| C14 | 产品 | Info | `remote_lan_ui.py` / `lan_remote.py` 仍在树内；发布 UI 已隐藏 | **已说明** |
| C15 | 运维 | Info | 历史上若曾外传 CF / OWM / Google 密钥，需在服务商侧轮换 | **提醒** |

**统计（当前树）**：Critical **0** · High 未修 **0** · Medium 未修 **0** · 建议关注 **2** · Low 新建 **2** · Info **2**。

---

## 3. 已落地的安全控制（对照源码）

### 3.1 依赖与可复现构建
- `requirements.txt` 抬高 `Pillow` / `urllib3` / `requests`，新增 `cryptography`。
- `requirements.lock` 钉死审核时解析版本，便于 CI/打包复现。
- `pip-audit` 对项目依赖无已知漏洞命中。

### 3.2 Zip Slip（`zip_safe.py`）
- `safe_extractall` 对每个成员做 `resolve` + `relative_to(dest)`，拒绝 `../` / 绝对路径逃逸。
- 调用点：`p2p_transfer.py`、`notebook_sync.py`、`cloudflare_api.py`（Node 解压）、`updater.py`（macOS zip）。
- 源码中**无**裸 `extractall` 解压路径。
- 冒烟：含 `../escape.txt` 的 zip → 抛出「拒绝危险压缩路径」。

### 3.3 自动更新（`updater.py` + `hub_ui.py`）
- 下载 URL 仅允许 `https` + `github.com` / `*.githubusercontent.com` 等白名单主机。
- `download_update(..., require_sha256=True)`：缺 sidecar 或摘要不符则删除文件并拒绝安装。
- Hub 更新流程传入 `require_sha256=True`。
- 冒烟：GitHub HTTPS 允许；明文 HTTP / 非 GitHub 主机拒绝。

### 3.4 局域网共享（`lan_share.py`）
- 鉴权仅 `Authorization: Bearer` / `X-Share-Token` / `X-Share-Password`；**明确拒绝** query 传密。
- 密码：PBKDF2-HMAC-SHA256（200_000 次）+ `secrets.compare_digest`；Handler 上只保留 salt+digest。
- 路径：`_safe_join` 拒绝 `..` 与越界。
- 暴力尝试：60s 内 ≥8 次失败 → 429，锁定约 120s。
- 仍存在：`Access-Control-Allow-Origin: *`（见 C7）。

### 3.5 P2P / Worker
- `make_room_code` 默认长度 **8**（字母表去掉易混字符），上限 16。
- Worker：`room` 必须 `4–32` 且 `/^[A-Z0-9]+$/`。
- 房间码即准入密钥的产品模型仍在（见 C8）。
- UI「生成」仍走 6 位（见 C12）。

### 3.6 凭据落盘（`secret_store.py` / `storage.py` / `gdrive_client.py`）
- Windows：DPAPI（`win32crypt`）；其他平台：本地 Fernet 密钥文件（尽量 `0600`）。
- `state.json`：匹配 `password` / `*_password` / `*_api_token` / `*_api_key` / `owm_api_key` / `cf_api_token` 等字段加密写入、读入解密；旧明文下次保存升级。
- Google Drive `gdrive_token.json`：整文件经 `protect` / `unprotect`。
- 冒烟：往返加解密成功；字段带 `enc:v1:` 前缀。
- 注意：`protect` 异常时回退明文（C13）。

### 3.7 CI（`.github/workflows/release.yml`）
- tag 经 `env` 传入 + `^v[0-9]+\.[0-9]+\.[0-9]+…$` 正则。
- Actions 钉 SHA：`checkout` / `setup-python` / `upload-artifact` / `download-artifact` / `softprops/action-gh-release`。

### 3.8 密钥与仓库卫生
- `.gitignore`：`.env`、`state.json`、`client_secrets*.json`、`token*.json`、`credentials*.json`。
- `.env.example`：仅变量名占位，无真实密钥。
- 源码扫描未见硬编码 API Key / Token 字面量。

### 3.9 远程控制暴露面
- Hub / 侧栏 / 首页发布路径已隐藏远程入口。
- `main.show_remote_control` 仅提示「开发中」。
- 悬浮菜单 `items` **未**挂载远程；`_act_remote` 为遗留死代码。
- `remote_lan_ui.py` / `lan_remote.py` 仍保留在树中（C14）。

### 3.10 危险原语扫描
- 无 `eval` / `exec` / `pickle.loads` / `os.system` / `shell=True` 业务用法（命中均为 Qt `exec()` 对话框）。
- 解压路径均走 `safe_extractall`。

---

## 4. 剩余风险与建议（按优先级）

| 优先级 | 项 | 说明 | 建议 |
|---|---|---|---|
| P2 | C12 P2P UI 仍生成 6 位码 | `p2p_ui._gen_room` → `make_room_code(6)`；占位「6 位房间码」；`setMaxLength(8)` 已允许更长 | 改为 `make_room_code()`（默认 8），文案改为「8 位房间码」 |
| P2 | C7 LAN CORS `*` | 任意 Origin 的浏览器页可对已鉴权会话发跨域请求（仍需知道密码） | 局域网工具场景可接受；若嵌入浏览器页，改为反射受信 Origin |
| P2 | C8 房间码即密钥 | 公开 Worker 上猜码/泄漏码即可加入 | 勿公开传播房间码；可选二次口令或短期房间 TTL |
| P3 | C13 `protect` 失败敞开 | 加密异常时写回明文，避免丢配置 | 改为失败时抛错或跳过写入并打日志，避免静默降级 |
| P3 | C14 远程源码在树 | 发布 UI 已关，但源码可被误启用 | 再启用前单独威胁建模；或移入实验分支 |
| P3 | OAuth client_secrets 文件 | 用户自备 JSON，通常明文落盘（已 gitignore 命名模式） | 多用户同机时可用 DPAPI 包装副本 |
| 运维 | C15 密钥轮换 | 仅改代码无法撤销已泄漏的云端凭证 | 在 Cloudflare / OWM / Google Cloud 控制台作废并重建 |

---

## 5. 本次冒烟结果

| 检查 | 结果 |
|---|---|
| Zip Slip 拦截 `../escape.txt` | 通过 |
| `protect` / `unprotect` 往返 | 通过 |
| `protect_state` 加密 `password` / `owm_api_key` | 通过 |
| 更新 URL 白名单（允许 GitHub HTTPS / 拒绝 HTTP 与异域） | 通过 |
| `make_room_code()` 默认长度 8；`make_room_code(6)` 仍为 6 | 与 C12 一致 |
| `pip-audit -r requirements.txt` | 无已知漏洞 |

---

## 6. 审核结论

在约定范围内，**当前 1.9.5 树（`3b4e5d7`）已消除此前识别的 Critical / High / Medium 代码与依赖问题**，并已随官方 Release（Windows 本地包 + fork CI Mac/Linux，共 10 个资产）发布。

剩余事项以**产品设计取舍**（LAN CORS、P2P 房间模型）和**低优先级一致性**（UI 仍生成 6 位房间码、`protect` 失败回退明文）为主，不阻塞当前版本继续使用。若出下一小版本，优先把 C12 房间码生成与文案对齐到默认 8 位。

---

## 7. 附录：关键文件索引

| 文件 | 作用 |
|---|---|
| `zip_safe.py` | Zip Slip 安全解压 |
| `secret_store.py` | DPAPI / Fernet 凭据包装 |
| `storage.py` | `state.json` 读写时加解密 |
| `updater.py` | 更新域名白名单 + SHA256 强制校验 |
| `lan_share.py` | Header 鉴权、PBKDF2、路径安全、失败锁定 |
| `p2p_transfer.py` | 房间码生成、接收 zip 安全解压 |
| `p2p_ui.py` | P2P UI（含 C12） |
| `cloudflare/signaling-worker.js` | 房间码字符集/长度校验 |
| `gdrive_client.py` | Drive token 文件加解密 |
| `.github/workflows/release.yml` | tag 防注入 + Actions SHA 钉死 |
| `requirements.txt` / `requirements.lock` | 依赖下限与锁文件 |
| `.env.example` | 环境变量模板（无密钥） |
