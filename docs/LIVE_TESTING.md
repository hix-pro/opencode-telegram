# Live testing

The hermetic suite fakes the HTTP API, so it cannot detect a changed content
type, an ignored query parameter, or a missing event type. The live suite
talks to a real OpenCode server and is opt-in:

```bash
python3 -m pytest -m live
```

It creates a throwaway session in a temporary directory, exercises prompt,
streaming, shell, filesystem, concurrency and abort behaviour, then deletes
the session.

## Prerequisites

- A running OpenCode server at `OPENCODE_URL` (default
  `http://127.0.0.1:49374`).
- `OPENCODE_PASSWORD` in the environment, or the server password in
  `~/.config/opencode/service.json` (the same fallback the bridge uses).
- Python 3.10+ and the test dependencies.

If the server is unreachable or no credential is available, the suite skips
instead of failing. A green run with every test skipped is not a live result;
check the summary line.

## Scheduled run on a self-hosted runner

The `Live tests` workflow is disabled by default. It is safe to add to a public
repository because it never triggers on pull requests and the job is skipped
unless a repository variable is set.

1. Register a runner for this repository with the labels
   `self-hosted`, `linux`, and `opencode-live`. Run it as the account that can
   reach the OpenCode server and read its credentials. Do not give it more
   access than that account already has.
2. Confirm the runner can run `python3 -m pytest -m live` from a checkout.
3. Enable the schedule:

   ```bash
   gh variable set LIVE_TESTS_ENABLED --body true -R hix-pro/opencode-telegram
   ```

4. Trigger one run with **Actions → Live tests → Run workflow**, or wait for
   Monday 04:17 UTC.
5. Disable it with `gh variable delete LIVE_TESTS_ENABLED`.

The runner executes workflow code from the default branch. Keep the
`opencode-live` label dedicated to this repository; a self-hosted runner is
equivalent to code execution on the host.
