#!/usr/bin/env python3
"""
Pi Ops MCP server — exposes system diagnostic tools.

Tools:
  - get_system_status: Run all health checks (memory, swap, disk, temp, services, etc.)
  - get_top_processes: Top N processes by memory or CPU
  - get_service_logs: Recent journalctl output for a monitored service
  - get_alert_history: Current alerts from state.json with duration
  - get_service_list: All monitored services with status and uptime
"""

import json
import subprocess
import time
from datetime import datetime
from pathlib import Path

from mcp.server import Server
import mcp.server.stdio
import mcp.types as types

from health_check import run_all_checks, CONFIG, load_state

server = Server("pi-ops")

# Allowlist for service names (prevents injection via arbitrary service args)
_ALLOWED_SERVICES = set(CONFIG["services"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _format_check_results(results: list) -> str:
    """Format health check results into a readable report."""
    lines = []
    crits = [r for r in results if r["severity"] == "CRIT"]
    warns = [r for r in results if r["severity"] == "WARN"]
    oks = [r for r in results if r["severity"] == "OK"]

    if crits:
        lines.append("CRITICAL:")
        for r in crits:
            lines.append(f"  X {r['name']}: {r['detail']}")
    if warns:
        lines.append("WARNING:")
        for r in warns:
            lines.append(f"  ! {r['name']}: {r['detail']}")
    if oks:
        lines.append(f"OK: {len(oks)} checks passing")
        for r in oks:
            lines.append(f"  . {r['name']}: {r['detail']}")

    summary = f"{len(crits)} critical, {len(warns)} warning, {len(oks)} ok"
    lines.insert(0, f"System Status — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.insert(1, summary)
    lines.insert(2, "")
    return "\n".join(lines)


def _get_service_uptime(service: str) -> str:
    """Get uptime for a systemd service."""
    try:
        out = subprocess.run(
            ["systemctl", "show", service, "--property=ActiveEnterTimestamp"],
            capture_output=True, text=True, timeout=5
        )
        val = out.stdout.strip().split("=", 1)[1]
        if not val or val == "n/a":
            return "unknown"
        parts = val.split()
        dt_str = f"{parts[1]} {parts[2]}"
        started = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
        delta = datetime.now() - started
        days = delta.days
        hours, remainder = divmod(delta.seconds, 3600)
        minutes = remainder // 60
        if days > 0:
            return f"{days}d {hours}h {minutes}m"
        elif hours > 0:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# MCP tool definitions
# ---------------------------------------------------------------------------

@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="get_system_status",
            description=(
                "Run all Pi health checks and return a full status report. "
                "Checks: memory, swap, disk, temperature, services, ports, "
                "log sizes, Claude session size, sync freshness. "
                "Start here for any system diagnostic question."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        types.Tool(
            name="get_top_processes",
            description=(
                "Get top N processes sorted by memory or CPU usage. "
                "Use when memory or swap is high to identify the culprit."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "sort_by": {
                        "type": "string",
                        "description": "'memory' (default) or 'cpu'.",
                        "enum": ["memory", "cpu"],
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Number of processes to return (default 10).",
                    },
                },
            },
        ),
        types.Tool(
            name="get_service_logs",
            description=(
                "Get recent log output for a monitored systemd service. "
                "Use when a service is down or misbehaving."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "service": {
                        "type": "string",
                        "description": f"Service name. Allowed: {', '.join(sorted(_ALLOWED_SERVICES))}",
                    },
                    "lines": {
                        "type": "integer",
                        "description": "Number of log lines to return (default 50, max 200).",
                    },
                    "since": {
                        "type": "string",
                        "description": "Time filter, e.g. '1h', '30m', '2h'. Default '1h'.",
                    },
                },
                "required": ["service"],
            },
        ),
        types.Tool(
            name="get_alert_history",
            description=(
                "Get current active alerts from state.json with how long each "
                "has been active. Shows what's currently broken and for how long."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        types.Tool(
            name="get_service_list",
            description=(
                "List all monitored services with their current status and uptime. "
                "Quick way to check if everything is running."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
    ]


# ---------------------------------------------------------------------------
# MCP tool handlers
# ---------------------------------------------------------------------------

@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:

    if name == "get_system_status":
        results = run_all_checks()
        return [types.TextContent(type="text", text=_format_check_results(results))]

    if name == "get_top_processes":
        sort_by = arguments.get("sort_by", "memory")
        limit = min(max(1, int(arguments.get("limit", 10))), 30)
        sort_key = "%mem" if sort_by == "memory" else "%cpu"
        try:
            out = subprocess.run(
                ["ps", "aux", "--sort", f"-{sort_key}"],
                capture_output=True, text=True, timeout=10
            )
            lines = out.stdout.strip().split("\n")
            # Header + top N
            header = lines[0]
            procs = lines[1:limit + 1]
            return [types.TextContent(
                type="text",
                text=f"Top {limit} by {sort_by}:\n\n{header}\n" + "\n".join(procs)
            )]
        except Exception as e:
            return [types.TextContent(type="text", text=f"Error running ps: {e}")]

    if name == "get_service_logs":
        service = arguments.get("service", "").strip()
        if service not in _ALLOWED_SERVICES:
            return [types.TextContent(
                type="text",
                text=f"Service '{service}' not in allowlist. Allowed: {', '.join(sorted(_ALLOWED_SERVICES))}"
            )]
        lines = min(max(1, int(arguments.get("lines", 50))), 200)
        since = arguments.get("since", "1h").strip()
        # Validate 'since' format: digits followed by h/m/s/d
        if not (since and since[-1] in "hmsd" and since[:-1].isdigit()):
            since = "1h"
        try:
            out = subprocess.run(
                ["journalctl", "-u", service, "-n", str(lines), "--since", f"-{since}", "--no-pager"],
                capture_output=True, text=True, timeout=15
            )
            log_text = out.stdout.strip() or "(no log output)"
            return [types.TextContent(
                type="text",
                text=f"Logs for {service} (last {since}, {lines} lines max):\n\n{log_text}"
            )]
        except Exception as e:
            return [types.TextContent(type="text", text=f"Error reading logs: {e}")]

    if name == "get_alert_history":
        state = load_state()
        if not state:
            return [types.TextContent(type="text", text="No active alerts. Everything is clean.")]
        now = time.time()
        lines = [f"Active alerts ({len(state)}):"]
        for check_name, info in sorted(state.items()):
            severity = info.get("severity", "?")
            alert_time = info.get("time", 0)
            age_min = (now - alert_time) / 60
            if age_min > 1440:
                age_str = f"{age_min / 1440:.1f} days"
            elif age_min > 60:
                age_str = f"{age_min / 60:.1f} hours"
            else:
                age_str = f"{age_min:.0f} min"
            lines.append(f"  [{severity}] {check_name} — active for {age_str}")
        return [types.TextContent(type="text", text="\n".join(lines))]

    if name == "get_service_list":
        lines = [f"Monitored services ({len(_ALLOWED_SERVICES)}):"]
        for svc in sorted(_ALLOWED_SERVICES):
            try:
                out = subprocess.run(
                    ["systemctl", "is-active", svc],
                    capture_output=True, text=True, timeout=5
                )
                status = out.stdout.strip()
            except Exception:
                status = "error"
            uptime = _get_service_uptime(svc) if status == "active" else "-"
            icon = "+" if status == "active" else "X"
            lines.append(f"  {icon} {svc}: {status} (uptime: {uptime})")
        return [types.TextContent(type="text", text="\n".join(lines))]

    return [types.TextContent(type="text", text=f"Unknown tool: {name}")]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
