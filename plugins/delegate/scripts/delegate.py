#!/usr/bin/env python3
"""
delegate.py - hooks of the delegate plugin (Python 3 standard library only).

  python3 delegate.py <mode>          hooks/hooks.json runs one mode per hook; stdin is the hook input JSON

  session-start   SessionStart                  make the details folder; tell the session its role, that folder and
                                                the exact watchdog Monitor command
  guard           PreToolUse, every tool        keep the session to its tools, the watchdog Monitor and delegate
                                                spawns; hold back a SendMessage to an agent that has handed back
                                                but whose run has not ended
  handback-gate   PreToolUse SubagentHandback   deny a worker or verifier report that breaks the report format
  handback-post   PostToolUse SubagentHandback  record the hand-back and its TASK; a verifier's verdict also goes to
                                                the ledger
  subagent-start  SubagentStart                 register a worker and its transcript for the watchdog
  subagent-stop   SubagentStop                  mark the worker ended, but only once it has handed back
  taskstop-post   PostToolUse TaskStop          mark the worker ended (stopped)
  agent-post      PostToolUse Agent|Task        a foreground spawn returned: mark its worker ended; keep spawn names
  prompt          UserPromptSubmit              a <task-notification> about a worker marks it ended; notification and
                                                agent-message turns get a reminder of the reply language
  stop-gate       Stop                          block DONE[T<n>] claims that the ledger does not support

The session is the main thread running as delegate:session (agent_type "delegate:session", no agent_id).
session-start, guard and stop-gate act only there, so a plain session (claude --agent "") is left alone.
Workers are subagents of type delegate:worker-* and delegate:verifier-*; the other modes act only on those.

State of one main session lives in <plugin data>/state/<session_id>/ (plugin data: $CLAUDE_PLUGIN_DATA):
  workers.jsonl  worker events, one JSON object per line with "ev", "agent_id", "t" (epoch seconds) and "time":
                 start {agent_type, transcript} | handback {agent_type, task, status, valid} | stop | nudge |
                 taskstop | returned | notified {status, summary} | stale {status, summary} | name {name}.
                 start makes a worker active (again, when it is resumed). nudge is a SubagentStop before any
                 hand-back since that start: Claude Code nudges the agent on, so it stays active. stale is a "killed"
                 notification that arrived after the stopped worker was resumed. name maps a spawn's name to its
                 agent_id. None of these three ends a worker; every other event does. watchdog.py follows this file.
  ledger.jsonl   verifier verdicts: {task, round, perspective, verdict, agent_id, agent_type, t, time}
  counters.json  {"stop_blocks": {"T<n>": count}}
  details/       the T<n>-details files of the workers (created at SessionStart)
  watchdog.owner the watchdog instance that runs for this session (a newer one takes over)
Writers hold <state dir>/.lock. A hook prints nothing unless it makes a decision (one JSON object on stdout) and
always exits 0; internal errors are appended to <plugin data>/error.log.
"""
import contextlib, json, os, re, shlex, sys, tempfile, time, traceback
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
WATCHDOG = os.path.join(HERE, "watchdog.py")
DATA = os.environ.get("CLAUDE_PLUGIN_DATA") or os.path.join(tempfile.gettempdir(), "claude-delegate")
STATE = os.path.join(DATA, "state")
SESSION = "delegate:session"
TOOLS = frozenset((
    "Agent", "Task",            # hooks name the Agent tool "Agent"; init and permission denials call it "Task"
    "SendMessage", "TaskStop", "Monitor", "ToolSearch", "AskUserQuestion", "ListAgents", "PushNotification",
    "EnterPlanMode", "ExitPlanMode", "mcp__plugin_workflowy_workflowy__create",
    "mcp__plugin_workflowy_workflowy__read", "mcp__plugin_workflowy_workflowy__close"))
ROLES = ("delegate:worker-low, delegate:worker-medium, delegate:worker-xhigh, "
         "delegate:verifier-xhigh or delegate:verifier-max (verifiers with model opus or fable)")
