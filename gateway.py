#!/usr/bin/env python3
"""api-watch 本地网关：给 Codex / Claude Code 一个固定入口，背后按渠道健康度自动切换。

    Codex:        base_url = http://127.0.0.1:8788/v1
    Claude Code:  ANTHROPIC_BASE_URL = http://127.0.0.1:8788

切换规则：按 priority 从小到大试；处于冷却期（近期失败）的渠道排到最后；
连接失败 / 超时 / 429 / 5xx 会重试下一个渠道，并把该渠道打入冷却。
4xx（除 429）视为请求本身有问题，不重试。
"""
from __future__ import annotations

import argparse
import http.client
import json
import sqlite3
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import core


def db_write(db, sql: str, args: tuple = ()) -> None:
    """写库失败只记 stderr：写 watch.db 出错不能让转发断掉——上游已经生成完的回复
    会被白白丢掉，客户端只看到 ECONNRESET，额度照扣。"""
    try:
        db.execute(sql, args)
        db.commit()
    except sqlite3.Error as e:  # noqa: BLE001
        print(f"[gateway] 写 watch.db 失败：{e}", file=sys.stderr, flush=True)


def db_call(db, fn, *args) -> None:
    """调 core 里的写库函数，同样不影响转发。"""
    try:
        fn(db, *args)
    except sqlite3.Error as e:  # noqa: BLE001
        print(f"[gateway] 写 watch.db 失败（{fn.__name__}）：{e}",
              file=sys.stderr, flush=True)


HOP_HEADERS = {
    "host", "content-length", "transfer-encoding", "connection", "keep-alive",
    "authorization", "x-api-key", "accept-encoding", "content-encoding",
}
RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504, 520, 522, 524, 529}


def pick_channels(cfg: dict, db, model: str | None, api_style: str) -> list[dict]:
    chans = [c for c in cfg["channels"] if c.get("api", "openai") == api_style]
    if model:
        exact = [c for c in chans if model in (c.get("models") or [])]
        if exact:
            chans = exact
    now = time.time()
    return sorted(chans, key=lambda c: (
        1 if core.cooldown_until(db, c["id"]) > now else 0, c.get("priority", 1)))


def split_target(ch: dict, path: str) -> tuple[str, int, str, bool]:
    base = ch["base_url"].rstrip("/")
    secure = base.startswith("https://")
    rest = base.split("://", 1)[1]
    host = rest.split("/", 1)[0]
    prefix = ("/" + rest.split("/", 1)[1]) if "/" in rest else ""
    # 客户端（Codex 等）自己会带 /v1，渠道 base_url 里也可能带 /v1：去掉重复的一段，
    # 否则会拼成 /v1/v1/chat/completions，上游回 "404 page not found"（2026-11-15 实测）。
    if prefix and (path == prefix or path.startswith(prefix + "/")):
        path = path[len(prefix):]
    hostname, _, port = host.partition(":")
    return hostname, int(port or (443 if secure else 80)), prefix + path, secure


