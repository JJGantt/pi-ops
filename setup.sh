#!/usr/bin/env bash
#
# setup.sh — Deploy pi-ops configs and enable the health check timer.
# Run on Pi: cd ~/pi-ops && sudo bash setup.sh
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== pi-ops setup ==="

# 1. Create state directory
echo "Creating state directory..."
mkdir -p /home/jaredgantt/data/ops
chown jaredgantt:jaredgantt /home/jaredgantt/data/ops

# 2. Install logrotate config
echo "Installing logrotate config..."
cp "$REPO_DIR/logrotate/claude-services" /etc/logrotate.d/claude-services
chmod 644 /etc/logrotate.d/claude-services
echo "  -> /etc/logrotate.d/claude-services"

# 3. Install journald retention config
echo "Installing journald retention config..."
mkdir -p /etc/systemd/journald.conf.d
cp "$REPO_DIR/journald/99-retention.conf" /etc/systemd/journald.conf.d/99-retention.conf
chmod 644 /etc/systemd/journald.conf.d/99-retention.conf
echo "  -> /etc/systemd/journald.conf.d/99-retention.conf"
echo "  Restarting journald..."
systemctl restart systemd-journald

# 4. Install systemd service and timer
echo "Installing systemd units..."
cp "$REPO_DIR/systemd/pi-ops-health.service" /etc/systemd/system/
cp "$REPO_DIR/systemd/pi-ops-health.timer" /etc/systemd/system/
chmod 644 /etc/systemd/system/pi-ops-health.service
chmod 644 /etc/systemd/system/pi-ops-health.timer
systemctl daemon-reload
systemctl enable pi-ops-health.timer
systemctl start pi-ops-health.timer
echo "  -> pi-ops-health.timer enabled and started"

# 5. Verify
echo ""
echo "=== Verification ==="
echo "Timer status:"
systemctl list-timers | grep pi-ops || echo "  (timer may not have triggered yet)"
echo ""
echo "Logrotate dry run:"
logrotate --debug /etc/logrotate.d/claude-services 2>&1 | tail -5
echo ""
echo "Journald config:"
journalctl --disk-usage
echo ""
echo "=== Setup complete ==="
echo "Run 'python3 $REPO_DIR/health_check.py --verbose --dry-run' to test."