VERIFIER_MODELS = ("opus", "fable")
PYTHONS = ("python3", "python", "py", "python3.exe", "python.exe", "py.exe")
SID = re.compile(r"[A-Za-z0-9_-]+")                   # a session id that is safe as a directory name

STATUSES = ("done", "question", "blocked", "failed")
VERDICTS = ("PASS", "FAIL")
PERSPECTIVES = ("requirements", "behavior", "side-effects", "conventions", "facts")
BASE = PERSPECTIVES[:3]                               # the perspectives every verification covers
WORKER_FIELDS = ("STATUS", "TASK", "SUMMARY", "ARTIFACTS", "VERIFIED", "OPEN")
VERIFIER_FIELDS = WORKER_FIELDS + ("VERDICT", "PERSPECTIVE", "ROUND")
FIELD = re.compile(r"\s*([A-Z]+):(.*)")
WORKER_FORMAT = ("STATUS: done | question | blocked | failed\nTASK: T<n>\nSUMMARY: ...\nARTIFACTS: ...\n"
                 "VERIFIED: ...\nOPEN: ...")
VERIFIER_FORMAT = (WORKER_FORMAT + "\nVERDICT: PASS | FAIL\n"
                   "PERSPECTIVE: requirements | behavior | side-effects | conventions | facts\nROUND: <n>")
MARK = re.compile(r"DONE\[T(\d+)\]")                 # the completion marker; see claims() for what counts
LISTNUM = re.compile(r"^\s*\d+[.)]\s+")              # "1. " in front of a marker line
REF = re.compile(r"\s*\[[^\[\]]*\]$")                # a trailing " [ref]" on a SendMessage target
NEEDED = 3        # PASS verdicts a task needs in its latest round: distinct perspectives from distinct verifiers
STRONG = 3        # after this many blocks of one task the stop gate tells the session to drop the claim
NOTE = re.compile(r"<task-notification>(.*?)(?:</task-notification>|\Z)", re.S)
REMIND = ("[delegate] Reply to the user in the user's language (Korean unless the user writes in another "
          "language), one-line status updates included.")

# ----------------------------------------------------------------- small helpers


def now():
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def fwd(p):
    return p.replace("\\", "/")


def clip(s, n=60):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[:n - 3] + "..."


def is_session(ev):
    """The main thread running as delegate:session (a subagent input always carries agent_id)."""
    return ev.get("agent_type") == SESSION and not ev.get("agent_id")


def role(ev):
    t = str(ev.get("agent_type") or "")
    return "worker" if t.startswith("delegate:worker-") else "verifier" if t.startswith("delegate:verifier-") else None


def tool_input(ev):
    x = ev.get("tool_input")
    return x if isinstance(x, dict) else {}


def deny(why):
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": why}}

# ----------------------------------------------------------------- state


def state_dir(ev):
    sid = str(ev.get("session_id") or "")
    return os.path.join(STATE, sid) if SID.fullmatch(sid) else None


@contextlib.contextmanager
def locked(d):
    """Hold <d>/.lock while hooks that may run at the same time write the state (the lock of wf.py).
    If it cannot be had within about 5 s, go on without it rather than lose the event."""
    os.makedirs(d, exist_ok=True)
    p, got = os.path.join(d, ".lock"), False
    for _ in range(100):
        try:
            os.close(os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            got = True
            break
        except OSError:
            try:
                if time.time() - os.stat(p).st_mtime > 10:     # left behind by a hook that was killed
                    os.unlink(p)
            except OSError:
                pass
            time.sleep(0.05)
    try:
        yield
    finally:
        if got:
            try:
                os.unlink(p)
            except OSError:
                pass


def append(path, rec):
    rec.update(t=round(time.time(), 3), time=now())
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def records(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict):
            out.append(r)
    return out


def load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            o = json.load(f)
        return o if isinstance(o, dict) else {}
    except (OSError, ValueError):
        return {}


def save_json(path, obj):
    """Replace the file whole, so a reader never sees half of it (Windows refuses while it is open: retry)."""
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    for _ in range(50):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.05)
    os.unlink(tmp)


