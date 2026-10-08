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
cp deploy/systemd/garmin-intervals-bridge*.{service,timer} /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now garmin-intervals-bridge.timer          # full run every 30 min (safety net)
systemctl enable --now garmin-intervals-bridge-watch.timer    # one-minute poll, Garmin only on new activities
systemctl start garmin-intervals-bridge.service     # first run now
journalctl -u garmin-intervals-bridge.service -o cat
```

The service runs in **dry-run mode** until you append `--apply` to `ExecStart`.
Read a few journals first: every run prints what it would write, per activity.

## Two timers, one purpose

- `garmin-intervals-bridge-watch` runs every minute, asks Intervals for the
  last two days (one ~450-byte request) and enriches only activities it sees
  for the first time. Latency after the official import: about a minute.
- `garmin-intervals-bridge` runs every 30 minutes over the last four days and
  catches anything the watcher missed (an import that lagged, a failed download
  that is due for its retry).

Both are dry runs until `--apply` is appended to their `ExecStart`.

## Backfilling the past

```bash
# as the service user, with the same environment as the units
garmin-intervals-bridge backfill --scope activities --from 2026-03-01            # dry run
garmin-intervals-bridge backfill --scope activities --from 2026-03-01 --apply
garmin-intervals-bridge backfill --scope wellness   --from 2026-01-01 --pause 3 --apply
```

Each wellness day costs about 22 Garmin requests, each activity one original
download; the pause keeps a long backfill polite. Runs are resumable: days and
activities already handled are skipped.

## Failure alerts

Both service units carry `OnFailure=garmin-intervals-bridge-alert@%n.service`.
That unit mails the failed unit's status and last 40 journal lines through
the local `sendmail`, at most once every six hours (a Garmin outage would
otherwise produce a mail per minute from the watcher). Configure the
addresses once:

```bash
printf 'ALERT_TO=you@example.org\nALERT_FROM=bridge@example.org\n' > /etc/garmin-intervals-bridge/alert.env
chmod 640 /etc/garmin-intervals-bridge/alert.env
```

Typical reasons for a failure: Garmin tokens expired (run `login` again as
the service user), Intervals API key revoked, no network.

## Daily health probe

`garmin-intervals-bridge health` logs in to Garmin with the stored tokens and
makes one small Intervals request. It exits 2 only when a service has been
failing for longer than `BRIDGE_STALE_HOURS` (default 24), so a single bad
night is a warning in the journal while an expired Garmin session becomes
one alert mail a day through the same `OnFailure=` hook. The timer runs it
once a day:

```bash
systemctl enable --now garmin-intervals-bridge-health.timer
```
