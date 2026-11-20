"""F-003 acceptance + contract tests: the loop watchdog's stall dump.

Written from the spec (F-003 v4) alone. Every test drives one watchdog pass,
``main.MyClient.loop_watchdog.coro(client)``, with ``os._exit`` patched, and reads the
log records the pass emitted. Entry kinds are told apart by the spec's own format:

* header  - first ``STALL-DUMP`` entry, not a task entry
* task    - message starts ``STALL-DUMP task <name>`` (FR-3)
* thread  - any other entry mentioning a thread's name (the spec fixes no thread prefix)
"""
import asyncio
import inspect
import logging
import re
import subprocess
import sys
import threading
import time
import traceback
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import main

ROOT = Path(__file__).resolve().parent.parent
THIS_FILE = Path(__file__).name
BASE_COMMIT = "94ea9f35f5cd0d50760a1c6330946ecce888af27"
TASK_PREFIX = "STALL-DUMP task "

CUS_LIMIT = main.CHECK_USER_STATES_STALL_LIMIT  # 240
DAILY_LIMIT = main.DAILY_TASK_STALL_LIMIT  # 5400


# --------------------------------------------------------------------------- helpers
class Env:
    """One watchdog pass with captured logs and a patched ``os._exit``."""

    def __init__(self, monkeypatch, caplog):
        self.mp = monkeypatch
        self.caplog = caplog
        self.exit = MagicMock(name="os._exit")
        self.records_at_exit = None

        def _exit(code):
            self.records_at_exit = len(self.caplog.records)
            self.exit(code)

        monkeypatch.setattr(main.os, "_exit", _exit)
        caplog.set_level(logging.DEBUG)
        caplog.set_level(logging.DEBUG, logger=main.logger.name)
        self.client = object.__new__(main.MyClient)
        self.set_loops(None, None)
        self.client._cus_last_ok = None
        self.client._daily_last_ok = None

    def set_loops(self, cus_task, daily_task):
        for name, task in (("check_user_states", cus_task), ("daily_task", daily_task)):
            self.client.__dict__[name] = types.SimpleNamespace(
                get_task=lambda t=task: t,
                is_running=lambda t=task: t is not None and not t.done(),
                current_loop=0, failed=lambda: False)

    def stall(self, cus_age=None, daily_age=None):
        now = time.monotonic()
        self.client._cus_last_ok = None if cus_age is None else now - cus_age
        self.client._daily_last_ok = None if daily_age is None else now - daily_age

    async def run(self):
        self.caplog.clear()
        await main.MyClient.loop_watchdog.coro(self.client)

    @property
    def dump(self):
        return [r for r in self.caplog.records if r.getMessage().startswith("STALL-DUMP")]

    @property
    def msgs(self):
        return [r.getMessage() for r in self.dump]

    @property
    def text(self):
        return "\n".join(r.getMessage() for r in self.caplog.records) + "\n" + self.caplog.text

    @property
    def header(self):
        for m in self.msgs:
            if not m.startswith(TASK_PREFIX):
                return m
        raise AssertionError("no header entry logged")

    @property
    def task_entries(self):
        return [m for m in self.msgs if m.startswith(TASK_PREFIX)]

    def task_entry(self, name):
        return next(x for x in self.task_entries if first_line(x).startswith(TASK_PREFIX + name))

    def thread_entries(self, names):
        return [m for m in self.msgs
                if not m.startswith(TASK_PREFIX) and any(n in m for n in names)]


@pytest.fixture
def env(monkeypatch, caplog):
    return Env(monkeypatch, caplog)


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


def first_line(m):
    return m.splitlines()[0]


def last_line(m):
    return [l for l in m.splitlines() if l.strip()][-1]


def frame_lines(entry, func, fname=THIS_FILE):
    """(index, text) of entry lines showing file, a line number and the function name."""
    return [(i, l) for i, l in enumerate(entry.splitlines())
            if fname in l and re.search(rf"\b{re.escape(func)}\b", l) and re.search(r"\d+", l)]


def line_of(marker):
    src = Path(__file__).read_text(encoding="utf-8").splitlines()
    hits = [i + 1 for i, l in enumerate(src) if l.rstrip().endswith(marker)]
    assert len(hits) == 1, marker
    return hits[0]


async def park(name):
    return asyncio.create_task(_waiter(asyncio.Event()), name=name)


async def _waiter(ev):
    await ev.wait()


async def outer():
    await middle()


async def middle():
    await inner()


async def inner():
    await asyncio.Event().wait()  # INNER-AWAIT


