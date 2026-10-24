# The bridge's web page

See [the README](../README.md#the-web-page) for opening the page locally or on your network.

## Check now from a phone

Set `BRIDGE_UI_TRIGGER_TOKEN` to a random secret of at least 24 characters without spaces.
You can generate one with `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
Keep it in your private settings file or container environment, then restart the web page.
On an editable web Settings page, the trigger token is a password field: blank keeps the
saved value; **Remove the Check now trigger token** disables the endpoint. The bridge never
shows the saved token or returns it in a response.

The trigger works wherever the web page is reachable. Local `ui` listens on 127.0.0.1, so it
can be called only from that computer. For a phone, use the password-protected network page
with `BRIDGE_UI_PASSWORD`: the systemd UI unit, Docker/NAS, or `ui --host` on your network.
Use your private network or an HTTPS reverse proxy, as for the web page itself.
Home Assistant does not need this setting: its page uses ingress only, and this endpoint
returns 404 there.

Send a POST with the separate trigger token in the Authorization header. For example,
with the secret already in your shell's `BRIDGE_UI_TRIGGER_TOKEN` variable:

```sh
curl -i -X POST 'http://bridge.local:8765/trigger/poll-now' \
  -H "Authorization: Bearer ${BRIDGE_UI_TRIGGER_TOKEN}"
```

No cookie, web password, session or form CSRF token is needed. Query-string tokens are not
accepted; the request body is ignored. A successful request returns `202` and a short plain
text line: `looking for new activities now` (when served by `run`) or
`looking for new activities within a minute` (the separate UI with a scheduled bridge).
The bridge must already be running or scheduled; the trigger does not start it.

It does exactly what the **Check now** button does: wakes the current run or next scheduled
watch and keeps the fast pace for the next hour within your configured polling hours. Outside
those hours it asks for one look. It still respects provider rate-limit pauses. Requests within
30 seconds return `202 already looking` without extending the schedule. Missing/wrong tokens
return `401` with `WWW-Authenticate: Bearer`; no configured token returns `404`.

In Apple Shortcuts:

1. Create a shortcut and add **Get Contents of URL** with your bridge URL ending in
   `/trigger/poll-now`.
2. Expand its options, choose method **POST**, and add a header named **Authorization** with
   the value `Bearer YOUR_TRIGGER_TOKEN` (one space after `Bearer`). No request body is needed.
3. Run the shortcut while the phone can reach your bridge. The response says when it will look.
4. Optionally run this shortcut from a personal automation, such as **Arrive** at home after
   an activity. Keep the token private if you share or export the shortcut.
