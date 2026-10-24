"""Permission watcher, scheduler, and approval delivery."""

from __future__ import annotations

import logging
import time

from . import attachments, keyboards
from .api import ApiError
from .telegram import TelegramError

log = logging.getLogger("bridge.watchers")

# Pending requests cached for button presses.
MAX_CACHED_REQUESTS = 200

# Permission ids remembered per watcher loop.
MAX_SEEN_PERMISSIONS = 500

# An auto-decision that keeps failing is retried every tick; the chat hears
# about the first failure and then at most once per this interval.
AUTO_RETRY_NOTICE = 60.0

# How often the scheduler checks for due work.
SCHEDULE_TICK = 60


def schedule_loop(bridge) -> None:
    """Submit scheduled prompts through the normal queue.

    Deliberately simple: tick once a minute and hand anything due to the
    same queue a message would use, so scheduled work streams, can be
    aborted and asks for permissions exactly like everything else.
    """
    while not bridge.stop_event.wait(SCHEDULE_TICK):
        try:
            for schedule in bridge.schedules.due():
                log.info("Schedule %s due for chat %s", schedule.id, schedule.chat_id)
                if schedule.chat_id not in bridge.config.allowed_chats:
                    continue
                bridge.work.put((schedule.chat_id, schedule.prompt, None))
            bridge.state.save_schedules(bridge.schedules.to_list())
        except Exception:
            log.exception("Scheduler tick failed")


