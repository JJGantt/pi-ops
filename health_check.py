#!/usr/bin/env python3
"""
Pi health checker — runs 8 checks, batches alerts to Telegram.
No pip dependencies; stdlib only.

Usage:
    python3 health_check.py              # Normal: alert on WARN/CRIT only
    python3 health_check.py --verbose    # Print all results
    python3 health_check.py --dry-run    # Show what would alert, don't send
"""

import argparse
import json
import os
import subprocess
import time
import urllib.request
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG = json.loads((SCRIPT_DIR / "config.json").read_text())

METRICS_FILE = Path.home() / "data" / "metrics" / "health_metrics.jsonl"

# Load Telegram creds from .env
def load_env():
    env_file = SCRIPT_DIR / ".env"
    vals = {}
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                vals[k.strip()] = v.strip()
    return vals

ENV = load_env()
BOT_TOKEN = ENV.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = ENV.get("TELEGRAM_CHAT_ID", "")

# ---------------------------------------------------------------------------
# Severity levels
# ---------------------------------------------------------------------------
OK, WARN, CRIT = "OK", "WARN", "CRIT"

def result(name, severity, detail=""):
    return {"name": name, "severity": severity, "detail": detail}

# ---------------------------------------------------------------------------
# Check implementations
# ---------------------------------------------------------------------------

def check_services():
    """Check systemctl is-active for each configured service."""
    results = []
    for svc in CONFIG["services"]:
        try:
            out = subprocess.run(
                ["systemctl", "is-active", svc],
                capture_output=True, text=True, timeout=5
            )
            state = out.stdout.strip()
        except Exception as e:
            state = f"error: {e}"

        if state == "active":
            results.append(result(f"svc:{svc}", OK, "active"))
        elif state == "activating":
            results.append(result(f"svc:{svc}", WARN, "activating"))
        else:
            results.append(result(f"svc:{svc}", CRIT, state))
    return results


def check_memory():
    """Check available memory from /proc/meminfo."""
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            parts = line.split()
            if parts[0].rstrip(":") in ("MemTotal", "MemAvailable"):
                info[parts[0].rstrip(":")] = int(parts[1])  # kB

    avail_mb = info.get("MemAvailable", 0) / 1024
    total_mb = info.get("MemTotal", 0) / 1024
    detail = f"{avail_mb:.0f} MB free / {total_mb:.0f} MB total"

    t = CONFIG["thresholds"]
    if avail_mb < t["mem_crit_mb"]:
        return [result("memory", CRIT, detail)]
    elif avail_mb < t["mem_warn_mb"]:
        return [result("memory", WARN, detail)]
    return [result("memory", OK, detail)]


def check_swap():
    """Check swap usage from /proc/meminfo."""
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            parts = line.split()
            if parts[0].rstrip(":") in ("SwapTotal", "SwapFree"):
                info[parts[0].rstrip(":")] = int(parts[1])  # kB

    total = info.get("SwapTotal", 0)
    free = info.get("SwapFree", 0)
    if total == 0:
        return [result("swap", OK, "no swap configured")]

    used_pct = ((total - free) / total) * 100
    detail = f"{used_pct:.1f}% used ({(total - free) / 1024:.0f} / {total / 1024:.0f} MB)"

    t = CONFIG["thresholds"]
    if used_pct > t["swap_crit_pct"]:
        return [result("swap", CRIT, detail)]
    elif used_pct > t["swap_warn_pct"]:
        return [result("swap", WARN, detail)]
    return [result("swap", OK, detail)]


def check_disk():
    """Check root filesystem usage."""
    st = os.statvfs("/")
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    used_pct = ((total - free) / total) * 100
    detail = f"{used_pct:.1f}% used ({(total - free) / (1024**3):.1f} / {total / (1024**3):.1f} GB)"

    t = CONFIG["thresholds"]
    if used_pct > t["disk_crit_pct"]:
        return [result("disk", CRIT, detail)]
    elif used_pct > t["disk_warn_pct"]:
        return [result("disk", WARN, detail)]
    return [result("disk", OK, detail)]


def check_temperature():
    """Check CPU temperature via vcgencmd."""
    try:
        out = subprocess.run(
            ["vcgencmd", "measure_temp"],
            capture_output=True, text=True, timeout=5
        )
        # Output: temp=52.0'C
        temp_str = out.stdout.strip().split("=")[1].rstrip("'C")
        temp = float(temp_str)
    except Exception as e:
        return [result("temperature", WARN, f"cannot read: {e}")]

    detail = f"{temp:.1f}°C"
    t = CONFIG["thresholds"]
    if temp > t["temp_crit_c"]:
        return [result("temperature", CRIT, detail)]
    elif temp > t["temp_warn_c"]:
        return [result("temperature", WARN, detail)]
    return [result("temperature", OK, detail)]


