# -*- coding: utf-8 -*-
"""本地采集：各服人数 + PvP 排行榜 -> 合并进 status.json（可选上传 GitHub）

用法:
  python collect_local.py          # 采集并更新本地 status.json
  python collect_local.py upload   # 采集并上传到 GitHub（需 GH_TOKEN 环境变量）
"""
import json
import os
import re
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATUS = os.path.join(BASE, "status.json")
STATS_YML = r"D:\pvp server\plugins\StarPvP\stats.yml"
RCON_ANY = r"D:\mc-velocity\rcon_any.py"
CN = timezone(timedelta(hours=8))

LOG_FILES = [
    ("生存服", r"D:\minecraft server\logs\latest.log"),
    ("登入服", r"D:\login server\logs\latest.log"),
    ("起床战争", r"D:\bedwars server\logs\latest.log"),
    ("PvP 竞技", r"D:\pvp server\logs\latest.log"),
]

HEAT_FILE = os.path.join(BASE, "heat_history.json")

USERCACHE_FILES = [
    r"D:\pvp server\usercache.json",
    r"D:\minecraft server\usercache.json",
    r"D:\bedwars server\usercache.json",
    r"D:\login server\usercache.json",
]

SERVERS = [
    ("登入服", 25575, "suxing2026"),
    ("生存服", 25576, "suxing2026"),
    ("起床战争", 25578, "suixingyu"),
    ("PvP 竞技", 25579, "suixingpvp"),
]

PATTERNS = [
    r"are\s+(\d+)\s+of",              # There are X of a max
    r"(\d+)\s+of\s+a\s+max",          # X of a max
    r"\u5f53\u524d\u6709\s*\u00a7?c?(\d+)",   # 当前有 X
    r"\u6709\s*\u00a7?c?(\d+)\s*\u00a7?6?\u4f4d",  # 有 X 位
    r"\u5728\u7ebf[:\uff1a]?\s*(\d+)",         # 在线: X
]


def rcon_count(port, password):
    """返回在线人数；失败返回 None（支持英文与中文 list 输出）"""
    try:
        out = subprocess.run(
            ["python", RCON_ANY, str(port), password, "list"],
            capture_output=True, timeout=15)
        raw = out.stdout
        for enc in ("utf-8", "gbk"):
            try:
                text = raw.decode(enc)
            except Exception:
                continue
            for pat in PATTERNS:
                m = re.search(pat, text)
                if m:
                    return int(m.group(1))
    except Exception:
        pass
    return None


def collect_processes(server_players):
    """采集各服 Java 进程的 CPU/内存（PowerShell）"""
    import os as _os
    cores = _os.cpu_count() or 4
    ps1 = ("Get-CimInstance Win32_Process -Filter \"name like '%java%'\" | "
           "Select-Object ProcessId,WorkingSetSize,CommandLine | ConvertTo-Json -Compress")
    ps2 = ("Get-Process java -ErrorAction SilentlyContinue | "
           "Select-Object Id,CPU | ConvertTo-Json -Compress")
    try:
        r1 = subprocess.run(["powershell", "-NoProfile", "-Command", ps1], capture_output=True, timeout=30)
        procs = json.loads(r1.stdout.decode("utf-8", "replace") or "[]")
        if isinstance(procs, dict):
            procs = [procs]
        # CPU 第一次采样
        c1 = subprocess.run(["powershell", "-NoProfile", "-Command", ps2], capture_output=True, timeout=30)
        cpu1 = json.loads(c1.stdout.decode("utf-8", "replace") or "[]")
        if isinstance(cpu1, dict):
            cpu1 = [cpu1]
        time.sleep(1.0)
        c2 = subprocess.run(["powershell", "-NoProfile", "-Command", ps2], capture_output=True, timeout=30)
        cpu2 = json.loads(c2.stdout.decode("utf-8", "replace") or "[]")
        if isinstance(cpu2, dict):
            cpu2 = [cpu2]
        cpu_map = {}
        for a in cpu1:
            for b in cpu2:
                if a.get("Id") == b.get("Id"):
                    delta = (b.get("CPU") or 0) - (a.get("CPU") or 0)
                    cpu_map[a["Id"]] = max(0.0, delta / 1.0 / cores * 100.0)
        # 映射到服务
        def map_service(cmd):
            cmd = cmd or ""
            if "velocity" in cmd:
                return "Velocity 代理"
            if "paper-1.21.11" in cmd:
                return "登入服"
            if "-Xmx4G" in cmd:
                return "生存服"
            if "-Xmx2G" in cmd and "paper-26.2" in cmd:
                return "起床战争"
            if "-Xmx1G" in cmd and "paper-26.2" in cmd:
                return "PvP 竞技"
            return None
        result = []
        for pr in procs:
            name = map_service(pr.get("CommandLine", ""))
            if not name:
                continue
            pid = pr.get("ProcessId")
            mem = int((pr.get("WorkingSetSize") or 0) / 1024 / 1024)
            players = server_players.get(name, 0)
            act = max(1, min(10, players + 2))
            result.append({
                "n": name, "st": "run",
                "cpu": round(cpu_map.get(pid, 0.0), 1),
                "mem": mem, "act": act,
            })
        order = ["Velocity 代理", "登入服", "生存服", "起床战争", "PvP 竞技"]
        result.sort(key=lambda r: order.index(r["n"]) if r["n"] in order else 99)
        return result
    except Exception as e:
        print("processes error:", e)
        return []


