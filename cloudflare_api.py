"""Cloudflare helpers for P2P signaling Worker: list / resolve URL / local wrangler deploy."""

from __future__ import annotations

import json
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable


API = "https://api.cloudflare.com/client/v4"


def _request(
    token: str,
    method: str,
    path: str,
    *,
    data: dict | None = None,
    timeout: float = 30.0,
) -> dict:
    url = path if path.startswith("http") else f"{API}{path}"
    body = None
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "DesktopToolkit/1.9",
    }
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except Exception:
            raise RuntimeError(f"Cloudflare HTTP {e.code}: {raw[:300]}") from e
        errs = payload.get("errors") or []
        msg = errs[0].get("message") if errs else raw[:300]
        raise RuntimeError(f"Cloudflare: {msg}") from e
    if not payload.get("success", True):
        errs = payload.get("errors") or []
        msg = errs[0].get("message") if errs else "unknown error"
        raise RuntimeError(f"Cloudflare: {msg}")
    return payload


def list_accounts(token: str) -> list[dict[str, str]]:
    data = _request(token, "GET", "/accounts?per_page=50")
    out = []
    for row in data.get("result") or []:
        out.append({"id": str(row.get("id") or ""), "name": str(row.get("name") or "")})
    return [a for a in out if a["id"]]


def list_worker_scripts(token: str, account_id: str) -> list[str]:
    data = _request(token, "GET", f"/accounts/{account_id}/workers/scripts")
    names = []
    for row in data.get("result") or []:
        n = str(row.get("id") or row.get("name") or "").strip()
        if n:
            names.append(n)
    return names


def workers_subdomain(token: str, account_id: str) -> str:
    data = _request(token, "GET", f"/accounts/{account_id}/workers/subdomain")
    result = data.get("result") or {}
    return str(result.get("subdomain") or "").strip()


def worker_https_url(script_name: str, subdomain: str) -> str:
    name = script_name.strip()
    sub = subdomain.strip()
    if not name or not sub:
        return ""
    return f"https://{name}.{sub}.workers.dev"


def resolve_handshake_url(token: str, account_id: str, script_name: str) -> str:
    sub = workers_subdomain(token, account_id)
    return worker_https_url(script_name, sub)


def cloudflare_dir() -> Path:
    try:
        from skin import bundle_root

        p = bundle_root() / "cloudflare"
        if p.is_dir():
            return p
    except Exception:
        pass
    return Path(__file__).resolve().parent / "cloudflare"


def deploy_signaling_worker(
    *,
    on_log: Callable[[str], None] | None = None,
) -> str:
    """
    Run `npx wrangler deploy` in cloudflare/ and parse workers.dev URL from output.
    Returns https://…workers.dev URL. Raises RuntimeError on failure.
    """
    def log(msg: str) -> None:
        if on_log:
            try:
                on_log(msg)
            except Exception:
                pass

    cf = cloudflare_dir()
    if not cf.is_dir():
        raise RuntimeError(f"找不到 cloudflare 目录：{cf}")
    log(f"部署目录：{cf}")

    # Ensure logged in (non-interactive whoami first)
    who = subprocess.run(
        ["npx", "--yes", "wrangler", "whoami"],
        cwd=str(cf),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
    )
    if who.returncode != 0:
        log("未登录 Cloudflare，请在弹出的浏览器中完成登录…")
        login = subprocess.run(
            ["npx", "--yes", "wrangler", "login"],
            cwd=str(cf),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
        )
        if login.returncode != 0:
            raise RuntimeError(
                "Cloudflare 登录失败。也可在终端手动执行：npx wrangler login\n"
                + (login.stderr or login.stdout or "")[:400]
            )

    log("正在 wrangler deploy…")
    dep = subprocess.run(
        ["npx", "--yes", "wrangler", "deploy"],
        cwd=str(cf),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
    )
    out = (dep.stdout or "") + "\n" + (dep.stderr or "")
    log(out[-800:] if len(out) > 800 else out)
    if dep.returncode != 0:
        raise RuntimeError("部署失败：\n" + out[-600:])

    # Parse https://name.sub.workers.dev from deploy output
    m = re.search(r"https://[a-zA-Z0-9._-]+\.workers\.dev", out)
    if m:
        return m.group(0).rstrip("/")
    # Fallback: name from wrangler.toml + subdomain via API not available here
    name = "desktop-toolkit-p2p"
    try:
        text = (cf / "wrangler.toml").read_text(encoding="utf-8", errors="replace")
        mm = re.search(r'name\s*=\s*"([^"]+)"', text)
        if mm:
            name = mm.group(1)
    except Exception:
        pass
    raise RuntimeError(
        f"部署似乎成功，但未从输出解析到 workers.dev 地址。\n"
        f"请到 Cloudflare 控制台查看 Worker「{name}」的地址后填入。"
    )


def fetch_account_workers_usage_today(
    token: str,
    account_id: str,
) -> dict[str, Any]:
    """
    Approximate account-level Workers invocations for today (UTC) via GraphQL.
    Requires a token with Account Analytics Read.
    """
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    day = now.strftime("%Y-%m-%d")
    query = {
        "query": """
        query ($accountTag: string!, $filter: AccountWorkersInvocationsAdaptiveFilter_InputObject) {
          viewer {
            accounts(filter: {accountTag: $accountTag}) {
              workersInvocationsAdaptive(limit: 1, filter: $filter) {
                sum { requests }
              }
            }
          }
        }
        """,
        "variables": {
            "accountTag": account_id,
            "filter": {
                "datetime_geq": f"{day}T00:00:00Z",
                "datetime_leq": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        },
    }
    # Cloudflare GraphQL endpoint
    url = "https://api.cloudflare.com/client/v4/graphql"
    body = json.dumps(query).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "DesktopToolkit/1.9",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    if payload.get("errors"):
        err = payload["errors"][0].get("message") if payload["errors"] else "GraphQL error"
        raise RuntimeError(str(err))
    used = 0
    try:
        accounts = payload["data"]["viewer"]["accounts"]
        rows = accounts[0]["workersInvocationsAdaptive"]
        if rows:
            used = int(rows[0]["sum"]["requests"] or 0)
    except Exception:
        used = 0
    limit = 100_000
    return {
        "ok": True,
        "day_utc": day,
        "account_requests_today": used,
        "daily_limit": limit,
        "remaining": max(0, limit - used),
        "source": "cloudflare_graphql",
        "note": "账户级 Workers 调用量（今日 UTC），非单个 Worker 近似计数。",
    }
