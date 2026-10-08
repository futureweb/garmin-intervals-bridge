# Security and privacy

This project handles GPS tracks, detailed medical/fitness/health metrics, Garmin session tokens and the Intervals API key.

**Must**

- Never commit `.env`, `data/`, FIT exports, JSON snapshots, OAuth tokens, logs, or raw API responses.
- Restrict data/tokens folder permissions (0700) and backup encrypted at rest. FIT files and JSON (0600) contain personal information.
- Use a non-root Docker container; bind mount the private storage to `/data`.
- Use the Intervals API key only over HTTPS. Do not put it into CLI arguments or screenshots.
- Never paste Garmin passwords, session tokens, Intervals API keys or private FITs into public GitHub issues.
- Run `login` once in a local interactive terminal and let Garmin session tokens refresh. Do not store Garmin account password in cron or compose environment variables.
- Keep remote writes behind explicit `--apply` and `--allow-activity-upload`; audit the difference before enabling them.
- Keep Garmin official wellness import if useful; only disable the overlapping *activity* import after proven testing.
- Do not auto-resubmit a pending FIT upload without checking Intervals for the activity. Upload requests may have succeeded even after a network timeout.

**Limitations**

- Garmin endpoints are unofficial/private, may change and may have access restrictions and anti-automation protection; honor their rules.
- FIT header validation is structural, not a cryptographic signature or complete FIT semantic/CRC validation. For deeper checks use `compare-fit` with `fitparse` and a verified Garmin web download.
- Existing wellness values are preserved, but concurrently changing server-side custom fields between GET and PUT can theoretically race; this is not a transactional multiwriter merge.
- Automatic retry is limited to GET requests; a failed write might still have taken effect, and the app avoids replaying activity uploads automatically.
- Synthetic tests cannot prove Garmin session tokens or Intervals API data shapes; see README acceptance checklist before release.

**Reporting vulnerabilities**

If publishing a public GitHub repo, set up private security advisories / vulnerability reporting first. Avoid posting tokens or customer data.
