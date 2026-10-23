"""Turn execution: one worker per chat, streaming, recovery and delivery."""

from __future__ import annotations

import logging
import queue
import threading
import time

from . import keyboards
from .api import ApiError, OpenCode
from .progress import TurnProgress
from .telegram import TelegramError

log = logging.getLogger("bridge.turns")

# Messages held per chat while a turn is running.
MAX_BACKLOG = 5

# Consecutive transport failures that mean the OpenCode server is gone rather
# than briefly busy. Five polls at the default 2s cadence is about ten seconds.
MAX_TRANSPORT_FAILURES = 5


def worker_loop(bridge) -> None:
    """Dispatch queued turns, one worker thread per chat.

    Turns are serialised *within* a chat, because a session processes one
    turn at a time anyway, but different chats run concurrently. A single
    global worker meant one twenty-minute job silently blocked every other
    allow-listed user for twenty minutes.
    """
    while not bridge.stop_event.is_set():
        try:
            chat_id, text, files = bridge.work.get(timeout=1.0)
        except queue.Empty:
            # The dispatcher demonstrably ran this iteration, so the
            # infrastructure is alive - the number of registered turn
            # workers has nothing to do with that.
            bridge.health.worker_alive = True
            continue

        bridge.health.worker_alive = True
        # The busy check and the backlog append must be atomic against the
        # turn worker's "pop worker + drain backlog" section: check-then-act
        # used to straddle the turn end, so a message could be appended to a
        # backlog tuple that had already been drained and was never sent.
        queued = False
        busy = False
        with bridge._lock:
            busy = bridge._chat_busy(chat_id)
            if busy:
                backlog = bridge._backlog.setdefault(chat_id, [])
                if len(backlog) < MAX_BACKLOG:
                    backlog.append((chat_id, text, files))
                    queued = True
        if not busy:
            pass
        elif queued:
            # /delivery promises queueing, so hold the message rather than
            # making the user retype it - and say so, because a silently
            # delayed message looks like a dropped one.
            bridge.send(
                chat_id,
                "! Queued - I am still working on the previous message.",
            )
            bridge.work.task_done()
            continue
        else:
            bridge.send(chat_id, "! Backlog full; send that again once I finish.")
            bridge.work.task_done()
            continue

        # Register before starting: the thread sets its own identity on
        # entry, so between .start() and that assignment a second prompt
        # for the same chat would look idle and run concurrently.
        placeholder = threading.current_thread()
        with bridge._lock:
            bridge._workers[chat_id] = placeholder
        thread = threading.Thread(
            target=bridge._turn_worker,
            args=(chat_id, text, files),
            name=f"turn-{chat_id}",
            daemon=True,
        )
        thread.start()
        bridge.work.task_done()


def chat_busy(bridge, chat_id: str) -> bool:
    worker = bridge._workers.get(chat_id)
    return bool(worker and worker.is_alive())


def turn_worker(bridge, chat_id: str, text: str, files: list | None) -> None:
    """Run one turn in its own thread. Errors are reported, never raised."""
    bridge._workers[chat_id] = threading.current_thread()
    try:
        bridge._run_turn(chat_id, text, files)
    except ApiError as exc:
        bridge.health.errors += 1
        bridge.send(chat_id, f"! OpenCode error: {exc.message}")
    except Exception:
        log.exception("Turn failed")
        bridge.health.errors += 1
        bridge.send(chat_id, "! The turn failed; check the bridge log.")
    finally:
        bridge.clear_abort(chat_id)
        # Pop the worker and drain the backlog as one step so a dispatcher
        # arriving between the two either still sees this turn as busy (and
        # appends to the backlog we then drain) or sees it free (and runs
        # directly). The old pop-then-drain let an interleaved append land
        # in a backlog nobody would ever revisit.
        with bridge._lock:
            if bridge._workers.get(chat_id) is threading.current_thread():
                bridge._workers.pop(chat_id, None)
            drained = bridge._backlog.pop(chat_id, [])
        for item in drained:
            bridge.work.put(item)