def check_log_sizes():
    """Check log file sizes."""
    results = []
    t = CONFIG["thresholds"]
    for log_path in CONFIG["log_files"]:
        p = Path(log_path)
        if not p.exists():
            results.append(result(f"log:{p.name}", OK, "file missing (ok)"))
            continue
        size_mb = p.stat().st_size / (1024 * 1024)
        detail = f"{size_mb:.1f} MB"
        if size_mb > t["log_crit_mb"]:
            results.append(result(f"log:{p.name}", CRIT, detail))
        elif size_mb > t["log_warn_mb"]:
            results.append(result(f"log:{p.name}", WARN, detail))
        else:
            results.append(result(f"log:{p.name}", OK, detail))
    return results


def check_ports():
    """Check that expected ports are listening."""
    results = []
    try:
        out = subprocess.run(
            ["ss", "-tlnp"],
            capture_output=True, text=True, timeout=5
        )
        listening = out.stdout
    except Exception as e:
        return [result("ports", WARN, f"cannot run ss: {e}")]

    for port in CONFIG["ports"]:
        if f":{port}" in listening:
            results.append(result(f"port:{port}", OK, "listening"))
        else:
            results.append(result(f"port:{port}", CRIT, "not listening"))
    return results


def check_claude_sessions():
    """Check total size of ~/.claude/projects/ directory."""
    projects_dir = Path.home() / ".claude" / "projects"
    if not projects_dir.exists():
        return [result("claude-sessions", OK, "no projects dir")]

    total_bytes = sum(f.stat().st_size for f in projects_dir.rglob("*") if f.is_file())
    total_mb = total_bytes / (1024 * 1024)
    detail = f"{total_mb:.0f} MB"

    t = CONFIG["thresholds"]
    if total_mb > t["claude_sessions_crit_mb"]:
        return [result("claude-sessions", CRIT, detail)]
    elif total_mb > t["claude_sessions_warn_mb"]:
        return [result("claude-sessions", WARN, detail)]
    return [result("claude-sessions", OK, detail)]


def check_sync_freshness():
    """Check how recently sync timers last fired."""
    results = []
    t = CONFIG["thresholds"]
    for timer in CONFIG["sync_timers"]:
        try:
            out = subprocess.run(
                ["systemctl", "show", timer, "--property=LastTriggerUSec"],
                capture_output=True, text=True, timeout=5
            )
            # LastTriggerUSec=Sun 2026-03-01 19:59:32 EST
            val = out.stdout.strip().split("=", 1)[1]
            if not val or val == "n/a":
                results.append(result(f"sync:{timer}", WARN, "never triggered"))
                continue

            # Parse the timestamp
            # systemd format: "Day YYYY-MM-DD HH:MM:SS TZ"
            parts = val.split()
            # parts: ['Sun', '2026-03-01', '19:59:32', 'EST']
            dt_str = f"{parts[1]} {parts[2]}"
            last = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
            age_min = (datetime.now() - last).total_seconds() / 60
            detail = f"{age_min:.0f} min ago"

            if age_min > t["sync_stale_min"]:
                results.append(result(f"sync:{timer}", WARN, detail))
            else:
                results.append(result(f"sync:{timer}", OK, detail))
        except Exception as e:
            results.append(result(f"sync:{timer}", WARN, f"error: {e}"))
    return results


# ---------------------------------------------------------------------------
# Metrics logging
# ---------------------------------------------------------------------------

def extract_value(check):
    """Pull numeric/boolean values out of a check result's detail string."""
    name = check["name"]
    detail = check["detail"]
    try:
        if name == "memory":
            # "3453 MB free / 4050 MB total"
            parts = detail.split()
            return {"mem_free_mb": float(parts[0]), "mem_total_mb": float(parts[4])}
        if name == "swap":
            # "3.2% used (65 / 2048 MB)"
            pct = float(detail.split("%")[0])
            inner = detail.split("(")[1].split(")")[0].split("/")
            return {"swap_pct": pct, "swap_used_mb": float(inner[0]), "swap_total_mb": float(inner[1])}
        if name == "disk":
            # "44.0% used (12.2 / 27.6 GB)"
            pct = float(detail.split("%")[0])
            inner = detail.split("(")[1].split(")")[0].split("/")
            return {"disk_pct": pct, "disk_used_gb": float(inner[0]), "disk_total_gb": float(inner[1])}
        if name == "temperature":
            # "57.1°C"
            return {"temp_c": float(detail.replace("°C", ""))}
        if name == "claude-sessions":
            # "49 MB"
            return {"size_mb": float(detail.split()[0])}
        if name.startswith("log:"):
            # "9.9 MB" or "file missing (ok)"
            if "missing" in detail:
                return {"size_mb": 0.0}
            return {"size_mb": float(detail.split()[0])}
        if name.startswith("svc:"):
            return {"active": detail == "active"}
        if name.startswith("port:"):
            return {"listening": detail == "listening"}
        if name.startswith("sync:"):
            # "12 min ago" or "never triggered"
            if "never" in detail:
                return {"age_min": None}
            return {"age_min": float(detail.split()[0])}
    except Exception:
        pass
    return {}


def log_metrics(results):
    """Append one JSONL record with all check results + extracted values."""
    METRICS_FILE.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "checks": [
            {**r, **extract_value(r)}
            for r in results
        ],
    }
    with METRICS_FILE.open("a") as f:
        f.write(json.dumps(record) + "\n")


