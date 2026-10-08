# Running the bridge as a systemd timer (no container)

Tested on AlmaLinux 10 with SELinux enforcing. Adjust paths and the user name
to taste; the defaults below are what the unit files expect.

```bash
# 1. Unprivileged service user and private state directory
useradd -r -s /sbin/nologin -d /var/lib/garmin-intervals-bridge -M gib
install -d -m 700 -o gib -g gib /var/lib/garmin-intervals-bridge

# 2. Virtualenv OUTSIDE any web document root (SELinux: systemd must be able to execute it)
python3 -m venv /opt/garmin-intervals-bridge/venv
/opt/garmin-intervals-bridge/venv/bin/pip install /path/to/garmin-intervals-bridge

# 3. Secrets, readable by the service user only
install -d -m 750 -o root -g gib /etc/garmin-intervals-bridge
umask 077; printf 'INTERVALS_API_KEY=%s\nINTERVALS_ATHLETE_ID=0\n' "$(read -rsp 'Intervals API key: ' k; echo "$k")" \
  > /etc/garmin-intervals-bridge/env
chown root:gib /etc/garmin-intervals-bridge/env; chmod 640 /etc/garmin-intervals-bridge/env

# 4. Garmin login once, interactively, as the service user (MFA prompt included; tokens only are stored)
sudo -u gib env HOME=/var/lib/garmin-intervals-bridge BRIDGE_DATA_DIR=/var/lib/garmin-intervals-bridge \
  GARMIN_TOKEN_DIR=/var/lib/garmin-intervals-bridge/tokens \
  /opt/garmin-intervals-bridge/venv/bin/garmin-intervals-bridge login

# 5. Units
cp deploy/systemd/garmin-intervals-bridge.{service,timer} /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now garmin-intervals-bridge.timer
systemctl start garmin-intervals-bridge.service     # first run now
journalctl -u garmin-intervals-bridge.service -o cat
```

The service runs in **dry-run mode** until you append `--apply` to `ExecStart`.
Read a few journals first: every run prints what it would write, per activity.