class Relay:
    def __init__(self, cfg: dict, db):
        self.cfg = cfg
        self.db = db
        # 探针的超时是 60s，转发不能沿用：Codex 一轮 SSE 流经常跑几分钟，
        # 用探针超时会在流中途 socket timeout。
        self.timeout = (cfg.get("gateway") or {}).get("timeout_sec") or cfg["probe"]["timeout_sec"]
        self.cooldown = cfg["probe"]["cooldown_sec"]

    def open(self, ch: dict, path: str, headers: dict, body: bytes,
             method: str = "POST"):
        host, port, target, secure = split_target(ch, path)
        conn = (http.client.HTTPSConnection if secure else http.client.HTTPConnection)(
            host, port, timeout=self.timeout)
        # 方法原样透传：Claude Code 会发 GET /v1/models（模型列表）、
        # POST /v1/messages/count_tokens 等非 /messages 路径，一律按 POST 转发会回 404。
        conn.request(method, target, body=body or None, headers=headers)
        return conn, conn.getresponse()

    def forward(self, method: str, path: str, headers: dict, body: bytes,
                payload_model: str | None):
        api_style = "anthropic" if "/messages" in path else "openai"
        try:
            candidates = pick_channels(self.cfg, self.db, payload_model, api_style)
        except sqlite3.Error as e:  # 读冷却表失败就退化成纯优先级排序，不能让请求挂掉
            print(f"[gateway] 读 cooldown 失败，按优先级直排：{e}", file=sys.stderr, flush=True)
            chans = [c for c in self.cfg["channels"] if c.get("api", "openai") == api_style]
            if payload_model:
                exact = [c for c in chans if payload_model in (c.get("models") or [])]
                chans = exact or chans
            candidates = sorted(chans, key=lambda c: c.get("priority", 1))
        if not candidates:
            return 503, {"error": {"message": "no channel configured"}}, None, None
        last_error = "no channel tried"
        tried = []
        for idx, ch in enumerate(candidates):
            key = core.channel_key(ch)
            if not key:
                last_error = f"{ch['id']}: 缺少 API Key"
                continue
            fwd = {k: v for k, v in headers.items() if k.lower() not in HOP_HEADERS}
            if ch.get("api") == "anthropic":
                fwd["x-api-key"] = key
                # Claude Code 发的是 OAuth token（ANTHROPIC_AUTH_TOKEN → Bearer），
                # 这里改写成渠道 Key 后要删掉原 Bearer，否则上游看到两个凭据。
                fwd.pop("Authorization", None)
                fwd.pop("authorization", None)
                fwd.setdefault("anthropic-version", "2023-06-01")
            else:
                fwd["Authorization"] = f"Bearer {key}"
            if body:
                fwd["Content-Length"] = str(len(body))
            started = time.time()
            try:
                conn, resp = self.open(ch, path, fwd, body, method)
            except Exception as e:  # noqa: BLE001
                last_error = f"{ch['id']}: {e}"
                tried.append(ch["id"])
                db_call(self.db, core.set_cooldown, ch["id"], self.cooldown, str(e))
                continue
            status = resp.status
            if status in RETRY_STATUS and idx < len(candidates) - 1:
                detail = resp.read(500).decode("utf-8", "replace")
                conn.close()
                last_error = f"{ch['id']}: HTTP {status} {detail[:200]}"
                tried.append(ch["id"])
                db_call(self.db, core.set_cooldown, ch["id"], self.cooldown, f"HTTP {status}")
                continue
            db_write(
                self.db,
                "INSERT INTO relay_log(ts, model, channel_id, ok, status, latency_ms,"
                " fallback_from, error) VALUES (?,?,?,?,?,?,?,?)",
                (time.time(), payload_model or "-", ch["id"], 1 if status < 400 else 0,
                 status, int((time.time() - started) * 1000),
                 tried[-1] if tried else None,
                 None if status < 400 else last_error))
            if status < 400:
                db_call(self.db, core.clear_cooldown, ch["id"])
            if tried:
                db_call(self.db, core.log_event, "warn", ch["id"],
                        f"自动切换生效：{tried} -> {ch['id']}")
            return status, resp, conn, ch["id"]
        db_write(
            self.db,
            "INSERT INTO relay_log(ts, model, channel_id, ok, status, latency_ms,"
            " fallback_from, error) VALUES (?,?,?,?,?,?,?,?)",
            (time.time(), payload_model or "-", "-", 0, 0, 0, None, last_error))
        db_call(self.db, core.log_event, "error", "-", f"全部渠道不可用：{last_error}")
        return 502, {"error": {"message": f"all channels failed: {last_error}"}}, None, None


def make_handler(cfg: dict, db):
    relay = Relay(cfg, db)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "api-watch-gw/0.1"

        def log_message(self, fmt, *args):
            pass

        def _body(self) -> bytes:
            if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
                chunks = []
                while True:
                    line = self.rfile.readline().strip()
                    size = int(line.split(b";")[0] or b"0", 16)
                    if size == 0:
                        self.rfile.readline()
                        break
                    chunks.append(self.rfile.read(size))
                    self.rfile.read(2)
                return b"".join(chunks)
            length = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(length) if length else b""

        def _handle(self):
            body = self._body()
            model = None
            if body:
                try:
                    model = (json.loads(body.decode("utf-8")) or {}).get("model")
                except Exception:
                    model = None
            status, resp, conn, chan = relay.forward(
                self.command, self.path, dict(self.headers.items()), body, model)
            if conn is None:  # 本地构造的错误响应
                data = json.dumps(resp, ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            try:
                self.send_response(resp.status)
                for k, v in resp.getheaders():
                    if k.lower() in ("content-length", "transfer-encoding", "connection"):
                        continue
                    self.send_header(k, v)
                self.send_header("X-ApiWatch-Channel", chan or "")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                read_chunk = getattr(resp, "read1", resp.read)
                while True:
                    chunk = read_chunk(4096)
                    if not chunk:
                        break
                    self.wfile.write(b"%X\r\n%s\r\n" % (len(chunk), chunk))
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            finally:
                conn.close()

        def do_POST(self):  # noqa: N802
            self._handle()

        def do_GET(self):  # noqa: N802
            self._handle()

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=None)
    args = parser.parse_args()
    cfg = core.load_config(args.config)
    db = core.ThreadDB()
    db.conn()  # 启动时先建一条连接，库有问题第一时间暴露在 stderr
    gw = cfg.get("gateway") or {}
    host, port = gw.get("host", "127.0.0.1"), int(gw.get("port", 8788))
    httpd = ThreadingHTTPServer((host, port), make_handler(cfg, db))
    # 常驻服务由 launchd 重定向 stdout 到文件：默认整块缓冲，启动行不落盘，
    # 看日志会误以为没起来（同 watchd 的启动行）。flush 一下。
    print(f"api-watch 网关: http://{host}:{port}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
