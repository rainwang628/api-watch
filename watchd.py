#!/usr/bin/env python3
"""api-watch 监控端：定时探针 + 本地面板（手机可访问）。

用法：
    python3 watchd.py            # 默认读 config.json
    python3 watchd.py --once     # 只跑一轮探针后退出
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import statistics
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import core
import probes

BALANCE_CACHE: dict[str, dict] = {}
BALANCE_TS: dict[str, float] = {}
LOCK = threading.Lock()


def collect(cfg: dict, db) -> None:
    timeout = cfg["probe"]["timeout_sec"]
    cap_interval = cfg["probe"]["capability_interval_sec"]
    for ch in cfg["channels"]:
        try:
            probes.health_probe(db, ch, timeout=timeout)
            last_cap = db.execute(
                "SELECT MAX(ts) FROM caps WHERE channel_id=?", (ch["id"],)
            ).fetchone()[0]
            if not last_cap or time.time() - last_cap > cap_interval:
                probes.capability_probe(db, ch, timeout=max(timeout, 120))
            if time.time() - BALANCE_TS.get(ch["id"], 0) > 1800:
                info = probes.balance_probe(db, ch)
                with LOCK:
                    BALANCE_TS[ch["id"]] = time.time()
                    if info:
                        BALANCE_CACHE[ch["id"]] = info
        except Exception as e:  # noqa: BLE001
            core.log_event(db, "error", ch.get("id", "?"), f"采集异常：{e}")


def in_quiet(now, quiet) -> bool:
    """quiet = [起, 止) 的本地小时区间；支持跨午夜（如 [23, 7]）。"""
    start, end = quiet
    hour = now.hour
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def seconds_until(hour: int) -> float:
    now = datetime.datetime.now()
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += datetime.timedelta(days=1)
    return (target - now).total_seconds()


def last_usage_ts(db) -> float:
    """最近一次经网关的使用时间；探针只在有人用的时候才有意义。"""
    row = db.execute("SELECT MAX(ts) FROM relay_log").fetchone()
    return float(row[0] or 0)


def scheduler(cfg: dict, db) -> None:
    probe = cfg["probe"]
    interval = float(probe.get("interval_sec") or 300)
    quiet = probe.get("quiet_hours") or []
    idle_stop = float(probe.get("idle_stop_sec") or 0)
    idle_check = float(probe.get("idle_check_sec") or 300)
    paused = None
    while True:
        # 静默时段不探针（探针在 AIGoCode 这类付费渠道上每次都要真金白银）；
        # 每 10 分钟醒一次看一眼时间，防止长时间 sleep 撞上系统休眠/改时。
        if quiet and in_quiet(datetime.datetime.now(), quiet):
            time.sleep(max(60.0, min(seconds_until(int(quiet[1])), 600.0)))
            continue
        # 没人用就不探：探针要花真钱，空闲期的降级等下次使用时再发现。
        idle_for = time.time() - last_usage_ts(db)
        if idle_stop > 0 and idle_for > idle_stop:
            if paused is not True:
                core.log_event(db, "info", "-", "已 %.1f 小时没有经网关的请求，暂停探针" % (idle_for / 3600))
                paused = True
            time.sleep(idle_check)
            continue
        if paused is True:
            core.log_event(db, "info", "-", "检测到新的中继请求，恢复探针")
            paused = False
        collect(cfg, db)
        time.sleep(interval)


def summarize(cfg: dict, db) -> dict:
    now = time.time()
    channels = []
    for ch in cfg["channels"]:
        cid = ch["id"]
        row = db.execute(
            "SELECT ts, ok, status, latency_ms, error FROM probes WHERE channel_id=?"
            " ORDER BY ts DESC LIMIT 1", (cid,)).fetchone()
        recent = db.execute(
            "SELECT ok, latency_ms FROM probes WHERE channel_id=? AND ts>?",
            (cid, now - 86400)).fetchall()
        latencies = [r[1] for r in recent if r[0] and r[1] is not None]
        ok_rate = (sum(1 for r in recent if r[0]) / len(recent) * 100) if recent else None
        caps = {}
        for probe_id in [p["id"] for p in probes.PROBES]:
            crow = db.execute(
                "SELECT ts, ok, detail FROM caps WHERE channel_id=? AND probe=?"
                " ORDER BY ts DESC LIMIT 1", (cid, probe_id)).fetchone()
            caps[probe_id] = {"ts": crow[0], "ok": bool(crow[1]),
                              "detail": crow[2]} if crow else None
        cd = core.cooldown_until(db, cid)
        events = db.execute(
            "SELECT ts, level, message FROM events WHERE channel_id=?"
            " ORDER BY ts DESC LIMIT 5", (cid,)).fetchall()
        cap_ok = [c["ok"] for c in caps.values() if c]
        if cd > now:
            status = "down"
        elif row and row[1]:
            status = "degraded" if cap_ok and not all(cap_ok) else "ok"
        elif row:
            status = "down"
        else:
            status = "unknown"
        with LOCK:
            bal = BALANCE_CACHE.get(cid)
        channels.append({
            "id": cid,
            "name": ch.get("name", cid),
            "api": ch.get("api", "openai"),
            "models": ch.get("models", []),
            "priority": ch.get("priority", 1),
            "status": status,
            "last_ts": row[0] if row else None,
            "last_ok": bool(row[1]) if row else None,
            "last_error": row[4] if row else None,
            "latency_ms": row[3] if row else None,
            "p50_ms": int(statistics.median(latencies)) if latencies else None,
            "ok_rate_24h": round(ok_rate, 1) if ok_rate is not None else None,
            "cooldown_until": cd if cd > now else None,
            "caps": caps,
            "balance": bal,
            "events": [{"ts": e[0], "level": e[1], "message": e[2]} for e in events],
        })
    recent_events = db.execute(
        "SELECT ts, level, channel_id, message FROM events ORDER BY ts DESC LIMIT 30"
    ).fetchall()
    relay = db.execute(
        "SELECT ts, model, channel_id, ok, status, latency_ms, fallback_from, error"
        " FROM relay_log ORDER BY ts DESC LIMIT 20").fetchall()
    return {
        "generated_at": now,
        "channels": channels,
        "events": [{"ts": e[0], "level": e[1], "channel": e[2], "message": e[3]}
                   for e in recent_events],
        "relay": [{"ts": r[0], "model": r[1], "channel": r[2], "ok": bool(r[3]),
                   "status": r[4], "latency_ms": r[5], "fallback_from": r[6],
                   "error": r[7]} for r in relay],
    }


def make_handler(cfg: dict, db):
    panel_path = os.path.join(core.ROOT, "panel", "index.html")
    token = (cfg.get("panel") or {}).get("token") or ""

    class Handler(BaseHTTPRequestHandler):
        server_version = "api-watch/0.1"

        def log_message(self, fmt, *args):  # 静音
            pass

        def _authorized(self, query: dict) -> bool:
            if not token:
                return True
            given = (query.get("token") or [""])[0] or self.headers.get("X-Panel-Token", "")
            return given == token

        def do_GET(self):  # noqa: N802
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            if parsed.path in ("/", "/index.html"):
                with open(panel_path, "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if parsed.path == "/api/summary":
                if not self._authorized(query):
                    self.send_response(401)
                    self.end_headers()
                    self.wfile.write(b'{"error":"unauthorized"}')
                    return
                body = json.dumps(summarize(cfg, db), ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(404)
            self.end_headers()

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=None)
    parser.add_argument("--once", action="store_true", help="只跑一轮探针")
    args = parser.parse_args()

    cfg = core.load_config(args.config)
    db = core.ThreadDB()  # 采集线程和面板线程各用各的连接，见 core.ThreadDB

    if args.once:
        collect(cfg, db)
        print(json.dumps(summarize(cfg, db), ensure_ascii=False, indent=2))
        return

    threading.Thread(target=scheduler, args=(cfg, db), daemon=True).start()
    panel = cfg.get("panel") or {}
    host = panel.get("host", "127.0.0.1")
    port = int(panel.get("port", 8787))
    httpd = ThreadingHTTPServer((host, port), make_handler(cfg, db))
    print(f"api-watch 面板: http://{host}:{port}/?token={panel.get('token', '')}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