def permission_loop(bridge) -> None:
    """Watch for approval requests.

    The whole body is guarded: this thread also runs attachment pruning,
    so an unguarded exception here would silently take auto-approval,
    prompts and disk housekeeping down with it, with no symptom other
    than the features quietly going missing.
    """
    seen: list[str] = []
    last_prune = 0.0
    # When an auto-answer fails, the request is retried every tick until the
    # server or the transport cooperates; this remembers when the chat was
    # last told, so a long outage cannot spam one line per poll.
    nagged: dict[str, float] = {}
    while not bridge.stop_event.wait(bridge.config.permission_poll):
        try:
            bridge.health.permissions_alive = True

            now = time.monotonic()
            if now - last_prune > 3600:
                last_prune = now
                try:
                    attachments.prune(
                        bridge.config.attachments_dir,
                        max_age_hours=bridge.config.attachment_max_age_hours,
                    )
                except Exception:
                    log.debug("Attachment prune failed", exc_info=True)

            bridge._prune_seen(seen)

            # The request ids still pending at the end of this tick. Every
            # chat has to have been polled before this set is trustworthy;
            # a half-polled tick would evict another chat's pending requests.
            fresh_ids: set[str] = set()
            polled_ok = True
            for chat_id in list(bridge.state.chats()):
                if chat_id not in bridge.config.allowed_chats:
                    # Polling a removed chat's directory would surface its
                    # pending requests - and possibly auto-answer them -
                    # for a conversation that is no longer allowed.
                    continue
                directory = bridge.state.directory(chat_id)
                try:
                    requests = bridge.api.permission_requests(directory=directory)
                except ApiError:
                    polled_ok = False
                    continue
                except Exception:
                    # A malformed response must not stop the watcher.
                    polled_ok = False
                    log.exception("Permission poll raised for %s", chat_id)
                    continue

                for request in requests or []:
                    request_id = request.get("id") or ""
                    if not request_id:
                        continue
                    fresh_ids.add(request_id)
                    if request_id in seen:
                        continue

                    bridge._requests[request_id] = request

                    decision = bridge.policy.decide(request, chat_id)
                    if decision:
                        # Only mark as seen once the answer actually landed.
                        # Marking first left the agent blocked forever on a
                        # request the watcher had stopped retrying.
                        if bridge._reply_permission(
                            chat_id,
                            request_id,
                            decision,
                            request,
                            notify=time.time() - nagged.get(request_id, 0.0)
                            > AUTO_RETRY_NOTICE,
                        ):
                            nagged.pop(request_id, None)
                            if request_id not in seen:
                                seen.append(request_id)
                                del seen[:-MAX_SEEN_PERMISSIONS]
                        else:
                            nagged[request_id] = time.time()
                            if len(nagged) > MAX_SEEN_PERMISSIONS:
                                for stale_id in list(nagged)[: len(nagged) // 2]:
                                    nagged.pop(stale_id, None)
                        continue

                    # Only mark as seen once the user has actually been
                    # told; marking first meant a failed send left the
                    # agent blocked on a request nobody was ever shown.
                    if bridge._notify_permission(chat_id, request_id, request):
                        seen.append(request_id)
                        del seen[:-MAX_SEEN_PERMISSIONS]

            # Reconcile only against a tick where every chat was polled.
            if polled_ok:
                bridge._reconcile_requests(fresh_ids)
        except Exception:
            log.exception("Permission watcher iteration failed")


def prune_seen(bridge, seen: list[str]) -> None:
    """Bound the dedupe list and the request cache."""
    if len(seen) > MAX_SEEN_PERMISSIONS:
        del seen[:-MAX_SEEN_PERMISSIONS]
    # Resolved-elsewhere ids are dropped by reconcile_requests() after a
    # fully-polled tick; this cap is only a backstop for pathological growth,
    # never an eviction policy: uniform "oldest half" trimming used to age
    # out still-pending requests.
    if len(bridge._requests) > MAX_CACHED_REQUESTS:
        for request_id in list(bridge._requests)[: len(bridge._requests) // 2]:
            bridge._requests.pop(request_id, None)


def reconcile_requests(bridge, fresh_ids: set[str]) -> None:
    """Drop cached request ids the server no longer lists as pending, and
    with them any reaction card pointed at one.

    Called only after a tick in which *every* allow-listed chat was polled:
    otherwise a single chat's API failure would evict the other chats'
    still-pending requests. Evicted-by-churn cards used to survive while
    their ids vanished, and a 👍 on them fell through to a chat-wide
    approve of whatever else happened to be pending.
    """
    for request_id in [key for key in bridge._requests if key not in fresh_ids]:
        bridge._requests.pop(request_id, None)
    for key in list(bridge._perm_cards):
        if bridge._perm_cards[key] not in bridge._requests:
            bridge._perm_cards.pop(key, None)


def reply_permission(
    bridge,
    chat_id: str,
    request_id: str,
    decision: str,
    request: dict | None = None,
    notify: bool = True,
) -> bool:
    """Answer a permission request. True when the answer actually landed.

    A ``False`` return must not drop the request from the watcher's cache or
    mark it seen: the agent stays blocked on the request, and the auto path
    polls reply_permission again on the next tick. ``notify`` is the retry
    notice switch, so a repeated failure inside :data:`AUTO_RETRY_NOTICE`
    is logged but not re-posted into the chat every second poll.
    """
    request = request or bridge._find_request(request_id)
    if not request:
        # The button still exists in the chat even though the request is
        # gone; silence here looks like a successful approval.
        bridge.send(chat_id, f"! {request_id} is no longer pending.")
        return False
    try:
        bridge.api.reply_permission(request.get("sessionID") or "", request_id, decision)
    except ApiError as exc:
        log.warning("Could not answer permission %s: %s", request_id, exc)
        # Silence here reads as a successful approval: the user reacted,
        # the card stayed, and the agent stayed blocked while we said
        # nothing. The request is still pending, so say so.
        if notify:
            bridge.send(chat_id, f"! Could not {decision} {request_id}: {exc.message}")
        return False

    bridge._requests.pop(request_id, None)
    for key in [k for k, v in bridge._perm_cards.items() if v == request_id]:
        bridge._perm_cards.pop(key, None)
    verb = {"once": "approved", "always": "always allowed", "reject": "rejected"}[decision]
    auto = " (auto)" if request.get("_auto") else ""
    bridge.send(chat_id, f"Permission {verb}{auto}: {request.get('action', '?')}")
    return True


def find_request(bridge, request_id: str) -> dict | None:
    """Look up a pending request from the watcher's cache.

    Button presses arrive with only the request id, and resolving it used
    to mean an API call per allow-listed chat until one matched. The
    watcher already polls these, so it caches them and this becomes a
    dict lookup - which also means answering works even if the server has
    gone away.
    """
    return bridge._requests.get(request_id)


def notify_permission(bridge, chat_id: str, request_id: str, request: dict) -> bool:
    """Ask the user. Returns False if the ask did not get through."""
    action = request.get("action", "?")
    resources = ", ".join(request.get("resources") or [])[:200]
    body = f"Permission needed: {action}\n{resources}\n\n{request_id}"
    try:
        message_id = bridge.telegram.send(
            chat_id, body, reply_markup=keyboards.permission_buttons(request_id)
        )
    except (TelegramError, OSError) as exc:
        log.warning(
            "Could not ask about permission %s: %s", request_id, bridge.telegram.redact(exc)
        )
        return False
    if message_id is None:
        # Delivered, but Telegram did not identify the message: the card
        # simply cannot be scoped by reaction. The ask still went
        # through - retrying would post a duplicate card.
        log.warning(
            "Permission %s card has no message id; reactions degrade to chat-wide",
            request_id,
        )
        return True
    bridge._perm_cards[(chat_id, message_id)] = request_id
    log.info("Asked chat %s about permission %s (%s)", chat_id, request_id, message_id)
    return True