def known(d):
    """agent_ids of the workers registered in this session."""
    return {r.get("agent_id") for r in records(os.path.join(d, "workers.jsonl")) if r.get("ev") == "start"}

# ----------------------------------------------------------------- the watchdog command (one helper for both sides)


def watchdog_argv(sid):
    """The one Monitor command the session may run, as argv. Positions 1 and 3 are paths."""
    return ["python3", fwd(WATCHDOG), "--state-dir", fwd(STATE), "--session", sid]


def watchdog_command(sid):
    """The command line given to the session at SessionStart; is_watchdog() accepts exactly this."""
    return " ".join(f'"{a}"' if i in (1, 3) else a for i, a in enumerate(watchdog_argv(sid)))


def norm(p):
    """Compare paths written as C:\\x, C:/x or (Git Bash) /c/x."""
    p = p.replace("\\", "/")
    if os.name == "nt":
        m = re.match(r"/([A-Za-z])(?=/|$)", p)
        if m:
            p = m.group(1) + ":" + (p[m.end():] or "/")
    return os.path.normcase(os.path.normpath(p))


def is_watchdog(cmd, sid):
    """True for the watchdog command of this session, in any path form and quoting. Chained commands, redirections
    and extra arguments add or change words, so they never match; $ and ` are refused outright."""
    if not isinstance(cmd, str) or not sid or re.search(r"[`$]", cmd):
        return False
    try:
        argv = shlex.split(cmd.replace("\\", "/"))
    except ValueError:
        return False
    want = watchdog_argv(sid)
    return (len(argv) == len(want) and os.path.basename(argv[0]).lower() in PYTHONS
            and all(norm(a) == norm(w) if i in (1, 3) else a == w
                    for i, (a, w) in enumerate(zip(argv, want)) if i))

# ----------------------------------------------------------------- reports


def parse_report(msg, fields):
    """{field: [occurrence, ...]}; an occurrence is the text after 'FIELD:' plus the lines up to the next field."""
    got, cur = {}, None
    for line in msg.splitlines():
        m = FIELD.match(line)
        if m and m.group(1) in fields:
            cur = [m.group(2).strip()]
            got.setdefault(m.group(1), []).append(cur)
        elif cur is not None:
            cur.append(line.strip())
    return got


EXPECT = {"STATUS": "one of done, question, blocked, failed", "TASK": "T<n>, for example T1",
          "VERDICT": "PASS or FAIL", "PERSPECTIVE": "one of " + ", ".join(PERSPECTIVES),
          "ROUND": "a round number: 1, 2, ..."}       # the one-word fields and what each takes


def value(f, s):
    """The normalized value of one-word field f, or None when s is not valid for it."""
    if f == "STATUS":
        return s.lower() if s.lower() in STATUSES else None
    if f == "VERDICT":
        return s.upper() if s.upper() in VERDICTS else None
    if f == "PERSPECTIVE":
        k = re.sub(r"[\s_]+", "-", s.lower())
        return k if k in PERSPECTIVES else None
    m = re.fullmatch(r"[Tt](\d+)" if f == "TASK" else r"[Rr]?(\d+)", s)
    if not m or int(m.group(1)) == 0:
        return None
    return f"T{int(m.group(1))}" if f == "TASK" else int(m.group(1))


def check_report(msg, verifier):
    """(problems, values): what is missing or invalid, and the normalized status, task, verdict, perspective, round.
    Field order, blank lines and text before the first field do not matter; a value may go on the following lines."""
    fields = VERIFIER_FIELDS if verifier else WORKER_FIELDS
    got = parse_report(msg if isinstance(msg, str) else "", fields)
    problems, v = [], {}
    for f in fields:
        occ = got.get(f, [])
        if not occ:
            problems.append(f"{f}: missing")
        elif len(occ) > 1:
            problems.append(f"{f}: given {len(occ)} times; give it once")
        elif f in EXPECT:
            s = next((x for x in occ[0] if x), "")        # its first non-empty line
            k = value(f, s) if s else None
            if k is None:
                problems.append(f"{f}: {clip(s, 40)!r} is not {EXPECT[f]}" if s else f"{f}: empty; expected {EXPECT[f]}")
            else:
                v[f.lower()] = k
        elif not any(occ[0]):
            problems.append(f"{f}: empty; write none if there is nothing to say")
    if verifier and v.get("status", "done") != "done" and v.get("verdict") == "PASS":
        problems.append(f"VERDICT: must be FAIL when STATUS is {v['status']}")
    return problems, v


