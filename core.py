#!/usr/bin/env python3
"""api-watch 公共模块：配置、数据库、上游请求。"""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import threading
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(ROOT, "config.json")
DB_PATH = os.path.join(ROOT, "data", "watch.db")


def load_dotenv(path: str | None = None) -> None:
    """读取项目内 .env（KEY=VALUE，# 注释），不覆盖已有环境变量。"""
    path = path or os.path.join(ROOT, ".env")
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_config(path: str | None = None) -> dict:
    path = path or DEFAULT_CONFIG
    load_dotenv()
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.setdefault("probe", {})
    cfg["probe"].setdefault("interval_sec", 300)
    cfg["probe"].setdefault("capability_interval_sec", 21600)
    cfg["probe"].setdefault("timeout_sec", 60)
    cfg["probe"].setdefault("cooldown_sec", 600)
    cfg.setdefault("channels", [])
    return cfg


def channel_key(ch: dict) -> str:
    env = ch.get("key_env")
    if env:
        return os.environ.get(env, "").strip()
    return (ch.get("key") or "").strip()


def init_db(path: str = DB_PATH) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    db = sqlite3.connect(path, check_same_thread=False, timeout=10)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS probes (
            ts REAL, channel_id TEXT, model TEXT, ok INTEGER,
            status INTEGER, latency_ms INTEGER, ttfb_ms INTEGER,
            error TEXT, in_tokens INTEGER, out_tokens INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_probes_ts ON probes(ts);
        CREATE TABLE IF NOT EXISTS caps (
            ts REAL, channel_id TEXT, probe TEXT, ok INTEGER, detail TEXT
        );
        CREATE TABLE IF NOT EXISTS events (
            ts REAL, level TEXT, channel_id TEXT, message TEXT
        );
        CREATE TABLE IF NOT EXISTS cooldown (
            channel_id TEXT PRIMARY KEY, until REAL, reason TEXT
        );
        CREATE TABLE IF NOT EXISTS relay_log (
            ts REAL, model TEXT, channel_id TEXT, ok INTEGER, status INTEGER,
            latency_ms INTEGER, fallback_from TEXT, error TEXT
        );
        """
    )
    db.commit()
    return db


class ThreadDB:
    """每线程一条 sqlite3 连接，调用方仍按 `db.execute(...)` 用。

    sqlite3 连接不能跨线程同时用：gateway 是每条 HTTP 连接一个线程，watchd 是采集线程
    加面板线程。共用一个连接时先报 "cannot commit transaction - SQL statements in
    progress"，接着连接状态彻底坏掉，写库抛 "database disk image is malformed"——
    gateway 会把上游已经生成完的回复直接丢掉，客户端只看到 ECONNRESET
    （2026-11-15 实测：Claude Code 一轮重试 11 次全被丢弃，额度照扣）。
    """

    def __init__(self, path: str = DB_PATH) -> None:
        self._path = path
        self._local = threading.local()

    def conn(self) -> sqlite3.Connection:
        db = getattr(self._local, "db", None)
        if db is None:
            db = init_db(self._path)
            self._local.db = db
        return db

    def execute(self, *args, **kwargs):
        return self.conn().execute(*args, **kwargs)

    def executemany(self, *args, **kwargs):
        return self.conn().executemany(*args, **kwargs)

    def executescript(self, *args, **kwargs):
        return self.conn().executescript(*args, **kwargs)

    def commit(self) -> None:
        self.conn().commit()

    def rollback(self) -> None:
        self.conn().rollback()

    def close(self) -> None:
        self.conn().close()


def log_event(db: sqlite3.Connection, level: str, channel_id: str, message: str) -> None:
    db.execute(
        "INSERT INTO events(ts, level, channel_id, message) VALUES (?,?,?,?)",
        (time.time(), level, channel_id, message[:500]),
    )
    db.commit()


# 上游前面挂着 Cloudflare：默认 Python-urllib 的 UA 会被判成坏客户端，直接回 `error code: 1010`
# （2026-11-15 实测：curl 能过，urllib 不能）。UA 可在渠道里用 user_agent 覆盖。
DEFAULT_UA = "curl/8.7.1"


def _headers(ch: dict, key: str) -> dict:
    ua = ch.get("user_agent") or DEFAULT_UA
    if ch.get("api") == "anthropic":
        return {
            "x-api-key": key,
            "anthropic-version": ch.get("anthropic_version", "2023-06-01"),
            "content-type": "application/json",
            "user-agent": ua,
        }
    return {"Authorization": f"Bearer {key}", "content-type": "application/json",
            "user-agent": ua}


def endpoint(ch: dict, path: str) -> str:
    base = ch["base_url"].rstrip("/")
    return base + path


def chat_path(ch: dict) -> str:
    return "/v1/messages" if ch.get("api") == "anthropic" else "/chat/completions"


def build_body(ch: dict, model: str, messages: list, max_tokens: int = 16) -> dict:
    if ch.get("api") == "anthropic":
        system = [m["content"] for m in messages if m["role"] == "system"]
        rest = [m for m in messages if m["role"] != "system"]
        body = {"model": model, "max_tokens": max_tokens, "messages": rest}
        if system:
            body["system"] = "\n".join(system)
        return body
    return {"model": model, "max_tokens": max_tokens, "messages": messages, "stream": False}


def parse_reply(ch: dict, payload: dict) -> str:
    try:
        if ch.get("api") == "anthropic":
            parts = payload.get("content") or []
            return "".join(p.get("text", "") for p in parts if isinstance(p, dict))
        return payload["choices"][0]["message"].get("content") or ""
    except Exception:
        return ""


def parse_usage(ch: dict, payload: dict) -> tuple[int, int]:
    u = payload.get("usage") or {}
    if ch.get("api") == "anthropic":
        return int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0)
    return int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)


def request_json(url: str, headers: dict, body: dict | None = None,
                 timeout: float = 60) -> dict:
    """返回 {status, payload, text, latency_ms, error}；不抛异常。"""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if data else "GET")
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read() if hasattr(e, "read") else b""
        status = e.code
    except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
        return {"status": 0, "payload": {}, "text": str(e), "error": str(e),
                "latency_ms": int((time.time() - started) * 1000)}
    except Exception as e:  # noqa: BLE001
        return {"status": 0, "payload": {}, "text": str(e), "error": str(e),
                "latency_ms": int((time.time() - started) * 1000)}
    latency = int((time.time() - started) * 1000)
    text = raw.decode("utf-8", "replace")
    try:
        payload = json.loads(text)
    except Exception:
        payload = {}
    err = None
    if status >= 400:
        err = (payload.get("error") or {}).get("message") if isinstance(payload, dict) else None
        err = err or text[:300]
    return {"status": status, "payload": payload, "text": text,
            "latency_ms": latency, "error": err}


def cooldown_until(db: sqlite3.Connection, channel_id: str) -> float:
    row = db.execute("SELECT until FROM cooldown WHERE channel_id=?", (channel_id,)).fetchone()
    return row[0] if row else 0.0


def set_cooldown(db: sqlite3.Connection, channel_id: str, seconds: int, reason: str) -> None:
    db.execute(
        "INSERT INTO cooldown(channel_id, until, reason) VALUES (?,?,?) "
        "ON CONFLICT(channel_id) DO UPDATE SET until=excluded.until, reason=excluded.reason",
        (channel_id, time.time() + seconds, reason[:300]),
    )
    db.commit()


def clear_cooldown(db: sqlite3.Connection, channel_id: str) -> None:
    db.execute("DELETE FROM cooldown WHERE channel_id=?", (channel_id,))
    db.commit()
