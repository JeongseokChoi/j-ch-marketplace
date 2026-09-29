#!/usr/bin/env python3
"""
watchdog.py - stall detector for delegate workers (Python 3 standard library only). The session runs it under the
Monitor tool, with the exact command the SessionStart hook gives it; each stdout line is one event.

  python3 watchdog.py --state-dir DIR --session ID [--stall 600] [--stall2 300] [--idle-grace 120]
                      [--interval 5] [--max-seconds 1860]

It follows DIR/ID/workers.jsonl (written by delegate.py's hooks) and watches the transcript (mtime and size) of every
worker and verifier that has started and not ended since: a hand-back, a SubagentStop after one, TaskStop, the return
of a foreground spawn or a notification ends it; a nudge (a SubagentStop before any hand-back, after which Claude Code
restarts the agent), a stale notification or a name record does not. One watchdog runs per session: a new one takes
over by writing DIR/ID/watchdog.owner, and the one it replaces ends quietly at its next loop (or at its next event,
which it then does not print). Its stdout carries these lines only:
  STALL <agent_id> <seconds> <where>    the transcript has not changed for <seconds>, at least --stall
  STALL2 <agent_id> <seconds> <where>   the stall went on for --stall2 more seconds
  ACTIVE <agent_id>                     a stalled worker's transcript changed again
<seconds> is the time since the transcript last changed (or since the worker started, if later). <where> is how the
transcript ends: tool=<name> inside a tool call, tool=<name>/<T>s inside a call with a timeout of T seconds (Bash),
model while it waits on the model, none without a transcript. Inside a call with timeout T the stall threshold is
max(--stall, T + 120): such a call cannot hold longer than T, after which it moves to the background. Each episode is
reported once; a worker that starts again (resumed) is watched afresh.
The defaults come from $DELEGATE_STALL, $DELEGATE_STALL2, $DELEGATE_IDLE_GRACE, $DELEGATE_INTERVAL and
$DELEGATE_MAX_SECONDS when set (the Monitor process inherits the environment of Claude Code), so tests can shorten them
without changing the command. It exits with status 0 once no worker has been active for --idle-grace seconds, or
after --max-seconds (a Monitor lasts 10-30 minutes; this ends a watchdog left behind). Events are also appended,
with a timestamp, to DIR/ID/watchdog.log.
"""
import argparse, json, os, sys, time
from datetime import datetime

TAIL = 1 << 20      # bytes at the end of a transcript read to see where a stalled worker is
SLACK = 120         # seconds a call with a timeout may take past it before it counts as stalled
ENDS = ("handback", "stop", "taskstop", "returned", "notified")      # not "nudge", "stale" or "name"


def env(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return float(default)


def stamp():
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def sig(path):
    try:
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_size)
    except (OSError, TypeError, ValueError):
        return None