def mc_cmd(port, password, cmd):
    """执行任意 RCON 命令，返回文本"""
    try:
        out = subprocess.run(
            ["python", RCON_ANY, str(port), password, cmd],
            capture_output=True, timeout=15)
        raw = out.stdout
        for enc in ("utf-8", "gbk"):
            try:
                return raw.decode(enc)
            except Exception:
                continue
        return raw.decode("utf-8", "replace")
    except Exception:
        return ""


def collect_logs(limit=22):
    """采集各服日志中的真实事件（加入/离开/死亡/成就/聊天/启动）"""
    events = []
    noise = ("[RCON", "Thread RCON", "issued server command", "Grim", "pausing",
             "checkForUpdates", "\tat ", "Server empty", "Starting minecraft server",
             "Preparing", "Loaded ", "Advancement", "Saving", "Stopping")
    for srv, path in LOG_FILES:
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()[-500:]
        except Exception:
            continue
        for line in lines:
            if "]: " not in line:
                continue
            mt = re.search(r"\[(\d{2}:\d{2}:\d{2})\]", line)
            tstr = mt.group(1) if mt else ""
            msg = line.split("]: ", 1)[1].strip()
            if any(x in line for x in noise):
                continue
            tag, cls = None, ""
            if "joined the game" in msg:
                tag, cls = "[JOIN]", "ok"
            elif "left the game" in msg:
                tag, cls = "[QUIT]", "warn"
            elif any(x in msg for x in ("slain by", "was killed", "burned to death", "drowned",
                                        "fell from", "blew up", "shot by", "withered away")):
                tag, cls = "[DEATH]", "warn"
            elif "has made the advancement" in msg or "has completed the challenge" in msg:
                tag, cls = "[成就]", "ok"
            elif re.match(r"^\[?Not Secure\]?\s*<", msg) and ">" in msg:
                tag, cls = "[CHAT]", "info"
            elif "Done (" in msg:
                tag, cls = "[启动]", "ok"
            elif "RCON" in msg:
                continue
            if tag:
                events.append({"t": tstr, "tag": tag, "msg": (srv + ": " + msg)[:90], "cls": cls})
    events.sort(key=lambda e: e.get("t", ""))
    return events[-limit:]