def reject(problems, verifier):
    return ("delegate: report rejected. Call SubagentHandback again with the whole report, fixed:\n- "
            + "\n- ".join(problems)
            + "\nPut each field on its own line that starts with the field name, once each (SUMMARY and OPEN in the "
              "user's language; write none for a field with nothing to say):\n"
            + (VERIFIER_FORMAT + "\nA STATUS other than done needs VERDICT: FAIL." if verifier else WORKER_FORMAT))

# ----------------------------------------------------------------- ledger rule


def matched(pairs):
    """Perspectives of the largest set of PASS verdicts with pairwise distinct perspectives and distinct agents
    (a bipartite matching over the (perspective, agent_id) pairs)."""
    agents = {}
    for p, a in pairs:
        agents.setdefault(p, set()).add(a)
    owner = {}                                    # agent_id -> perspective it counts for

    def take(p, seen):
        for a in sorted(agents[p]):
            if a not in seen:
                seen.add(a)
                if a not in owner or take(owner[a], seen):
                    owner[a] = p
                    return True
        return False

    for p in sorted(agents):
        take(p, set())
    return set(owner.values())


def last_handback(recs, task):
    """Time of the latest accepted worker hand-back (delegate:worker-*, not a verifier) for task, or None."""
    ts = [r["t"] for r in recs if r.get("ev") == "handback" and r.get("task") == task and r.get("valid") is True
          and str(r.get("agent_type") or "").startswith("delegate:worker-") and isinstance(r.get("t"), (int, float))]
    return max(ts) if ts else None


def gap(ledger, task, since=None):
    """None when the ledger supports DONE[task], else what is missing.
    Only verdicts recorded after the latest worker hand-back for the task count (since: its time, None when no worker
    handed back for it): a rework, or a conversation rewind that reuses the T number, voids the verdicts before it.
    Of the rest, the latest ROUND needs NEEDED PASS with distinct perspectives from distinct verifier agents, and no
    FAIL. A PASS from an agent that also reported in an earlier round, or before that hand-back, does not count:
    every round gets fresh verifiers."""
    rows = [r for r in ledger if r.get("task") == task and isinstance(r.get("round"), int)
            and r.get("verdict") in VERDICTS]
    fresh = [r for r in rows if since is None or (isinstance(r.get("t"), (int, float)) and r["t"] > since)]
    void = len(rows) - len(fresh)
    head = f"DONE[{task}] is not supported by the ledger:"
    note = (f" ({void} verdict(s) for {task} were recorded before its latest worker hand-back and do not count.)"
            if void else "")
    if not fresh:
        if void:
            return (f"{head} all {void} verdict(s) for {task} were recorded before its latest worker hand-back (a "
                    "rework, or a rewind that reused the number), so none counts. Verify the current result with at "
                    f"least {NEEDED} fresh verifiers (model opus or fable) on distinct perspectives "
                    f"({', '.join(BASE)}), each reporting TASK: {task}, in a new ROUND.")
        return (f"{head} no verifier verdict for {task} is recorded. Verify it first with at least {NEEDED} verifiers "
                f"(delegate:verifier-xhigh, model opus or fable), one per perspective ({', '.join(BASE)}), each "
                f"reporting TASK: {task} and ROUND: 1.")
    rnd = max(r["round"] for r in fresh)
    cur = [r for r in fresh if r["round"] == rnd]
    fails = [r for r in cur if r["verdict"] == "FAIL"]
    if fails:
        who = ", ".join(f"{r.get('perspective')} by {r.get('agent_id')}" for r in fails)
        return (f"{head} ROUND {rnd} of {task} has FAIL ({who}). Rework it, then re-verify every perspective with "
                f"fresh delegate:verifier-max agents in ROUND {rnd + 1}." + note)
    fresh_ids = {id(r) for r in fresh}
    reused = {r.get("agent_id") for r in rows if r["round"] < rnd or id(r) not in fresh_ids}
    ok = [r for r in cur if r["verdict"] == "PASS" and r.get("agent_id") not in reused]
    stale = sorted({r.get("agent_id") for r in cur if r["verdict"] == "PASS" and r.get("agent_id") in reused})
    got = matched({(r.get("perspective"), r.get("agent_id")) for r in ok})
    if len(got) >= NEEDED:
        return None
    todo = [p for p in PERSPECTIVES if p not in got]
    todo.sort(key=lambda p: p not in BASE)
    return (f"{head} ROUND {rnd} of {task} counts {len(got)} of the {NEEDED} PASS verdicts it needs; each must come "
            f"from a different verifier agent on a different perspective (counted: {', '.join(sorted(got)) or 'none'}; "
            f"PASS so far from {len({r.get('agent_id') for r in ok})} new verifier agent(s))."
            + (f" A PASS from an agent that already reported in an earlier round or before the latest worker hand-back "
               f"does not count ({', '.join(stale)})." if stale else "")
            + f" Missing: {NEEDED - len(got)} more PASS in ROUND {rnd} from fresh verifier agents, on "
              f"{', '.join(todo[:NEEDED - len(got)])}." + note)