def where(path):
    """(where, timeout seconds or None): the last tool call without a result, else "model"; "none" if unreadable."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - TAIL))
            lines = f.read().split(b"\n")
    except (OSError, TypeError, ValueError):
        return "none", None
    if size > TAIL:
        lines = lines[1:]                               # the first line may be cut
    pending = {}                                        # tool_use id -> (name, timeout seconds)
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        m = e.get("message") if isinstance(e, dict) and e.get("type") in ("assistant", "user") else None
        content = m.get("content") if isinstance(m, dict) else None
        for b in content if isinstance(content, list) else ():
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                to = (b.get("input") or {}).get("timeout") if isinstance(b.get("input"), dict) else None
                ok = isinstance(to, (int, float)) and not isinstance(to, bool) and to > 0
                pending[b.get("id")] = (str(b.get("name")), to / 1000 if ok else None)
            elif b.get("type") == "tool_result":
                pending.pop(b.get("tool_use_id"), None)
    if not pending:
        return "model", None
    name, to = list(pending.values())[-1]
    return (f"tool={name}/{int(to)}s" if to else f"tool={name}"), to


class Watchdog:
    def __init__(self, a):
        self.a = a
        self.dir = d = os.path.join(a.state_dir, a.session)
        self.state, self.log = os.path.join(d, "workers.jsonl"), os.path.join(d, "watchdog.log")
        self.owner, self.token = os.path.join(d, "watchdog.owner"), f"{os.getpid()}-{time.time_ns()}"
        self.workers, self.offset, self.rest, self.gone = {}, 0, b"", False

    def claim(self):
        """Take the session over: the watchdog that ran for it so far sees another owner and ends."""
        try:
            os.makedirs(self.dir, exist_ok=True)
            tmp = f"{self.owner}.{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(self.token)
            for _ in range(50):
                try:
                    os.replace(tmp, self.owner)
                    return
                except PermissionError:                 # Windows: a reader has it open
                    time.sleep(0.02)
            os.unlink(tmp)
        except OSError:
            pass

    def replaced(self):
        """True once a newer watchdog of this session has taken over."""
        try:
            with open(self.owner, encoding="utf-8") as f:
                return f.read().strip() not in ("", self.token)
        except OSError:
            return False

    def emit(self, line):
        if self.gone or self.replaced():                # the newer watchdog reports this itself
            self.gone = True
            return
        sys.stdout.buffer.write((line + "\n").encode("utf-8"))
        sys.stdout.buffer.flush()
        self.note(line)

    def note(self, line):
        try:
            with open(self.log, "a", encoding="utf-8") as f:
                f.write(f"{stamp()} {line}\n")
        except OSError:
            pass

    def read(self):
        """Take in the lines appended to workers.jsonl since the last read."""
        try:
            with open(self.state, "rb") as f:
                f.seek(self.offset)
                data = f.read()
        except OSError:
            return
        self.offset += len(data)
        lines = (self.rest + data).split(b"\n")
        self.rest = lines.pop()                         # a line still being written
        for line in lines:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            aid = r.get("agent_id") if isinstance(r, dict) else None
            if not isinstance(aid, str) or not aid:
                continue
            if r.get("ev") == "start":
                t = r.get("t")
                self.workers[aid] = {"transcript": r.get("transcript"), "ended": False, "sig": None, "level": 0,
                                     "t0": t if isinstance(t, (int, float)) else time.time()}
            elif r.get("ev") in ENDS and aid in self.workers:
                self.workers[aid]["ended"] = True

    def poll(self):
        now = time.time()
        for aid, w in self.workers.items():
            if w["ended"]:
                continue
            s = sig(w["transcript"])
            if s != w["sig"]:
                w["sig"] = s
                if w["level"]:
                    w["level"] = 0
                    self.emit(f"ACTIVE {aid}")
            idle = now - max(w["t0"], s[0] / 1e9 if s else 0)
            if idle < self.a.stall:
                continue
            if w.get("wsig", ()) != s:                  # read the tail again only after the transcript changed
                w["where"], w["wsig"] = where(w["transcript"]), s
            place, to = w["where"]
            limit = max(self.a.stall, to + SLACK) if to else self.a.stall
            if w["level"] == 0 and idle >= limit:
                w["level"] = 1
                self.emit(f"STALL {aid} {int(idle)} {place}")
            if w["level"] == 1 and idle >= limit + self.a.stall2:
                w["level"] = 2
                self.emit(f"STALL2 {aid} {int(idle)} {place}")

    def run(self):
        start = idle_since = time.time()
        a = self.a
        self.claim()
        self.note(f"(start pid={os.getpid()} stall={a.stall:g} stall2={a.stall2:g} idle_grace={a.idle_grace:g} "
                  f"interval={a.interval:g} max_seconds={a.max_seconds:g})")
        while True:
            if self.gone or self.replaced():
                self.note(f"(exit pid={os.getpid()}: a newer watchdog took over)")
                return 0
            self.read()
            self.poll()
            now = time.time()
            if any(not w["ended"] for w in self.workers.values()):
                idle_since = None
            elif idle_since is None:
                idle_since = now
            if idle_since is not None and now - idle_since >= a.idle_grace:
                self.note("(exit: no active worker)")
                return 0
            if a.max_seconds and now - start >= a.max_seconds:
                self.note("(exit: max-seconds)")
                return 0
            time.sleep(a.interval)


def main():
    p = argparse.ArgumentParser(description="Print STALL, STALL2 and ACTIVE events for delegate workers.")
    p.add_argument("--state-dir", required=True, help="<plugin data>/state")
    p.add_argument("--session", required=True, help="main session id; its state is <state-dir>/<session>/")
    p.add_argument("--stall", type=float, default=env("DELEGATE_STALL", 600),
                   help="seconds without a transcript change that make a STALL")
    p.add_argument("--stall2", type=float, default=env("DELEGATE_STALL2", 300),
                   help="further seconds of the same stall that make a STALL2")
    p.add_argument("--idle-grace", type=float, default=env("DELEGATE_IDLE_GRACE", 120),
                   help="exit after this many seconds without an active worker")
    p.add_argument("--interval", type=float, default=env("DELEGATE_INTERVAL", 5), help="poll interval in seconds")
    p.add_argument("--max-seconds", type=float, default=env("DELEGATE_MAX_SECONDS", 1860),
                   help="exit after this many seconds (0: never)")
    a = p.parse_args()
    if not a.session or os.sep in a.session or "/" in a.session or a.session in (".", ".."):
        return 2
    try:
        return Watchdog(a).run()
    except (BrokenPipeError, KeyboardInterrupt):        # the Monitor went away
        return 0


if __name__ == "__main__":
    sys.exit(main())