async def holder(secret):
    local_value = "SENTINEL-LOCAL-" + "9c1b"
    await asyncio.Event().wait()  # HOLDER-AWAIT
    return secret, local_value


class ReprFuture(asyncio.Future):
    def __repr__(self):
        return "SENTINEL-REPR-" + "8b5f"


async def await_future(fut):
    await fut


class BadTask(asyncio.Task):
    """A task whose introspection raises - stands in for a capture failure on one task."""

    def get_coro(self):
        raise RuntimeError("boom-12")

    def get_stack(self, *a, **k):
        raise RuntimeError("boom-12")

    def print_stack(self, *a, **k):
        raise RuntimeError("boom-12")


class Worker:
    """A worker thread blocked in ``blocker()`` holding a sentinel local."""

    def __init__(self, name="stall-worker-blocker"):
        self.release = threading.Event()
        self.started = threading.Event()
        self.thread = threading.Thread(target=self.blocker, name=name, daemon=True)

    def blocker(self):
        thread_local = "SENTINEL-THREAD-" + "4d2e"
        self.started.set()
        self.release.wait()
        return thread_local

    def __enter__(self):
        self.thread.start()
        assert self.started.wait(5)
        time.sleep(0.05)
        return self

    def __exit__(self, *exc):
        self.release.set()
        self.thread.join(5)


async def cancel_all(*tasks):
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


# --------------------------------------------------------------------------- AC-1, AC-2
async def test_ac1_header_names_only_stalled_loop(env):
    t = await park("cus-task")
    await settle()
    env.set_loops(t, None)
    env.stall(cus_age=300, daily_age=5)
    await env.run()
    h = env.header
    assert "check_user_states" in h
    assert re.search(r"\b30[0-3]\b", h), h
    assert "240" in h
    assert "daily_task" not in h
    await cancel_all(t)


async def test_ac2_header_names_both_loops_with_own_age_and_limit(env):
    a, b = await park("cus-task"), await park("daily-task")
    await settle()
    env.set_loops(a, b)
    env.stall(cus_age=300, daily_age=6000)
    await env.run()
    h = env.header
    assert "check_user_states" in h and "daily_task" in h
    assert re.search(r"\b30[0-3]\b", h) and "240" in h
    assert re.search(r"\b60\d\d\b", h) and "5400" in h
    await cancel_all(a, b)


# --------------------------------------------------------------------------- AC-3
async def test_ac3_one_entry_per_task_labelled_with_name(env):
    ts = [await park(n) for n in ("alpha-task", "bravo-task", "charlie-task")]
    await settle()
    env.stall(cus_age=300)
    await env.run()
    names = {t.get_name() for t in asyncio.all_tasks()}
    entries = env.task_entries
    for n in ("alpha-task", "bravo-task", "charlie-task"):
        assert len([e for e in entries if first_line(e) == TASK_PREFIX + n]) == 1, n
    assert len(entries) == len(names), (len(entries), names)
    await cancel_all(*ts)


# --------------------------------------------------------------------------- AC-4, AC-5
async def test_ac4_stalled_loop_task_first_with_marker(env):
    others = [await park("other-1"), await park("other-2")]
    cus = await park("cus-task")
    await settle()
    env.set_loops(cus, None)
    env.stall(cus_age=300)
    await env.run()
    entries = env.task_entries
    assert first_line(entries[0]) == "STALL-DUMP task cus-task [STALLED LOOP: check_user_states]"
    for e in entries[1:]:
        assert "[STALLED LOOP:" not in first_line(e)
    await cancel_all(cus, *others)


async def test_ac5_both_stalled_order_and_plain_others(env):
    others = [await park("other-1"), await park("other-2")]
    daily = await park("daily-task")
    cus = await park("cus-task")
    await settle()
    env.set_loops(cus, daily)
    env.stall(cus_age=300, daily_age=6000)
    await env.run()
    firsts = [first_line(e) for e in env.task_entries]
    assert firsts[0].endswith("[STALLED LOOP: check_user_states]")
    assert firsts[1].endswith("[STALLED LOOP: daily_task]")
    known = {t.get_name() for t in asyncio.all_tasks()}
    for f in firsts[2:]:
        assert f.startswith(TASK_PREFIX) and f[len(TASK_PREFIX):] in known, f
    await cancel_all(cus, daily, *others)