# ----------------------------------------------------------------- modes


def h_session_start(ev):
    sid = str(ev.get("session_id") or "")
    if not is_session(ev) or not SID.fullmatch(sid):
        return None
    details = os.path.join(STATE, sid, "details")
    try:
        os.makedirs(details, exist_ok=True)
    except OSError:
        pass
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": (
        "[delegate] You are delegate:session: you judge, plan, dispatch, monitor, verify and report, and every piece "
        "of hands-on work (reading files, research, edits, commands) goes to a delegate:worker-* agent.\n"
        f"[delegate] Details folder for the T<n>-details files (the DETAILS path of every brief): {fwd(details)}\n"
        "[delegate] While any worker runs, keep the worker watchdog armed: one Monitor (timeout_ms up to 1800000) "
        "with exactly this command; the guard denies any other Monitor command. Arm it only when none is running. "
        "A watch can expire after 10-30 minutes: arm it again only after its end or expiry notice. A new watchdog "
        "replaces a running one, which then ends quietly; that end needs no new watch.\n"
        f"{watchdog_command(sid)}\n"
        "[delegate] Its only output lines are STALL <agent_id> <seconds> <where>, STALL2 <agent_id> <seconds> "
        "<where> and ACTIVE <agent_id>; it exits by itself when no worker is active.")}}


def h_guard(ev):
    if not is_session(ev):
        return None
    tool, inp, sid = str(ev.get("tool_name") or ""), tool_input(ev), str(ev.get("session_id") or "")
    if tool not in TOOLS:
        return deny(f"delegate: the session role does not use {tool or 'this tool'}. It only judges and dispatches: "
                    "hand this work to a worker (Agent tool, subagent_type delegate:worker-low, "
                    "delegate:worker-medium or delegate:worker-xhigh) and wait for its report.")
    if tool == "Monitor":
        if not is_watchdog(inp.get("command"), sid):
            return deny("delegate: the session role runs Monitor only for the worker watchdog, with exactly this "
                        f"command: {watchdog_command(sid)} -- hand any other command to a worker.")
    elif tool in ("Agent", "Task"):
        kind = str(inp.get("subagent_type") or "")
        if not kind.startswith(("delegate:worker-", "delegate:verifier-")):
            return deny(f"delegate: the session spawns only {ROLES}; this spawn has subagent_type "
                        f"{json.dumps(inp.get('subagent_type'), ensure_ascii=False)}.")
        if kind.startswith("delegate:verifier-") and inp.get("model") not in VERIFIER_MODELS:
            return deny(f"delegate: {kind} runs only with model \"opus\" or \"fable\"; this spawn has model "
                        f"{json.dumps(inp.get('model'), ensure_ascii=False)}. Spawn it again with one of them.")
    elif tool == "SendMessage":
        to, d = inp.get("to") or inp.get("recipient"), state_dir(ev)
        if isinstance(to, str) and to.strip() and d:
            recs = records(os.path.join(d, "workers.jsonl"))
            aid = resolve(recs, to)
            if aid and awaiting_end(recs, aid):
                who = aid if to.strip() == aid else f"{to.strip()} ({aid})"
                return deny(f"delegate: {who} has handed back its report, but its run has not ended yet: its task "
                            "notification has not arrived. Wait for that agent's task notification before you send "
                            "it more. A message sent now lands in the finished run, and the answer would come as "
                            "plain text that bypasses the report gate, the ledger and the watchdog.")
    return None


