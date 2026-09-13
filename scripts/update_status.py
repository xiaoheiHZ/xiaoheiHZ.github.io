# -*- coding: utf-8 -*-
"""采集 Minecraft 服务器实时状态 -> status.json（GitHub Actions 定时运行）"""
import json
import os
from datetime import datetime, timezone, timedelta

ADDR = "mc.suixingyu.top:51145"
OUT = "status.json"
CN = timezone(timedelta(hours=8))

result = {
    "updated": datetime.now(CN).isoformat(timespec="seconds"),
    "online": False,
    "latency": None,
    "players": {"online": 0, "max": 0, "list": []},
    "motd": "",
    "version": "",
}

# 保留本地脚本维护的字段（各服人数/排行榜等）
if os.path.exists(OUT):
    try:
        old = json.load(open(OUT, encoding="utf-8"))
        for k in ("servers", "leaderboard", "processes", "logs", "heat", "pvp_kb", "note"):
            if k in old:
                result[k] = old[k]
    except Exception:
        pass

try:
    from mcstatus import JavaServer
    server = JavaServer.lookup(ADDR, timeout=15)
    status = server.status()
    result["online"] = True
    result["latency"] = round(status.latency)
    result["players"] = {
        "online": status.players.online,
        "max": status.players.max,
        "list": [p.name for p in (status.players.sample or [])],
    }
    try:
        motd = status.description
        if isinstance(motd, dict):
            motd = motd.get("text", "") or str(motd)
        result["motd"] = str(motd)[:200]
    except Exception:
        pass
    try:
        result["version"] = status.version.name
    except Exception:
        pass
except Exception as e:
    result["error"] = str(e)[:300]

json.dump(result, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print(json.dumps(result, ensure_ascii=False, indent=2))