def collect_heat():
    """采集主服在线玩家坐标 -> 30 天热力历史 -> Top8 热区"""
    text = mc_cmd(25576, "suxing2026", "minecraft:list")
    names = []
    m = re.search(r"online:\s*(.+)$", text, re.M)
    if m:
        names = [n.strip() for n in m.group(1).split(",") if n.strip()]

    points = []
    for n in names[:12]:
        if not re.match(r"^\w{3,16}$", n):
            continue
        r = mc_cmd(25576, "suxing2026", "data get entity " + n + " Pos")
        mm = re.search(r"\[([-\d.]+)d?,\s*([-\d.]+)d?,\s*([-\d.]+)d?\]", r)
        if mm:
            points.append({"name": n, "x": round(float(mm.group(1))), "z": round(float(mm.group(3)))})

    hist = []
    if os.path.exists(HEAT_FILE):
        try:
            hist = json.load(open(HEAT_FILE, encoding="utf-8"))
        except Exception:
            hist = []
    ts = time.time()
    for pt in points:
        hist.append({"x": pt["x"], "z": pt["z"], "t": ts})
    cutoff = ts - 30 * 86400
    hist = [h for h in hist if h.get("t", 0) > cutoff][-50000:]
    try:
        json.dump(hist, open(HEAT_FILE, "w", encoding="utf-8"), ensure_ascii=False)
    except Exception:
        pass

    from collections import Counter
    grid = Counter()
    for h in hist:
        grid[(int(h["x"] // 512) * 512, int(h["z"] // 512) * 512)] += 1
    total = sum(grid.values()) or 1
    zones = []
    for (gx, gz), cnt in grid.most_common(8):
        zones.append({
            "name": "X%d / Z%d" % (gx, gz),
            "x": int(gx), "z": int(gz),
            "count": int(cnt),
            "pct": round(cnt * 100.0 / total, 1),
        })
    return {"zones": zones, "points": points, "total": len(hist)}


def tcp_latency(host, port, timeout=5):
    try:
        start = time.time()
        s = socket.create_connection((host, port), timeout=timeout)
        ms = round((time.time() - start) * 1000)
        s.close()
        return ms
    except Exception:
        return None


def load_names():
    """从各服 usercache.json 读取 UUID -> 玩家名"""
    names = {}
    for f in USERCACHE_FILES:
        if not os.path.exists(f):
            continue
        try:
            arr = json.load(open(f, encoding="utf-8"))
            for e in arr:
                if isinstance(e, dict):
                    u = str(e.get("uuid", "")).lower()
                    n = e.get("name")
                    if u and n:
                        names[u] = n
        except Exception:
            pass
    return names


def load_leaderboard():
    """从 stats.yml 读取排行榜"""
    if not os.path.exists(STATS_YML):
        return []
    try:
        entries = {}
        cur = None
        with open(STATS_YML, encoding="utf-8", errors="replace") as f:
            for raw in f:
                line = raw.rstrip("\n")
                if not line.strip() or line.startswith("#"):
                    continue
                if not line.startswith(" ") and line.rstrip().endswith(":"):
                    cur = line.rstrip()[:-1].strip()
                    entries.setdefault(cur, {})
                elif line.startswith("  ") and cur:
                    k, _, v = line.strip().partition(":")
                    try:
                        entries[cur][k.strip()] = int(v.strip())
                    except ValueError:
                        pass
        names = load_names()
        rows = []
        for uuid, d in entries.items():
            kills = d.get("kills", 0)
            wins = d.get("wins", 0)
            if kills == 0 and wins == 0:
                continue
            rows.append({"uuid": uuid, "name": names.get(uuid.lower(), ""),
                         "kills": kills, "wins": wins,
                         "losses": d.get("losses", 0), "deaths": d.get("deaths", 0)})
        rows.sort(key=lambda r: (r["kills"], r["wins"]), reverse=True)
        return rows[:10]
    except Exception as e:
        print("leaderboard error:", e)
        return []


def main():
    data = {}
    if os.path.exists(STATUS):
        try:
            data = json.load(open(STATUS, encoding="utf-8"))
        except Exception:
            pass

    total = 0
    servers = []
    for name, port, pw in SERVERS:
        cnt = rcon_count(port, pw)
        online = cnt is not None
        n = cnt or 0
        total += n
        servers.append({"name": name, "online": online, "players": n})
    data["servers"] = servers

    lat = tcp_latency("mc.suixingyu.top", 51145)
    data["latency"] = lat
    data["online"] = lat is not None
    old_players = data.get("players", {})
    data["players"] = {
        "online": total,
        "max": old_players.get("max", 100),
        "list": old_players.get("list", []),
    }
    data["leaderboard"] = load_leaderboard()
    players_by_name = {s["name"]: s["players"] for s in servers}
    # Velocity 的玩家数 = 全部之和
    players_by_name["Velocity 代理"] = total
    data["processes"] = collect_processes(players_by_name)
    data["logs"] = collect_logs()
    data["heat"] = collect_heat()
    data["updated"] = datetime.now(CN).isoformat(timespec="seconds")

    json.dump(data, open(STATUS, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print("servers:", [(s["name"], s["online"], s["players"]) for s in servers])
    print("latency:", lat, "| leaderboard:", len(data["leaderboard"]))

    if len(sys.argv) > 1 and sys.argv[1] == "upload":
        upload()


def upload():
    token = os.environ.get("GH_TOKEN", "").strip()
    if not token:
        print("!! GH_TOKEN not set, skip upload")
        return
    import base64
    import urllib.request
    with open(STATUS, "rb") as f:
        content = base64.b64encode(f.read()).decode()
    body = json.dumps({"message": "chore: update status (local)", "content": content}).encode()
    req = urllib.request.Request(
        "https://api.github.com/repos/xiaoheihz/xiaoheihz.github.io/contents/status.json",
        data=body, method="PUT")
    req.add_header("Authorization", "token " + token)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "suixingyu-status")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            print("GitHub upload OK:", resp.status)
    except Exception as e:
        print("upload failed:", e)


if __name__ == "__main__":
    main()