async def test_watchdog_own_task_has_no_marker(env):
    me = asyncio.current_task()
    cus = await park("cus-task")
    await settle()
    env.set_loops(cus, None)
    env.stall(cus_age=300)
    await env.run()
    mine = [e for e in env.task_entries if first_line(e) == TASK_PREFIX + me.get_name()]
    assert len(mine) == 1
    await cancel_all(cus)


# --------------------------------------------------------------------------- AC-6, AC-7
async def test_ac6_await_chain_outer_middle_inner_in_order(env):
    t = asyncio.create_task(outer(), name="chain-task")
    await settle()
    env.stall(cus_age=300)
    await env.run()
    e = env.task_entry("chain-task")
    pos = {}
    for f in ("outer", "middle", "inner"):
        fl = frame_lines(e, f)
        assert fl, f"no frame line with file, line number and function {f}:\n{e}"
        pos[f] = fl[0][0]
    assert pos["outer"] < pos["middle"] < pos["inner"]
    await cancel_all(t)


async def test_ac7_inner_frame_shows_line_of_its_await(env):
    t = asyncio.create_task(outer(), name="chain-task")
    await settle()
    env.stall(cus_age=300)
    await env.run()
    e = env.task_entry("chain-task")
    want = line_of("# INNER-AWAIT")
    lines = [l for _, l in frame_lines(e, "inner")]
    assert any(re.search(rf"\b{want}\b", l) for l in lines), (want, lines)
    await cancel_all(t)


# --------------------------------------------------------------------------- AC-8
async def test_ac8_future_awaitable_named_by_type(env):
    fut = asyncio.get_running_loop().create_future()
    t = asyncio.create_task(await_future(fut), name="fut-task")
    await settle()
    env.stall(cus_age=300)
    await env.run()
    assert "Future" in last_line(env.task_entry("fut-task")), env.task_entry("fut-task")
    await cancel_all(t)


async def test_ac8_event_wait_chain_ends_with_awaitable_type(env):
    t = asyncio.create_task(outer(), name="chain-task")
    await settle()
    env.stall(cus_age=300)
    await env.run()
    assert "Future" in last_line(env.task_entry("chain-task"))
    await cancel_all(t)


# --------------------------------------------------------------------------- AC-9, AC-10
async def test_ac9_worker_and_main_thread_entries(env):
    t = await park("some-task")
    await settle()
    env.stall(cus_age=300)
    with Worker() as w:
        await env.run()
    ents = env.thread_entries(["stall-worker-blocker"])
    assert ents, env.msgs
    e = ents[0]
    assert str(w.thread.ident) in e
    assert frame_lines(e, "blocker"), e
    mt = threading.main_thread()
    assert any(mt.name in m and str(mt.ident) in m
               for m in env.msgs if not m.startswith(TASK_PREFIX)), env.msgs
    await cancel_all(t)


async def test_ac10_threads_after_all_tasks(env):
    ts = [await park(f"t{i}") for i in range(3)]
    await settle()
    env.stall(cus_age=300)
    with Worker():
        await env.run()
    msgs = env.msgs
    task_idx = [i for i, m in enumerate(msgs) if m.startswith(TASK_PREFIX)]
    thr_idx = [i for i, m in enumerate(msgs)
               if not m.startswith(TASK_PREFIX)
               and ("stall-worker-blocker" in m or threading.main_thread().name in m)]
    assert task_idx and thr_idx
    assert max(task_idx) < min(thr_idx)
    await cancel_all(*ts)


# --------------------------------------------------------------------------- AC-11
async def test_ac11_exit_once_with_1_after_all_entries(env):
    t = await park("some-task")
    await settle()
    env.stall(cus_age=300)
    await env.run()
    env.exit.assert_called_once_with(1)
    assert env.records_at_exit == len(env.caplog.records) and env.records_at_exit > 0
    await cancel_all(t)


# --------------------------------------------------------------------------- AC-12, AC-13
async def test_ac12_one_task_capture_raises_rest_still_dumped(env):
    good = [await park("good-1"), await park("good-2")]
    bad = BadTask(_waiter(asyncio.Event()), name="bad-task")
    await settle()
    env.stall(cus_age=300)

    def boom(*a, **k):
        raise RuntimeError("boom-12")

    with env.mp.context() as m:
        if hasattr(asyncio, "capture_call_graph"):
            m.setattr(asyncio, "capture_call_graph", boom)
        await env.run()
    assert any("bad-task" in x and "boom-12" in x for x in env.msgs), env.msgs
    firsts = [first_line(e) for e in env.task_entries]
    for n in ("good-1", "good-2"):
        assert any(f.startswith(TASK_PREFIX + n) for f in firsts), n
    assert env.thread_entries([threading.main_thread().name])
    env.exit.assert_called_once_with(1)
    await cancel_all(bad, *good)