def run_turn(bridge, chat_id: str, text: str, files: list | None = None) -> None:
    session = bridge.ensure_session(chat_id)
    bridge.health.turns += 1
    bridge.health.last_turn_at = time.time()
    bridge.health.prompts += 1
    log.info(
        "Prompt for %s (%d chars): %s",
        chat_id,
        len(text),
        text[: bridge.config.log_prompt_chars].replace("\n", " "),
    )

    watch = bool(bridge.state.pref(chat_id, "watch", True) and bridge.config.stream_enabled)
    progress = TurnProgress()
    # Per turn, not on bridge: with two chats running concurrently, a shared
    # flag meant one turn's alert silenced the other's, or doubled it.
    cost_alerted = {"fired": False}
    message_id = None

    if bridge.config.stream_enabled:
        bridge.events.listen(session, progress.on_event)

    submitted_at = int(time.time() * 1000)
    delivery = bridge.state.pref(chat_id, "delivery", bridge.config.prompt_delivery)
    try:
        if watch:
            # Guarded: a transport error here used to abort the turn before
            # the prompt was ever submitted, silently losing the message.
            try:
                message_id = bridge.telegram.send(chat_id, progress.render())
            except (TelegramError, OSError) as exc:
                log.warning(
                    "Could not post the live placeholder: %s", bridge.telegram.redact(exc)
                )
                message_id = None

        # Remember the in-flight turn so a bridge restart can report it
        # instead of silently orphaning the work.
        bridge.state.set_busy(chat_id, session=session, started=submitted_at)

        try:
            bridge.api.send_prompt(session, text, files, delivery=delivery)
        except ApiError as exc:
            if exc.status != 404:
                raise
            old_session = session
            session = bridge._recover_session(chat_id, exc)
            submitted_at = int(time.time() * 1000)
            bridge.state.set_busy(chat_id, session=session, started=submitted_at)
            if bridge.config.stream_enabled:
                # Unlisten the *old* id: after reassignment the new id was
                # never registered, so the dead listener leaked forever.
                bridge.events.unlisten(old_session)
                bridge.events.listen(session, progress.on_event)
            bridge.api.send_prompt(session, text, files, delivery=delivery)

        message_id = bridge._pump(
            chat_id, session, progress, message_id, submitted_at, watch, cost_alerted
        )
    finally:
        if bridge.config.stream_enabled:
            bridge.events.unlisten(session)
        bridge.state.clear_busy(chat_id)
        bridge.clear_abort(chat_id)

    final = bridge._final_text(progress, session, submitted_at)
    markup = keyboards.session_buttons(session)

    bridge._deliver_final(chat_id, message_id, final, markup, watch)

    bridge._check_cost_alert(chat_id, progress, cost_alerted)


def pump(
    bridge,
    chat_id: str,
    session: str,
    progress: TurnProgress,
    message_id: int | None,
    submitted_at: int,
    watch: bool,
    cost_alerted: dict,
) -> int | None:
    """Poll until the turn ends, editing the live message as it goes.

    The hard deadline only fires for genuinely wedged turns. A long but
    healthy job gets a heads-up at HEADS_UP_AFTER and then keeps waiting,
    because abandoning a turn that is still running is what makes a chat
    look dead.
    """
    deadline = time.time() + bridge.config.prompt_timeout
    heads_up = bridge.config.heads_up_after
    warned = False
    failures = 0
    last_render = 0.0
    last_messages: list[dict] = []

    while time.time() < deadline:
        if bridge.abort_flag(chat_id).is_set():
            progress.add_note("interrupted")
            progress.status = "done"
            progress.finished_at = time.time()
            try:
                bridge.api.interrupt_session(session)
            except ApiError:
                pass
            break

        if not warned and progress.elapsed >= heads_up:
            warned = True
            progress.add_note(f"still working after {progress.elapsed / 60:.0f}m")
            log.info("Long turn in %s after %ds", session, int(progress.elapsed))
            last_render = 0.0  # force a refresh so the note is visible

        try:
            messages = bridge.api.messages(session, limit=30, order="desc")
            failures = 0
        except ApiError as exc:
            # A dead server used to mean silence for the whole timeout -
            # up to two hours of nothing, one log line every poll. Count
            # consecutive transport failures and bail out with an
            # explanation instead.
            failures += 1
            if failures == 1 or failures % 15 == 0:
                log.warning("Message poll failed (%d in a row): %s", failures, exc)
            if failures >= MAX_TRANSPORT_FAILURES:
                progress.add_note("lost contact with the OpenCode server")
                progress.status = "failed"
                progress.error = "the server became unreachable"
                progress.finished_at = time.time()
                bridge.health.errors += 1
                log.error("Abandoning turn after %d transport failures", failures)
                break
            messages = []

        progress.absorb_messages(messages, since=submitted_at)
        last_messages = messages

        polled = OpenCode.assistant_text(messages, submitted_at)
        if polled:
            # The event stream is the fast path for text. If it is down or
            # dropped events, fall back to what polling already has.
            # Adopting the polled text only when it is *longer* keeps a
            # healthy stream authoritative while still letting a turn
            # whose stream died partway through be repaired - and doing
            # it under the progress lock stops the adoption from
            # interleaving with a delta append mid-write.
            progress.set_if_longer(polled)

        finished = any(
            m.get("type") == "idle" and (m.get("time") or {}).get("created", 0) > submitted_at
            for m in messages
        )

        # Cost alerts fire while the turn is still spending, not only at
        # the end: by then the money is already gone.
        if not cost_alerted["fired"] and progress.cost:
            threshold = float(
                bridge.state.pref(chat_id, "cost_alert", bridge.config.cost_alert) or 0
            )
            if threshold and progress.cost >= threshold:
                cost_alerted["fired"] = True
                bridge.send(
                    chat_id,
                    f"Cost alert: ${progress.cost:.4f} so far, past your "
                    f"${threshold:.2f} threshold. /abort if you want it stopped.",
                )

        if (
            watch
            and message_id
            and time.time() - last_render >= bridge.telegram.effective_interval()
        ):
            last_render = time.time()
            try:
                bridge.telegram.edit(chat_id, message_id, progress.render())
            except (TelegramError, OSError):
                # Cosmetic only: never let a progress tick break the turn.
                log.debug("Live edit failed", exc_info=True)

        if finished:
            if not progress.finished_at:
                progress.status = "done"
                progress.finished_at = time.time()
            break

        time.sleep(bridge.config.poll_messages)
    else:
        # The turn may still be alive on the server; say so rather than
        # pretending it ended.
        progress.add_note("gave up waiting; the job may still be running")
        progress.status = "done"
        progress.finished_at = time.time()

    if last_messages:
        final_polled = OpenCode.assistant_text(last_messages, submitted_at)
        if final_polled:
            progress.set_if_longer(final_polled)
    return message_id