# ---------------------------------------------------------------------------
# Aggregate runner (importable by mcp_server.py)
# ---------------------------------------------------------------------------

def run_all_checks():
    """Run every health check and return a flat list of result dicts."""
    all_results = []
    all_results.extend(check_services())
    all_results.extend(check_memory())
    all_results.extend(check_swap())
    all_results.extend(check_disk())
    all_results.extend(check_temperature())
    all_results.extend(check_log_sizes())
    all_results.extend(check_ports())
    all_results.extend(check_claude_sessions())
    all_results.extend(check_sync_freshness())
    return all_results


# ---------------------------------------------------------------------------
# Alerting
# ---------------------------------------------------------------------------

def load_state():
    state_path = Path(CONFIG["state_file"])
    if state_path.exists():
        return json.loads(state_path.read_text())
    return {}


def save_state(state):
    state_path = Path(CONFIG["state_file"])
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2) + "\n")


def should_alert(name, severity, state):
    """Decide if we should send an alert for this check result.

    Returns (should_send, reason) tuple.
    - New problem: always alert
    - Severity escalation (WARN→CRIT): alert (bypass cooldown)
    - Same severity, within cooldown: suppress
    - Recovery (was WARN/CRIT, now OK): alert (recovery notification)
    """
    now = time.time()
    cooldown_sec = CONFIG["cooldown_minutes"] * 60

    if severity == OK:
        if name in state:
            # Recovery — was previously alerting
            return True, "recovery"
        return False, "ok"

    prev = state.get(name)
    if prev is None:
        # New problem
        return True, "new"

    prev_severity = prev.get("severity")
    prev_time = prev.get("time", 0)

    # Severity escalation bypasses cooldown
    if prev_severity == WARN and severity == CRIT:
        return True, "escalation"

    # Within cooldown
    if now - prev_time < cooldown_sec:
        return False, "cooldown"

    return True, "repeat"


def send_telegram(message):
    if not BOT_TOKEN or not CHAT_ID:
        print("WARNING: Telegram credentials not configured in .env")
        return False
    try:
        body = json.dumps({
            "chat_id": int(CHAT_ID),
            "text": message,
            "parse_mode": "HTML",
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception as e:
        print(f"Telegram send failed: {e}")
        return False


def format_alert_message(alerts, recoveries):
    """Build a single batched Telegram message."""
    lines = []

    if alerts:
        lines.append("<b>Pi Health Alert</b>")
        lines.append("")
        for a in alerts:
            icon = "\u26a0\ufe0f" if a["severity"] == WARN else "\u274c"
            lines.append(f"{icon} <b>[{a['severity']}]</b> {a['name']}: {a['detail']}")

    if recoveries:
        if lines:
            lines.append("")
        lines.append("<b>Recovered</b>")
        lines.append("")
        for r in recoveries:
            lines.append(f"\u2705 {r['name']}: {r['detail']}")

    lines.append(f"\n<i>{datetime.now().strftime('%Y-%m-%d %H:%M')}</i>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Pi health checker")
    parser.add_argument("--verbose", "-v", action="store_true", help="Print all check results")
    parser.add_argument("--dry-run", "-n", action="store_true", help="Show alerts without sending")
    args = parser.parse_args()

    all_results = run_all_checks()
    log_metrics(all_results)

    if args.verbose:
        print(f"Pi Health Check — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("-" * 60)
        for r in all_results:
            icon = {"OK": "\u2705", "WARN": "\u26a0\ufe0f ", "CRIT": "\u274c"}[r["severity"]]
            print(f"  {icon} [{r['severity']:4s}] {r['name']:35s} {r['detail']}")
        print("-" * 60)

    # Load state, determine what to alert on
    state = load_state()
    alerts = []
    recoveries = []

    for r in all_results:
        should, reason = should_alert(r["name"], r["severity"], state)
        if should:
            if reason == "recovery":
                recoveries.append(r)
            else:
                alerts.append(r)
                if args.verbose:
                    print(f"  ALERT ({reason}): {r['name']} [{r['severity']}]")
        elif args.verbose and r["severity"] != OK:
            print(f"  suppressed ({reason}): {r['name']} [{r['severity']}]")

    # Update state (skip on dry-run so it doesn't affect cooldowns)
    if not args.dry_run:
        now = time.time()
        for r in all_results:
            if r["severity"] != OK:
                state[r["name"]] = {"severity": r["severity"], "time": now}
            elif r["name"] in state:
                del state[r["name"]]
        save_state(state)

    # Send batched alert
    if alerts or recoveries:
        message = format_alert_message(alerts, recoveries)
        if args.dry_run:
            print("\nDRY RUN — would send:")
            print(message)
        else:
            send_telegram(message)
            if args.verbose:
                print(f"\nSent Telegram alert ({len(alerts)} alerts, {len(recoveries)} recoveries)")
    elif args.verbose:
        print("\nNo alerts to send.")


if __name__ == "__main__":
    main()