def _helper_candidates():
    """Callables the feature adds for the dump (found by name containing dump/stall)."""
    found = []
    mods = [main]
    for mod in list(sys.modules.values()):
        f = getattr(mod, "__file__", None)
        if f and mod is not main and Path(f).parent == ROOT and \
                Path(f).stem not in ("conftest", "name_utils", "server"):
            mods.append(mod)
    for mod in mods:
        for n, obj in vars(mod).items():
            if re.search(r"dump|stall", n, re.I) and inspect.isfunction(obj) \
                    and obj.__module__ == mod.__name__:
                found.append((mod, n, obj))
    for n, obj in vars(main.MyClient).items():
        if re.search(r"dump|stall", n, re.I) and inspect.isfunction(obj) and n != "_loops_stalled":
            found.append((main.MyClient, n, obj))
    return found


async def test_ac13_every_dump_helper_raises_exit_still_called(env):
    t = await park("some-task")
    await settle()
    env.stall(cus_age=300, daily_age=6000)

    def boom(*a, **k):
        raise RuntimeError("boom-13")

    with env.mp.context() as m:
        for owner, n, _ in _helper_candidates():
            m.setattr(owner, n, boom)
        m.setattr(sys, "_current_frames", boom)
        m.setattr(threading, "enumerate", boom)
        for fn in ("extract_stack", "format_stack", "walk_stack", "format_list", "extract_tb"):
            m.setattr(traceback, fn, boom)
        if hasattr(asyncio, "capture_call_graph"):
            m.setattr(asyncio, "capture_call_graph", boom)
        await env.run()  # must not raise
    env.exit.assert_called_once_with(1)
    await cancel_all(t)


# --------------------------------------------------------------------------- AC-14
async def test_ac14_pass_never_suspends(env):
    t = await park("some-task")
    await settle()
    env.stall(cus_age=300)
    coro = main.MyClient.loop_watchdog.coro(env.client)
    with pytest.raises(StopIteration):
        coro.send(None)  # finishes on first send => it awaited nothing that suspends
    env.exit.assert_called_once_with(1)
    await cancel_all(t)


def test_ac14_helpers_are_plain_functions_and_log_full_dump_without_event_loop(env, monkeypatch):
    cands = _helper_candidates()
    assert cands, "no dump helper found in main.py (names containing 'dump' or 'stall')"
    for _, n, obj in cands:
        assert not inspect.iscoroutinefunction(obj), f"{n} is a coroutine function"

    loop = asyncio.new_event_loop()
    try:
        async def sleeper(ev):
            await ev.wait()
        tasks = [loop.create_task(sleeper(asyncio.Event()), name=f"sync-task-{i}") for i in range(3)]
        loop.run_until_complete(asyncio.sleep(0.01))  # tasks suspended; loop not running now
        monkeypatch.setattr(asyncio, "all_tasks", lambda loop=None: set(tasks))
        env.stall(cus_age=300)
        stalled = [("check_user_states", 300, CUS_LIMIT)]
        by_name = {
            "self": env.client, "client": env.client, "bot": env.client,
            "now": time.monotonic(), "tasks": list(tasks), "all_tasks": list(tasks),
            "snapshot": list(tasks), "stalled": stalled, "stalled_loops": stalled, "loops": stalled,
        }
        ok = []
        for _, n, obj in cands:
            env.caplog.clear()
            args = {}
            for p in inspect.signature(obj).parameters.values():
                if p.name in by_name:
                    args[p.name] = by_name[p.name]
                elif p.default is inspect.Parameter.empty and p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY):
                    args[p.name] = tasks[0] if "task" in p.name else None
            try:
                obj(**args)
            except Exception:
                continue
            txt = "\n".join(env.msgs)
            if all(f"sync-task-{i}" in txt for i in range(3)) and threading.main_thread().name in txt:
                ok.append(n)
        assert ok, "no dump helper, called directly from sync code, logged the full dump"
    finally:
        for t in tasks:
            t.cancel()
        loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
        loop.close()
        asyncio.set_event_loop(None)


# --------------------------------------------------------------------------- AC-15, AC-16
@pytest.mark.parametrize("cus,daily", [(None, None), (5, 5), (None, 5), (5, None)])
async def test_ac15_fresh_or_none_heartbeats_do_nothing(env, cus, daily):
    env.stall(cus_age=cus, daily_age=daily)
    await env.run()
    assert not [r for r in env.caplog.records if r.levelno >= logging.CRITICAL]
    env.exit.assert_not_called()


