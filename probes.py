#!/usr/bin/env python3
"""探针：健康检查、能力验真、余额查询。

能力验真的答案由本地暴力计算得出，不依赖官方基线，
答错即说明后台模型能力明显弱于目标型号。
"""
from __future__ import annotations

import re
import sqlite3
import time

import core


def _sieve(n: int) -> list[bool]:
    flags = [True] * (n + 1)
    flags[0] = flags[1] = False
    for i in range(2, int(n ** 0.5) + 1):
        if flags[i]:
            for j in range(i * i, n + 1, i):
                flags[j] = False
    return flags


def _twin_pairs(lo: int, hi: int) -> int:
    flags = _sieve(hi)
    primes = [n for n in range(lo, hi + 1) if flags[n]]
    return sum(1 for a, b in zip(primes, primes[1:]) if b - a == 2)


def _count_with_digit(lo: int, hi: int, digit: str) -> int:
    return sum(1 for n in range(lo, hi + 1) if digit in str(n))


def _sum_divisible(upper: int, a: int, b: int) -> int:
    return sum(n for n in range(1, upper + 1) if n % a == 0 and n % b != 0)


PROBES = [
    {
        "id": "twin_primes_200",
        "prompt": "统计 2 到 200（含两端）之间共有多少对孪生素数（相差 2 的素数对）。只输出一个整数，不要解释。",
        "expected": _twin_pairs(2, 200),
    },
    {
        "id": "digit7_999",
        "prompt": "1 到 999（含两端）之间，有多少个整数的十进制写法里包含数字 7？只输出一个整数，不要解释。",
        "expected": _count_with_digit(1, 999, "7"),
    },
    {
        "id": "sum_div_3_not_5",
        "prompt": "求 1 到 100 之间所有能被 3 整除、但不能被 5 整除的整数之和。只输出一个整数，不要解释。",
        "expected": _sum_divisible(100, 3, 5),
    },
]


def extract_int(text: str) -> int | None:
    nums = re.findall(r"-?\d[\d,]*", text or "")
    if not nums:
        return None
    raw = nums[-1].replace(",", "")
    try:
        return int(raw)
    except ValueError:
        return None


def health_probe(db: sqlite3.Connection, ch: dict, timeout: float = 60) -> dict:
    key = core.channel_key(ch)
    model = (ch.get("models") or [""])[0]
    if not key:
        result = {"ok": False, "error": "缺少 API Key", "status": 0, "latency_ms": 0}
    elif not model:
        result = {"ok": False, "error": "未配置模型", "status": 0, "latency_ms": 0}
    else:
        body = core.build_body(ch, model, [{"role": "user", "content": "ping"}], max_tokens=4)
        resp = core.request_json(core.endpoint(ch, core.chat_path(ch)),
                                 core._headers(ch, key), body, timeout=timeout)
        reply = core.parse_reply(ch, resp["payload"])
        # 探针只要 4 个 token：thinking 型模型（deepseek-flash）会全部用在 reasoning 上，
        # 正文为空但渠道是健康的。anthropic 侧有 content 块就算通，避免误报「空回复」。
        has_block = bool((resp.get("payload") or {}).get("content")) if ch.get("api") == "anthropic" else False
        ok = resp["status"] == 200 and (bool(reply.strip()) or has_block)
        result = {
            "ok": ok, "status": resp["status"], "latency_ms": resp["latency_ms"],
            "error": None if ok else (resp.get("error") or "空回复"),
            "usage": core.parse_usage(ch, resp["payload"]),
        }
    in_tok, out_tok = result.get("usage") or (0, 0)
    db.execute(
        "INSERT INTO probes(ts, channel_id, model, ok, status, latency_ms, ttfb_ms,"
        " error, in_tokens, out_tokens) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (time.time(), ch["id"], model, 1 if result["ok"] else 0, result.get("status", 0),
         result.get("latency_ms", 0), result.get("ttfb_ms"), result.get("error"),
         in_tok, out_tok),
    )
    db.commit()
    if result["ok"]:
        core.clear_cooldown(db, ch["id"])
    else:
        core.log_event(db, "error", ch["id"], f"健康检查失败：{result.get('error')}")
    return result


def capability_probe(db: sqlite3.Connection, ch: dict, timeout: float = 120) -> None:
    key = core.channel_key(ch)
    model = (ch.get("models") or [""])[0]
    if not key or not model:
        return
    for probe in PROBES:
        body = core.build_body(
            ch, model, [{"role": "user", "content": probe["prompt"]}], max_tokens=1200
        )
        resp = core.request_json(core.endpoint(ch, core.chat_path(ch)),
                                 core._headers(ch, key), body, timeout=timeout)
        reply = core.parse_reply(ch, resp["payload"])
        got = extract_int(reply)
        ok = resp["status"] == 200 and got == probe["expected"]
        detail = f"expected={probe['expected']} got={got} status={resp['status']}"
        db.execute(
            "INSERT INTO caps(ts, channel_id, probe, ok, detail) VALUES (?,?,?,?,?)",
            (time.time(), ch["id"], probe["id"], 1 if ok else 0, detail),
        )
        db.commit()
        if resp["status"] == 200 and not ok:
            core.log_event(db, "warn", ch["id"],
                           f"能力探针疑似降智：{probe['id']} {detail}")
        elif resp["status"] != 200:
            core.log_event(db, "warn", ch["id"],
                           f"能力探针失败：{probe['id']} {resp.get('error')}")


def balance_probe(db: sqlite3.Connection, ch: dict, timeout: float = 20) -> dict | None:
    """best-effort：one-api / new-api 常见余额端点，读不到就返回 None。"""
    key = core.channel_key(ch)
    if not key:
        return None
    base = ch["base_url"].rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    headers = {"Authorization": f"Bearer {key}"}
    for path in ("/v1/dashboard/billing/subscription", "/api/user/self"):
        resp = core.request_json(base + path, headers, None, timeout=timeout)
        if resp["status"] == 200 and resp["payload"]:
            payload = resp["payload"]
            data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
            quota = data.get("hard_limit_usd") or data.get("quota") or data.get("total_available")
            if quota is not None:
                return {"raw": data, "quota": quota, "source": path}
    return None
