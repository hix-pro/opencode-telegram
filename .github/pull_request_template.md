## Summary

What changed, and why?

## Tests

- [ ] `ruff check .`
- [ ] `ruff format --check .`
- [ ] `mypy`
- [ ] `bandit -q -c pyproject.toml -r bridge.py bridgekit scripts`
- [ ] `pytest`
- [ ] `pytest -m live` (if a server is available)

## Security and compatibility

- [ ] No `.env`, state, session, log, attachment, or credential file is included.
- [ ] New behavior is documented in README and, if configurable, in
      `docs/CONFIGURATION.md` and `.env.example`.
- [ ] This does not weaken authorization or path confinement by default.
- [ ] I did not use `--no-verify`.

## Release note

One or two sentences for `CHANGELOG.md`. Mention any user-visible default
change explicitly.