def test_ac16_existing_loop_resilience_tests_unmodified_and_pass():
    diff = subprocess.run(["git", "diff", "--quiet", BASE_COMMIT, "--", "tests/test_loop_resilience.py"],
                          cwd=ROOT, capture_output=True)
    assert diff.returncode == 0, "tests/test_loop_resilience.py was modified"
    r = subprocess.run([sys.executable, "-m", "pytest", "tests/test_loop_resilience.py", "-q",
                        "-p", "no:cacheprovider"], cwd=ROOT, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr


# --------------------------------------------------------------------------- AC-17, AC-18
async def test_ac17_no_locals_or_arguments_in_log(env):
    t = asyncio.create_task(holder("SENTINEL-ARG-" + "7f3a"), name="holder-task")
    await settle()
    env.stall(cus_age=300)
    with Worker():
        await env.run()
    for s in ("SENTINEL-ARG-7f3a", "SENTINEL-LOCAL-9c1b", "SENTINEL-THREAD-4d2e"):
        assert s not in env.text, s
    e = env.task_entry("holder-task")
    lines = frame_lines(e, "holder")
    assert lines and any(str(line_of("# HOLDER-AWAIT")) in l for _, l in lines), e
    await cancel_all(t)


async def test_ac18_awaitable_named_by_type_never_repr(env):
    fut = ReprFuture(loop=asyncio.get_running_loop())
    t = asyncio.create_task(await_future(fut), name="repr-task")
    await settle()
    env.stall(cus_age=300)
    await env.run()
    assert "ReprFuture" in last_line(env.task_entry("repr-task"))
    assert "SENTINEL-REPR-8b5f" not in env.text
    await cancel_all(t)


# --------------------------------------------------------------------------- AC-19
async def test_ac19_all_entries_on_bot_logger_at_critical_with_prefix(env):
    t = await park("some-task")
    await settle()
    env.stall(cus_age=300, daily_age=6000)
    with Worker():
        await env.run()
    recs = [r for r in env.caplog.records if r.name == main.logger.name]
    assert len(env.dump) >= 3 and len(recs) >= 3
    for r in env.dump:
        assert r.name == main.logger.name and r.levelno == logging.CRITICAL
    for r in recs:
        assert r.levelno == logging.CRITICAL and r.getMessage().startswith("STALL-DUMP"), r.getMessage()
    await cancel_all(t)


# --------------------------------------------------------------------------- section 6 edge cases
async def test_edge_stalled_loop_without_task_noted_and_dump_continues(env):
    t = await park("some-task")
    await settle()
    env.set_loops(None, None)
    env.stall(cus_age=300)
    await env.run()
    h = env.header
    assert "check_user_states" in h and re.search(r"\b30[0-3]\b", h)
    assert re.search(r"no (running )?task", h, re.I), h
    assert any(first_line(e).startswith(TASK_PREFIX + "some-task") for e in env.task_entries)
    assert env.thread_entries([threading.main_thread().name])
    env.exit.assert_called_once_with(1)
    await cancel_all(t)


async def test_edge_done_stalled_task_does_not_raise(env):
    async def quick():
        return 1
    done = asyncio.create_task(quick(), name="done-task")
    await done
    env.set_loops(done, None)
    env.stall(cus_age=300)
    await env.run()
    assert "check_user_states" in env.header
    env.exit.assert_called_once_with(1)


async def test_edge_not_yet_started_task_gets_entry(env):
    fresh = asyncio.create_task(_waiter(asyncio.Event()), name="fresh-task")  # never yielded
    env.stall(cus_age=300)
    await env.run()
    assert any(first_line(e).startswith(TASK_PREFIX + "fresh-task") for e in env.task_entries)
    env.exit.assert_called_once_with(1)
    await cancel_all(fresh)


async def test_edge_very_deep_chain_terminates(env):
    async def deep(n):
        if n == 0:
            await asyncio.Event().wait()
        else:
            await deep(n - 1)
    t = asyncio.create_task(deep(600), name="deep-task")
    await settle()
    env.stall(cus_age=300)
    await asyncio.wait_for(env.run(), 30)
    assert any(first_line(e).startswith(TASK_PREFIX + "deep-task") for e in env.task_entries)
    env.exit.assert_called_once_with(1)
    await cancel_all(t)
