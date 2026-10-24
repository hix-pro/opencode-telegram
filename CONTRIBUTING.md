# Contributing

Thanks for helping improve the bridge.

## Project goals

This is a small, security-sensitive project: the same process can run shell
commands and read files on the host, so changes are reviewed with that blast
radius in mind. When changing it, the goals are:

- keep the bot responsive while long agent turns run
- protect the local machine with sensible security defaults
- make installation and operation easy on a normal Linux/systemd setup
- keep changes covered by tests and documented clearly

## Where security concerns go

Please report vulnerabilities through
[GitHub private vulnerability reporting](https://github.com/hix-pro/opencode-telegram/security/advisories/new),
not a public issue. See [SECURITY.md](SECURITY.md) for the threat model.

## Development setup

Requirements: Python 3.10+, `requests`, and an OpenCode server for live tests.

```bash
git clone https://github.com/hix-pro/opencode-telegram.git
cd opencode-telegram
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
pip install requests==2.34.2 pytest==9.1.1 ruff==0.16.0 mypy==2.3.1 \
  types-requests bandit==1.9.4
git config core.hooksPath hooks
cp .env.example .env
chmod 600 .env
```

Fill in the two required values for a local run. Never commit `.env`, state,
session, log, attachment, or local backup files; the hook is the second line of
defence after `.gitignore`.

Run the service in the foreground:

```bash
set -a && . ./.env && set +a
python3 bridge.py
```

## Required checks

Run these before opening a pull request:

```bash
pytest                       # hermetic suite
pytest -m live               # needs a running server and credentials
ruff check .                 # lint
ruff format --check .        # formatting
mypy                         # typed shipped surface
bandit -q -c pyproject.toml -r bridge.py bridgekit scripts
```

The pre-commit hook runs all four release gates. Do not use `--no-verify`; if a
gate is wrong, fix the gate or the code in the same change.

## What makes a good change

- Add a focused test for every behavior change, especially authorization,
  path confinement, command guarding, and Telegram wire shapes.
- Keep the default secure. A new escape hatch must be opt-in, documented, and
  called out in the changelog.
- Do not add a dependency for something the standard library does well. If a
  dependency is necessary, explain why and update the CI pins.
- Preserve the public identity and history cleanup rules: commits use the
  project identity, and private files do not enter the repository.
- If behavior is user-visible, update README, `docs/CONFIGURATION.md`,
  `.env.example`, and `CHANGELOG.md` together.
- If behavior changes any security boundary, say so plainly in the PR and
  release note, even if the direction is to weaken a guard.

## Live tests

The hermetic suite fakes the HTTP API and cannot catch protocol drift. The live
suite uses a real server, creates a throwaway session in a temporary directory,
and deletes it. See [docs/LIVE_TESTING.md](docs/LIVE_TESTING.md) for the
optional self-hosted scheduled job.

## Pull requests

Keep one concern per pull request. Fill in the template, include the test output
you ran, and call out any behavior change that could surprise an existing user.
Squash merges are used on `master`; the commit message should read as a
release-note line.