def resolve(recs, to):
    """The agent_id that a SendMessage target names, or None when it names no known agent, names several, or is main.
    The target is trimmed, loses a trailing " [ref]" and is compared without case. It may be an agent_id, a unique
    prefix of one with at least 7 characters (Claude Code prints "Resuming agent a41dae4"), or the name given to a
    spawn (the latest spawn with that name wins)."""
    key = REF.sub("", to.strip()).strip().lower()
    if not key or key == "main":
        return None
    ids = sorted({r["agent_id"] for r in recs if r.get("ev") == "start" and isinstance(r.get("agent_id"), str)})
    for a in ids:
        if a.lower() == key:
            return a
    if len(key) >= 7:
        hits = [a for a in ids if a.lower().startswith(key)]
        if hits:
            return hits[0] if len(hits) == 1 else None
    aid = None
    for r in recs:
        n = r.get("name")
        if r.get("ev") == "name" and isinstance(n, str) and REF.sub("", n.strip()).strip().lower() == key:
            aid = r.get("agent_id")
    return aid


def awaiting_end(recs, aid):
    """True when aid has handed back since its latest start and that run has not ended yet: no SubagentStop, task
    notification or foreground return since the hand-back. A TaskStop does not end the wait; its killed
    notification does."""
    waiting = False
    for r in recs:
        if r.get("agent_id") == aid:
            if r.get("ev") in ("start", "stop", "notified", "returned"):
                waiting = False
            elif r.get("ev") == "handback":
                waiting = True
    return waiting


def h_handback_gate(ev):
    kind = role(ev)
    if not kind or ev.get("tool_name") != "SubagentHandback":
        return None
    problems, _ = check_report(tool_input(ev).get("message"), kind == "verifier")
    return deny(reject(problems, kind == "verifier")) if problems else None


def h_handback_post(ev):
    aid, kind, d = ev.get("agent_id"), role(ev), state_dir(ev)
    if not (aid and kind and d) or ev.get("tool_name") != "SubagentHandback":
        return None
    resp = ev.get("tool_response")
    if isinstance(resp, dict) and resp.get("success") is False:
        return None
    problems, v = check_report(tool_input(ev).get("message"), kind == "verifier")
    with locked(d):
        append(os.path.join(d, "workers.jsonl"),
               {"ev": "handback", "agent_id": aid, "agent_type": ev.get("agent_type"), "task": v.get("task"),
                "status": v.get("status"), "valid": not problems})
        if kind == "verifier" and not problems:
            append(os.path.join(d, "ledger.jsonl"),
                   {"task": v["task"], "round": v["round"], "perspective": v["perspective"],
                    "verdict": v["verdict"], "agent_id": aid, "agent_type": ev.get("agent_type")})
    return None


def h_subagent_start(ev):
    aid, d = ev.get("agent_id"), state_dir(ev)
    if not (aid and role(ev) and d):
        return None
    tp = str(ev.get("transcript_path") or "")           # the main session's transcript
    tr = os.path.join(os.path.dirname(tp), ev["session_id"], "subagents", f"agent-{aid}.jsonl") if tp else None
    with locked(d):
        append(os.path.join(d, "workers.jsonl"),
               {"ev": "start", "agent_id": aid, "agent_type": ev.get("agent_type"), "transcript": tr})
    return None