def deliver_final(
    bridge, chat_id: str, message_id: int | None, text: str, markup: dict | None, watch: bool
) -> None:
    """Send the finished reply, and never let it be swallowed.

    Editing the live message is preferred, but Telegram rate-limits edits
    and refuses edits of old messages. Both cases used to drop the answer on
    the floor, which from the chat looks exactly like the bot stopped
    mid-job. If the edit does not land, send a fresh message instead.
    """
    if watch and message_id:
        if bridge.telegram.edit(chat_id, message_id, text, reply_markup=markup, force=True):
            return
        log.warning("Final edit failed (rate limit or stale message); sending instead")
    bridge.send(chat_id, text, markup)


def recover_session(bridge, chat_id: str, exc: ApiError) -> str:
    """Recreate a session the server no longer has, and say so.

    Reached when the OpenCode server restarted mid-conversation and lost
    the session: the prompt came back 404 instead of being queued.
    """
    log.warning("Session for chat %s is gone (%s); recreating", chat_id, exc.message)
    directory = bridge.state.directory(chat_id)
    created = bridge.api.create_session(directory)
    bridge.state.set_session(chat_id, created["id"], directory=directory)
    bridge.send(
        chat_id,
        "The OpenCode server restarted and dropped this session, so I started a "
        f"fresh one ({created['id']}) and resent your message. Earlier context "
        "in this chat is gone; /sessions lists anything the server kept.",
    )
    return created["id"]


def final_text(bridge, progress: TurnProgress, session: str, submitted_at: int) -> str:
    text = progress.final_text(bridge.config.max_output)
    # Test the agent's text, not the rendered block: an answer that happens
    # to contain the words "(no reply)" was being thrown away.
    if progress.text.strip() or "(no reply)" not in text:
        return text
    try:
        messages = bridge.api.messages(session, limit=10, order="desc")
    except ApiError:
        return text
    fallback = OpenCode.assistant_text(messages, submitted_at)
    if not fallback:
        return text
    # Re-render rather than return the bare fallback, so notes like
    # "interrupted" and the duration footer survive.
    progress.set_if_longer(fallback.strip())
    return progress.final_text(bridge.config.max_output)


def check_cost_alert(bridge, chat_id: str, progress: TurnProgress, cost_alerted: dict) -> None:
    """Catch a turn that finished fast but still crossed the threshold."""
    threshold = float(bridge.state.pref(chat_id, "cost_alert", bridge.config.cost_alert) or 0)
    if threshold and progress.cost >= threshold and not cost_alerted["fired"]:
        cost_alerted["fired"] = True
        bridge.send(
            chat_id,
            f"Cost alert: this session has reached ${progress.cost:.4f} "
            f"(threshold ${threshold:.2f}).",
        )
