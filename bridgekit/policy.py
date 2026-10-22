"""Permission policy and command guard.

Two independent controls:

*Approval policy* decides whether a pending permission request is answered
automatically or handed to a human. Modes are ``ask`` (default), ``read``,
and ``all``. ``read`` auto-approves operations that cannot modify anything,
which removes most of the interruption noise from read-heavy work.

*Command guard* is the safety rail underneath that. It never approves
anything; it force-rejects commands matching a deny-list regardless of the
policy in force. Auto-approve without a guard is how a model eventually runs
``rm -rf`` on a typo, so the guard is the reason ``all`` mode is offered at
all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

ASK = "ask"
READ = "read"
ALL = "all"
MODES = (ASK, READ, ALL)

# Commands that are destructive enough to never auto-approve.
DEFAULT_DENY = (
    r"\brm\s+(-[a-zA-Z]*\s+)*-[a-zA-Z]*[rf]",
    r"\bmkfs\b",
    r"\bdd\s+.*\bof=/dev/",
    r":\(\)\s*\{.*\};:",
    r"\bshutdown\b|\breboot\b|\bhalt\b|\bpoweroff\b",
    r"\bchmod\s+(-[a-zA-Z]+\s+)*777\s+/",
    r"\bchown\b.*\s/(\s|$)",
    r"\bcurl\b[^|]*\|\s*(ba)?sh",
    r"\bwget\b[^|]*\|\s*(ba)?sh",
    r"\bsudo\b",
    r"\bgit\s+push\s+.*--force\b",
    r"\bgit\s+reset\s+--hard\b",
    r"\btruncate\b.*-s\s*0",
    r">\s*/dev/(sd|nvme|hd)",
)

# Actions the server reports that are safe to approve in `read` mode.
READ_ONLY_ACTIONS = {
    "read",
    "glob",
    "grep",
    "list",
    "ls",
    "webfetch",
    "todowrite",
    "websearch",
    "listdir",
}


@dataclass
class Policy:
    """Approval decisions for pending permission requests.

    Checked in order:

    1. The command guard, which force-rejects destructive commands. It outranks
       everything, including an ``allow bash`` rule.
    2. Per-action rules, which override the coarse mode for one action.
    3. The mode, consulted only for actions with no rule.
    """

    mode: str = ASK
    deny: list = field(default_factory=lambda: list(DEFAULT_DENY))
    # chat_id -> {action -> decision}
    rules: dict = field(default_factory=dict)
    _compiled: list = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        self._compile()

    def _compile(self) -> None:
        # Keep patterns and regexes paired: skipping a bad pattern used to
        # shift the two lists, so /guard test named the wrong rule.
        self._compiled = []
        for pattern in self.deny:
            try:
                self._compiled.append((pattern, re.compile(pattern, re.IGNORECASE)))
            except re.error:
                continue

    def set_mode(self, mode: str) -> None:
        self.mode = mode if mode in MODES else ASK

    def add_deny(self, pattern: str) -> None:
        """Validate before storing: an invalid regex protected nothing while
        reporting success."""
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"not a valid regular expression: {exc}") from exc
        if pattern in self.deny:
            return
        self.deny.append(pattern)
        self._compile()

    def remove_deny(self, pattern: str) -> None:
        self.deny = [p for p in self.deny if p != pattern]
        self._compile()

    # ------------------------------------------------------------- rules

    def rules_for(self, chat_id: str) -> dict:
        return self.rules.setdefault(chat_id, {})

    def set_rule(self, chat_id: str, action: str, decision: str) -> None:
        self.rules_for(chat_id)[action.lower()] = decision

    def clear_rule(self, chat_id: str, action: str) -> None:
        self.rules_for(chat_id).pop(action.lower(), None)

    def clear_rules(self, chat_id: str) -> None:
        self.rules.setdefault(chat_id, {}).clear()

    def is_read_only(self, action: str) -> bool:
        return (action or "").lower() in READ_ONLY_ACTIONS

    def blocked_command(self, command: str) -> str | None:
        """Return the pattern that rejects this command, if any."""
        text = " ".join(str(command).split())
        for pattern, regex in self._compiled:
            if regex.search(text):
                return pattern
        return None

    def decide(self, request: dict, chat_id: str | None = None) -> str | None:
        """Answer a permission request automatically, or None to ask a human."""
        action = (request.get("action") or "").lower()
        resources = request.get("resources") or []

        # The guard outranks everything, including an explicit allow rule.
        if action in ("bash", "shell", "execute", "run"):
            for resource in resources:
                if self.blocked_command(resource):
                    return "reject"

        rules = self.rules.get(chat_id or "", {})
        for key in (action, "*"):
            if key in rules:
                decision = rules[key]
                # An "ask" rule means the human decides: no auto-answer.
                return None if decision == "ask" else decision

        if self.mode == ALL:
            writes = ("edit", "write", "patch", "bash", "shell")
            return "always" if action in writes else "once"
        if self.mode == READ and self.is_read_only(action):
            return "once"
        return None

    def snapshot(self, chat_id: str = "") -> dict:
        """Serialisable state, for persistence across restarts."""
        data: dict[str, object] = {"mode": self.mode, "deny": list(self.deny)}
        if chat_id:
            data["rules"] = dict(self.rules_for(chat_id))
        else:
            data["rules"] = {c: dict(r) for c, r in self.rules.items() if r}
        return data

    def restore(self, data: dict) -> None:
        """Reload previously persisted settings."""
        if not data:
            return
        mode = data.get("mode")
        if mode in MODES:
            self.mode = mode
        deny = data.get("deny")
        # An empty list is a real setting ("/policy deny clear"), not bad
        # data: restore it, or cleared entries resurrect after a restart.
        if isinstance(deny, list):
            self.deny = list(deny)
            self._compile()
        rules = data.get("rules")
        if isinstance(rules, dict):
            for chat_id, entries in rules.items():
                if isinstance(entries, dict) and entries:
                    self.rules[chat_id] = dict(entries)

    def describe(self) -> str:
        if self.mode == ASK:
            return "ask for everything"
        if self.mode == READ:
            return "auto-approve read-only, ask otherwise"
        return "auto-approve all except denied commands"

    def deny_summary(self, limit: int = 6) -> str:
        return ", ".join(self.deny[:limit]) + ("..." if len(self.deny) > limit else "")