def handed_back(recs, aid):
    """True when aid has handed back since its latest start."""
    done = False
    for r in recs:
        if r.get("agent_id") == aid and r.get("ev") in ("start", "handback"):
            done = r["ev"] == "handback"
    return done


def h_subagent_stop(ev):
    """SubagentStop ends a worker only once it has handed back since its latest start. Before that, Claude Code nudges
    it to deliver its report ([handback-send-enforce]) and it runs on without a new SubagentStart, so it is recorded
    as a nudge; its end then comes from the hand-back, the task notification, TaskStop or the foreground return."""
    aid, d = ev.get("agent_id"), state_dir(ev)
    if not (aid and role(ev) and d):
        return None
    with locked(d):
        path = os.path.join(d, "workers.jsonl")
        append(path, {"ev": "stop" if handed_back(records(path), aid) else "nudge", "agent_id": aid})
    return None


def h_taskstop_post(ev):
    inp, d = tool_input(ev), state_dir(ev)
    tid = inp.get("task_id") or inp.get("shell_id")
    if not (tid and d) or ev.get("tool_name") != "TaskStop" or not os.path.exists(os.path.join(d, "workers.jsonl")):
        return None
    with locked(d):
        if tid in known(d):
            append(os.path.join(d, "workers.jsonl"), {"ev": "taskstop", "agent_id": tid})
    return None


def h_agent_post(ev):
    """A foreground spawn returns (status completed) once its worker has ended, with no notification and, after a
    maxTurns stop, no SubagentStop either. A background spawn returns at once (async_launched) and ends nothing.
    The name a delegate session gives a spawn is kept, so the guard knows which agent a SendMessage to it reaches."""
    resp, d, name = ev.get("tool_response"), state_dir(ev), tool_input(ev).get("name")
    if ev.get("tool_name") not in ("Agent", "Task") or not isinstance(resp, dict) or not d:
        return None
    aid, path = resp.get("agentId"), os.path.join(d, "workers.jsonl")
    named = is_session(ev) and isinstance(name, str) and bool(name) and isinstance(aid, str) and bool(aid)
    done = resp.get("status") == "completed" and os.path.exists(path)
    if not (named or done):
        return None
    with locked(d):
        if named:
            append(path, {"ev": "name", "agent_id": aid, "name": name})
        if done and aid in known(d):
            append(path, {"ev": "returned", "agent_id": aid})
    return None


def tag(text, name):
    m = re.search(rf"<{name}>(.*?)</{name}>", text, re.S)
    return m.group(1).strip() if m else None


def stale_kill(recs, aid):
    """True when a "killed" notification about aid answers a TaskStop from before its latest start: the worker was
    resumed before the notification arrived, so the notification is about the stopped run, not the running one.
    Each TaskStop brings one killed notification; a TaskStop that one already answered does not count again."""
    start = stop = None
    answered = False
    for i, r in enumerate(recs):
        if r.get("agent_id") != aid:
            continue
        if r.get("ev") == "start":
            start = i
        elif r.get("ev") == "taskstop":
            stop, answered = i, False
        elif r.get("ev") in ("notified", "stale") and r.get("status") == "killed":
            answered = True
    return stop is not None and start is not None and stop < start and not answered


def h_prompt(ev):
    """Every way a background worker ends reaches the main session as a <task-notification> (the only sign after
    TaskStop or a maxTurns stop). Its task-id is the worker's agent_id. Monitor events and background commands are
    not workers. On such turns and on <agent-message> turns the delegate session tends to answer in English, so it
    gets a one-line reminder of the reply language."""
    p, d = str(ev.get("prompt") or ""), state_dir(ev)
    head, path = p.lstrip(), os.path.join(d, "workers.jsonl") if d else None
    if head.startswith("<task-notification>") and path and os.path.exists(path):
        with locked(d):
            recs = records(path)
            ids = {r.get("agent_id") for r in recs if r.get("ev") == "start"}
            for block in NOTE.findall(p):
                tid, status = tag(block, "task-id"), tag(block, "status")
                if tid in ids and status:
                    late = status == "killed" and stale_kill(recs, tid)
                    rec = {"ev": "stale" if late else "notified", "agent_id": tid, "status": status,
                           "summary": clip(tag(block, "summary") or "", 200)}
                    append(path, rec)
                    recs.append(rec)
    if is_session(ev) and head.startswith(("<task-notification>", "<agent-message")):
        return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": REMIND}}
    return None


