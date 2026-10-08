# Contributing

Thanks for helping. Three things keep this project trustworthy:

1. **No personal data, ever.** No FIT files, Garmin responses, wellness
   JSON, tokens or API keys in issues, pull requests or test fixtures.
   Tests use synthetic data only (see `tests/`, e.g. `build_fit()`).
2. **Dry run stays the default.** Anything that writes to Intervals must sit
   behind `--apply`, must never delete, and must never overwrite a value
   that may be real data (see the rules in `docs/PLAN.md`).
3. **Verify API shapes against the real thing**, not against assumptions.
   If you add a field or stream mapping, say in the PR which live response
   or chart definition you checked it against.

Workflow: `pip install -e '.[dev]'`, then `ruff check src tests && pytest -q`
must pass. Keep commits focused and explain the *why* in the message.

Found a security problem? Use GitHub's private vulnerability reporting on
this repository rather than a public issue.
