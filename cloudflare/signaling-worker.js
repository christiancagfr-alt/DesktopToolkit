/**
 * Desktop Toolkit — zero-storage WebSocket signaling + chunk relay Worker
 *
 * Deploy (Cloudflare free):
 *   1. npx wrangler login
 *   2. cd cloudflare && npx wrangler deploy
 *   3. Copy the worker URL into the app "中转地址"
 *
 * Behavior:
 *   - Room-based WebSocket fan-out (join with ?room=CODE)
 *   - Messages are forwarded to other peers in the same room only
 *   - File bytes are streamed in memory and NEVER written to R2/KV/disk
 *   - /usage returns approximate daily request count (Cache API, no R2/KV)
 *
 * Rooms use Durable Objects so both peers always land on the same instance.
 * Free plan: ~100,000 requests/day per account (shared). WebSocket Upgrade
 * counts as 1 request; messages after connect do NOT count as requests.
 */

const FREE_DAILY_LIMIT = 100000;

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const path =
      url.pathname.length > 1 && url.pathname.endsWith("/")
        ? url.pathname.slice(0, -1)
        : url.pathname;

    if (path === "/" || path === "/health") {
      ctx.waitUntil(bumpDayCounter());
      return json({
        ok: true,
        name: "desktop-toolkit-p2p-signaling",
        storage: "none",
        durable_rooms: Boolean(env.ROOMS),
        usage: "WebSocket ws(s)://<host>/ws?room=ROOMCODE",
        endpoints: {
          health: "/health",
          usage: "/usage",
          ws: "/ws?room=ROOMCODE",
        },
      });
    }

    if (path === "/usage") {
      await bumpDayCounter();
      const snap = await readDayCounter();
      const used = snap.used;
      const remaining = Math.max(0, FREE_DAILY_LIMIT - used);
      const pct = Math.min(100, Math.round((used / FREE_DAILY_LIMIT) * 10000) / 100);
      return json({
        ok: true,
        plan: "free",
        daily_limit: FREE_DAILY_LIMIT,
        day_utc: snap.day,
        worker_requests_today: used,
        remaining_estimate: remaining,
        percent_used: pct,
        durable_rooms: Boolean(env.ROOMS),
        note:
          "近似值：本 Worker 今日请求（边缘缓存计数，非 Cloudflare 账户精确账单）。" +
          "免费额度是整个账户共享 10 万次/天；WebSocket 建连计 1 次，传文件数据块不计请求。",
        tips: {
          ws_upgrade_counts_as_request: true,
          ws_messages_count_as_request: false,
          one_transfer_roughly: "双方各连 1 次 ≈ 2 次请求",
          room_pairing:
            env.ROOMS
              ? "Durable Objects 房间已启用，双方可跨节点配对"
              : "未启用 Durable Objects：双方可能连到不同实例导致等对方超时",
        },
      });
    }

    if (path === "/ws") {
      ctx.waitUntil(bumpDayCounter());
      const room = (url.searchParams.get("room") || "").trim().toUpperCase();
      // Alphanumeric room codes only — reject path/injection-like values
      if (!room || room.length < 4 || room.length > 32 || !/^[A-Z0-9]+$/.test(room)) {
        return new Response("room required (?room=ABCD, 4-32 A-Z0-9)", { status: 400 });
      }
      if (request.headers.get("Upgrade") !== "websocket") {
        return new Response("expected websocket", { status: 426 });
      }

      // Prefer Durable Object rooms (sticky per room code).
      if (env.ROOMS) {
        const id = env.ROOMS.idFromName(room);
        const stub = env.ROOMS.get(id);
        return stub.fetch(request);
      }

      // Fallback only (unreliable across Cloudflare isolates)
      return handleEphemeralRoom(request, room);
    }

    return new Response("not found", { status: 404 });
  },
};

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store",
      "access-control-allow-origin": "*",
    },
  });
}

function utcDay() {
  return new Date().toISOString().slice(0, 10);
}

function counterKey(day) {
  return new Request(`https://quaker-parrot-p2p.internal/usage-counter/${day}`);
}

async function readDayCounter() {
  const day = utcDay();
  try {
    const hit = await caches.default.match(counterKey(day));
    if (hit) {
      const used = parseInt(await hit.text(), 10);
      return { day, used: Number.isFinite(used) && used >= 0 ? used : 0 };
    }
  } catch (_) {}
  return { day, used: 0 };
}

async function bumpDayCounter() {
  try {
    const day = utcDay();
    const key = counterKey(day);
    let used = 0;
    const hit = await caches.default.match(key);
    if (hit) {
      used = parseInt(await hit.text(), 10) || 0;
    }
    used += 1;
    const resp = new Response(String(used), {
      headers: {
        "content-type": "text/plain",
        "cache-control": "max-age=172800",
      },
    });
    await caches.default.put(key, resp);
  } catch (_) {}
}

/** In-isolate room map — only works if both peers hit the same isolate. */
const EPHEMERAL = new Map();

function handleEphemeralRoom(request, room) {
  const pair = new WebSocketPair();
  const [client, server] = Object.values(pair);
  server.accept();

  if (!EPHEMERAL.has(room)) EPHEMERAL.set(room, new Set());
  const peers = EPHEMERAL.get(room);
  peers.add(server);

  server.addEventListener("message", (event) => {
    for (const p of peers) {
      if (p !== server && p.readyState === 1) {
        try {
          p.send(event.data);
        } catch (_) {}
      }
    }
  });

  const cleanup = () => {
    peers.delete(server);
    if (peers.size === 0) EPHEMERAL.delete(room);
  };
  server.addEventListener("close", cleanup);
  server.addEventListener("error", cleanup);

  return new Response(null, { status: 101, webSocket: client });
}

/**
 * Durable Object room — same room code always routes to the same instance.
 * Hibernation API: getWebSockets() survives DO sleep.
 */
export class Room {
  constructor(state, env) {
    this.state = state;
    this.env = env;
  }

  async fetch(request) {
    if (request.headers.get("Upgrade") !== "websocket") {
      return new Response("expected websocket", { status: 426 });
    }
    const pair = new WebSocketPair();
    const [client, server] = Object.values(pair);
    // Hibernatable WebSocket — required for DO free plan reliability
    this.state.acceptWebSocket(server);
    return new Response(null, { status: 101, webSocket: client });
  }

  async webSocketMessage(ws, message) {
    const peers = this.state.getWebSockets();
    for (const p of peers) {
      if (p === ws) continue;
      try {
        p.send(message);
      } catch (_) {}
    }
  }

  async webSocketClose(ws, code, reason, wasClean) {
    try {
      ws.close(code, reason);
    } catch (_) {}
  }

  async webSocketError(ws) {
    try {
      ws.close(1011, "error");
    } catch (_) {}
  }
}