def claims(msg):
    """Task numbers claimed done in msg. A line claims when, besides its marker(s) and a leading list number, it holds
    no letter or digit: only spaces, markup and punctuation (**DONE[T1]**, `DONE[T1]`, - DONE[T1], DONE[T1].,
    > DONE[T1], 1. DONE[T1], a check mark). A marker inside a sentence is a mention, not a claim."""
    out = set()
    for line in str(msg).splitlines():
        found = MARK.findall(line)
        if found and not re.search(r"[^\W_]", MARK.sub("", LISTNUM.sub("", line, count=1))):
            out.update(int(n) for n in found)
    return sorted(out)


def h_stop_gate(ev):
    """A premature claim (see claims) is blocked every time, never let through; from the (STRONG+1)th block of a task
    on, the reason tells the session to drop the line and tell the user that the verification has not passed."""
    if not is_session(ev):
        return None
    tasks = claims(ev.get("last_assistant_message") or "")
    d = state_dir(ev)
    if not tasks or not d:
        return None
    reasons = []
    with locked(d):
        ledger = records(os.path.join(d, "ledger.jsonl"))
        work = records(os.path.join(d, "workers.jsonl"))
        cpath = os.path.join(d, "counters.json")
        c = load_json(cpath)
        blocks = c.setdefault("stop_blocks", {})
        for n in tasks:
            task = f"T{n}"
            why = gap(ledger, task, last_handback(work, task))
            if not why:
                continue
            k = blocks.get(task, 0)
            blocks[task] = k + 1
            reasons.append(why if k < STRONG else
                           f"{why} This claim has been blocked {k} times already: remove the DONE[{task}] line from "
                           f"your reply and tell the user, in the user's language, that the verification of {task} "
                           "has not passed.")
        if reasons:
            save_json(cpath, c)
    if not reasons:
        return None
    return {"decision": "block",
            "reason": "delegate: " + " ".join(reasons) + " A DONE[T<n>] line counts only once the latest ROUND of "
                      f"that task, recorded after its latest worker hand-back, has {NEEDED} PASS from distinct fresh "
                      "verifiers on distinct perspectives and no FAIL."}


MODES = {"session-start": h_session_start, "guard": h_guard, "handback-gate": h_handback_gate,
         "handback-post": h_handback_post, "subagent-start": h_subagent_start, "subagent-stop": h_subagent_stop,
         "taskstop-post": h_taskstop_post, "agent-post": h_agent_post, "prompt": h_prompt, "stop-gate": h_stop_gate}

# ----------------------------------------------------------------- entry point


def log_error(mode, e):
    try:
        tb = traceback.extract_tb(e.__traceback__)
        where = f" (line {tb[-1].lineno})" if tb else ""
        os.makedirs(DATA, exist_ok=True)
        with open(os.path.join(DATA, "error.log"), "a", encoding="utf-8") as f:
            f.write(f"{now()} [{mode}] {type(e).__name__}: {e}{where}\n")
    except Exception:
        pass


def main():
    # Windows gives piped stdio the locale encoding (cp949 here); Claude Code talks UTF-8, so use bytes both ways.
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        raw = sys.stdin.buffer.read().decode("utf-8", "replace")
        try:
            ev = json.loads(raw) if raw.strip() else {}
        except ValueError:
            ev = {}
        out = MODES[mode](ev) if mode in MODES and isinstance(ev, dict) else None
        if out:
            sys.stdout.buffer.write(json.dumps(out, ensure_ascii=False).encode("utf-8"))
            sys.stdout.buffer.flush()
    except Exception as e:
        log_error(mode, e)
    sys.exit(0)   # never break the session: exit 2 would block it


if __name__ == "__main__":
    main()
