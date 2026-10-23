"""Command handler modules.

``bridgekit.commands`` imports this package at the bottom of its module so the
shared registry, context and decorator exist before handlers register
themselves. Each module keeps ``from .. import commands as core`` and reads
constants through it, which preserves the historic test surface
(``bridgekit.commands.SHELL_PROBE``) without import cycles.
"""

from __future__ import annotations

from . import files, inspect, operations, permissions, policy, sessions  # noqa: F401
