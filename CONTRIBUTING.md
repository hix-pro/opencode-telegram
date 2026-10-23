# Contributing to OpenCode Telegram Bridge

Thanks for helping improve the project.

## Project goals

This project is a Telegram bridge for a local OpenCode server. The main goals are:

- keep the bot responsive while long agent turns run
- protect the local machine with sensible security defaults
- make installation and operation easy on a normal Linux desktop/systemd setup
- keep changes covered by tests and documented clearly

## Local setup

1. Clone the repo.
2. Create a virtual environment.
3. Install runtime and dev dependencies:

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install requests pytest ruff mypy bandit types-requests
```

## Required checks

Run these before opening a PR:

```bash
python3 -m pytest
python3 -m ruff check .
python3 -m mypy
python3 -m bandit -q -c pyproject.toml -r bridge.py bridgekit scripts
```

For live-server validation only:

```bash
python3 -m pytest -m live
```

## Documentation standards

- Update the README when behavior changes.
- Keep configuration examples in sync with `.env.example`.
- Prefer small, focused PRs.
- Document any new security boundaries or default behaviors.

## Security expectations

This project talks to Telegram and executes commands through the local OpenCode server. Please treat security as a first-class concern:

- do not add hard-coded secrets
- do not weaken the allow-list or guard defaults without a clear reason
- disclose security concerns privately if needed
- note the impact of any new filesystem or command access in the PR

## Pull request checklist

- [ ] tests pass locally
- [ ] lint/type/security checks pass locally
- [ ] README or docs updated if behavior changed
- [ ] no secret values added
- [ ] the change is scoped and easy to review

## Good bug fixes and features

Prefer PRs that:

- improve reliability under long turns
- make Telegram behavior clearer and more robust
- improve startup, recovery, or permissions logic
- strengthen safety and documentation

If you are unsure whether a change is a good fit, open a discussion first.
