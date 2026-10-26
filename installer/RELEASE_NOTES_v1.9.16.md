# Desktop Toolkit 1.9.16

## 安全
- **Pillow ≥ 13.0.0**：覆盖 12.3.0 线已知 Critical/High 图像解析问题
- **certifi ≥ 2026.10.19**：更新 CA 捆绑，规避 emSign 被吊销中间证书风险
- **凭据落盘 fail-closed**：加密失败不再把明文密码/Token 写入 `state.json` / Google token 文件
- **P2P 房间码**：UI 生成与文案对齐默认 8 位
- **软件卸载**：移除 `shell=True` 回退，降低注册表卸载命令注入面
- 重写 `requirements.lock` 以便复现构建

## 说明
- 仍包含 1.9.15 的 MSVCP140 内置修复（对端不必单独装齐 VC++ 也能启动）
