#!/usr/bin/env python3
"""
selftest.py - offline test of the delegate plugin's hooks (scripts/delegate.py) and watchdog (scripts/watchdog.py).
Python 3 standard library only; no Claude Code session and no API calls.

  python3 tests/selftest.py [--keep]

Every hook mode runs as its own process with a synthetic hook input on stdin, as Claude Code runs it, against a
temporary plugin data dir ($CLAUDE_PLUGIN_DATA) and without PYTHONUTF8/PYTHONIOENCODING, so the locale encoding
(cp949 on this machine) would show. The watchdog runs against fake worker transcripts whose mtimes the test sets.
It prints one line per failed check and a total, and exits 1 if any check failed. The temp dir is removed unless a
check failed or --keep is given.
"""
import json, os, queue, re, shlex, shutil, subprocess, sys, tempfile, threading, time

sys.stdout.reconfigure(encoding="utf-8")
TESTS = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(TESTS)
DELEGATE = os.path.join(PLUGIN, "scripts", "delegate.py")
WATCHDOG = os.path.join(PLUGIN, "scripts", "watchdog.py")
TMP = tempfile.mkdtemp(prefix="delegate-selftest-")
DATA = os.path.join(TMP, "data")
STATE = os.path.join(DATA, "state")
PROJ = os.path.join(TMP, "projects")                   # stands in for ~/.claude/projects/<slug>
ENV = {k: v for k, v in os.environ.items() if k not in ("PYTHONIOENCODING", "PYTHONUTF8")
       and not k.startswith("DELEGATE_") and not k.startswith("CLAUDE_PLUGIN")}
ENV.update(CLAUDE_PLUGIN_DATA=DATA, CLAUDE_PLUGIN_ROOT=PLUGIN)
SID = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"            # guard, session-start, gate
SID_W = "11111111-0000-4000-8000-000000000001"          # worker events and notifications
SID_L = "11111111-0000-4000-8000-000000000002"          # ledger and stop gate
SID_D = "11111111-0000-4000-8000-000000000003"          # watchdog scenario
SID_X = "11111111-0000-4000-8000-000000000004"          # watchdog idle exit
SID_I = "11111111-0000-4000-8000-000000000005"          # hooks + watchdog together
SID_E = "11111111-0000-4000-8000-000000000006"          # internal error
SID_P = "11111111-0000-4000-8000-000000000007"          # a plain session's SessionStart
SID_T = "11111111-0000-4000-8000-000000000008"          # watchdog takeover
SESSION = {"agent_type": "delegate:session"}
WORKER = {"agent_type": "delegate:worker-low", "agent_id": "a0000000000000001"}

passed, failed, bad_calls, times, procs = 0, [], [], [], []


def check(name, cond, detail=""):
    global passed
    if cond:
        passed += 1
    else:
        failed.append(name)
        print(f"FAIL {name}" + (f"  [{detail}]" if detail != "" else ""))


def run_hook(mode, data):
    t = time.time()
    p = subprocess.run([sys.executable, DELEGATE, mode], input=data, capture_output=True, env=ENV, timeout=60)
    times.append(time.time() - t)
    return p


def hook(mode, **x):
    """Run one hook mode with input x; the parsed JSON it printed, or None when it printed nothing."""
    x.setdefault("session_id", SID)
    x.setdefault("transcript_path", os.path.join(PROJ, f"{x['session_id']}.jsonl"))
    x.setdefault("cwd", TMP)
    p = run_hook(mode, json.dumps(x, ensure_ascii=False).encode("utf-8"))
    out = p.stdout.decode("utf-8")
    try:
        o = json.loads(out) if out.strip() else None
    except ValueError:
        o = "<not JSON>"
    if p.returncode != 0 or p.stderr or o == "<not JSON>" or (o is not None and not isinstance(o, dict)):
        bad_calls.append((mode, p.returncode, out[:200], p.stderr[-200:]))
    return o


def pre(tool, inp=None, **ctx):
    return hook("guard", hook_event_name="PreToolUse", tool_name=tool, tool_input=inp or {}, **ctx)


def denied(o):
    return isinstance(o, dict) and o.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


def why(o):
    try:
        return o["hookSpecificOutput"]["permissionDecisionReason"]
    except (TypeError, KeyError):
        return ""


def records(sid, name):
    try:
        with open(os.path.join(STATE, sid, name), encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
    except OSError:
        return []


def same_path(a, b):
    return bool(a and b) and os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def fw(p):
    return p.replace("\\", "/")


def gitbash(p):
    p = fw(p)
    return "/" + p[0].lower() + p[2:] if re.match(r"[A-Za-z]:/", p) else p

# ----------------------------------------------------------------- hooks.json


with open(os.path.join(PLUGIN, "hooks", "hooks.json"), encoding="utf-8") as f:
    hj = json.load(f)["hooks"]
want = {("SessionStart", None, "session-start"), ("UserPromptSubmit", None, "prompt"), ("PreToolUse", None, "guard"),
        ("PreToolUse", "SubagentHandback", "handback-gate"), ("PostToolUse", "SubagentHandback", "handback-post"),
        ("PostToolUse", "TaskStop", "taskstop-post"), ("PostToolUse", "Agent|Task", "agent-post"),
        ("SubagentStart", None, "subagent-start"),
        ("SubagentStop", None, "subagent-stop"), ("Stop", None, "stop-gate")}
got, style = set(), True
for event, groups in hj.items():
    for g in groups:
        for h in g["hooks"]:
            m = re.fullmatch(r'python3 "\$\{CLAUDE_PLUGIN_ROOT\}/scripts/delegate\.py" ([a-z-]+)', h.get("command", ""))
            style = style and bool(m) and h.get("type") == "command" and not h.get("async")
            if m:
                got.add((event, g.get("matcher"), m.group(1)))
check("hooks.json: every hook runs python3 \"${CLAUDE_PLUGIN_ROOT}/scripts/delegate.py\" <mode>, synchronously", style)
check("hooks.json: events, matchers and modes as specified", got == want, sorted(got ^ want))

# ----------------------------------------------------------------- session-start

o = hook("session-start", hook_event_name="SessionStart", source="startup", **SESSION)
ctx = (o or {}).get("hookSpecificOutput", {})
check("session-start: SessionStart additionalContext for delegate:session", ctx.get("hookEventName") == "SessionStart"
      and isinstance(ctx.get("additionalContext"), str))
text = ctx.get("additionalContext") or ""
lines = [ln for ln in text.splitlines() if ln.startswith("python3 ")]
CMD = lines[0] if len(lines) == 1 else ""
check("session-start: the context has exactly one command line", len(lines) == 1, text)
check("session-start: the command names the watchdog, the state dir and this session",
      CMD == f'python3 "{fw(WATCHDOG)}" --state-dir "{fw(STATE)}" --session {SID}', CMD)
check("session-start: a role reminder comes with it", "delegate:session" in text and "delegate:worker-*" in text
      and "STALL2 <agent_id>" in text)
DETAILS = os.path.join(STATE, SID, "details")
check("session-start: makes <state>/<sid>/details/ and names it as the folder of the T<n>-details files",
      os.path.isdir(DETAILS) and any(ln.endswith(fw(DETAILS)) and "T<n>-details" in ln for ln in text.splitlines()),
      text)
check("session-start: a watch can expire after 10-30 minutes; arm only when none runs, re-arm only after its notice",
      "10-30 minutes" in text and "only when none is running" in text and "only after its end or expiry notice" in text,
      text)
check("session-start: no details folder for a plain session", hook("session-start", session_id=SID_P,
                                                                    hook_event_name="SessionStart") is None
      and not os.path.exists(os.path.join(STATE, SID_P)))
check("session-start: after compaction too", hook("session-start", hook_event_name="SessionStart", source="compact",
                                                   **SESSION) == o)
check("session-start: nothing for a plain session", hook("session-start", hook_event_name="SessionStart") is None)
check("session-start: nothing for agent_type \"\"", hook("session-start", hook_event_name="SessionStart",
                                                          agent_type="") is None)
check("session-start: nothing with an agent_id", hook("session-start", hook_event_name="SessionStart",
                                                       agent_id="a1", **SESSION) is None)

# ----------------------------------------------------------------- guard: who it applies to

for label, ctx in (("plain session (no agent_type)", {}), ("agent_type \"\"", {"agent_type": ""}),
                   ("agent_type null", {"agent_type": None}), ("worker call (agent_id)", WORKER),
                   ("delegate:session with an agent_id", dict(SESSION, agent_id="a2")),
                   ("main thread as delegate:worker-low", {"agent_type": "delegate:worker-low"})):
    check(f"guard: leaves a {label} alone: Read", pre("Read", {"file_path": "x"}, **ctx) is None)
    check(f"guard: leaves a {label} alone: Monitor", pre("Monitor", {"command": "echo hi"}, **ctx) is None)
    check(f"guard: leaves a {label} alone: Agent general-purpose",
          pre("Agent", {"subagent_type": "general-purpose"}, **ctx) is None)

# ----------------------------------------------------------------- guard: the allowlist

for tool in ("SendMessage", "TaskStop", "ToolSearch", "AskUserQuestion", "ListAgents", "PushNotification",
             "EnterPlanMode", "ExitPlanMode", "mcp__plugin_workflowy_workflowy__create",
             "mcp__plugin_workflowy_workflowy__read", "mcp__plugin_workflowy_workflowy__close"):
    check(f"guard: allows {tool}", pre(tool, {"x": 1}, **SESSION) is None)
for tool in ("Read", "Write", "Edit", "Bash", "PowerShell", "Grep", "Glob", "WebFetch", "WebSearch", "TodoWrite",
             "NotebookEdit", "Skill", "SubagentHandback", "mcp__plugin_workflowy_workflowy__update", ""):
    o = pre(tool, {"x": 1}, **SESSION)
    check(f"guard: denies {tool or '(no tool name)'}, telling it to delegate",
          denied(o) and "worker" in why(o) and "delegate:worker-low" in why(o), why(o))

# ----------------------------------------------------------------- guard: Monitor

W, S = fw(WATCHDOG), fw(STATE)
Wb, Sb = WATCHDOG.replace("/", "\\"), STATE.replace("/", "\\")
ok_forms = {
    "the session-start command verbatim": CMD,
    "Windows backslash paths": f'python3 "{Wb}" --state-dir "{Sb}" --session {SID}',
    "Git Bash /c/ paths": f'python3 "{gitbash(W)}" --state-dir "{gitbash(S)}" --session {SID}',
    "single quotes": f"python3 '{W}' --state-dir '{S}' --session {SID}",
    "python instead of python3": f'python "{W}" --state-dir "{S}" --session {SID}',
    "a lower-case drive letter": f'python3 "{W[0].lower() + W[1:]}" --state-dir "{S}" --session {SID}',
    "extra spaces": f'python3   "{W}"  --state-dir   "{S}"   --session  {SID} ',
}
if " " not in W + S:
    ok_forms["unquoted paths"] = f"python3 {W} --state-dir {S} --session {SID}"
for label, cmd in ok_forms.items():
    check(f"guard: Monitor allowed with {label}", pre("Monitor", {"command": cmd, "description": "watchdog",
                                                                  "timeout_ms": 1800000}, **SESSION) is None, cmd)
bad_forms = {
    "; echo": CMD + "; echo hi", "&& echo": CMD + " && echo hi", "| tee": CMD + " | tee out.txt",
    "> redirect": CMD + " > out.txt", "& background": CMD + " &", "a second line": CMD + "\necho hi",
    "a command in front": "echo hi; " + CMD, "an extra argument": CMD + " --stall 1",
    "a missing --session": f'python3 "{W}" --state-dir "{S}"', "another session id": CMD.replace(SID, SID_W),
    "another script": CMD.replace("watchdog.py", "delegate.py"),
    "another state dir": CMD.replace(fw(STATE), fw(DATA)), "bash -c": f"bash -c '{CMD}'",
    "python3 -c": 'python3 -c "print(1)"', "$(...)": CMD.replace(SID, "$(whoami)"),
    "backticks": CMD + " `whoami`", "env prefix": "FOO=1 " + CMD, "not python": CMD.replace("python3", "node", 1),
}
for label, cmd in bad_forms.items():
    o = pre("Monitor", {"command": cmd}, **SESSION)
    check(f"guard: Monitor denied with {label}, quoting the allowed command", denied(o) and CMD in why(o), cmd)
check("guard: Monitor denied without a command", denied(pre("Monitor", {"description": "x"}, **SESSION)))
check("guard: Monitor denied with a non-string command", denied(pre("Monitor", {"command": ["python3", W]}, **SESSION)))

# ----------------------------------------------------------------- guard: Agent / Task spawns

for tool in ("Agent", "Task"):
    for kind, model in (("delegate:worker-low", "haiku"), ("delegate:worker-medium", "sonnet"),
                        ("delegate:worker-xhigh", "opus"), ("delegate:worker-xhigh", "fable"),
                        ("delegate:worker-low", None), ("delegate:verifier-xhigh", "opus"),
                        ("delegate:verifier-xhigh", "fable"), ("delegate:verifier-max", "opus"),
                        ("delegate:verifier-max", "fable")):
        inp = {"subagent_type": kind, "description": "T1 x", "prompt": "p", "run_in_background": True}
        if model:
            inp["model"] = model
        check(f"guard: {tool} allows {kind} with model {model}", pre(tool, inp, **SESSION) is None)
    for kind in ("general-purpose", "Explore", "delegate:session", "delegate:probe", None):
        o = pre(tool, {"subagent_type": kind, "model": "opus"} if kind else {"model": "opus"}, **SESSION)
        check(f"guard: {tool} denies subagent_type {kind}, naming the delegate roles",
              denied(o) and "delegate:worker-low" in why(o) and "delegate:verifier-max" in why(o), why(o))
    for kind, model in (("delegate:verifier-xhigh", "sonnet"), ("delegate:verifier-xhigh", "haiku"),
                        ("delegate:verifier-xhigh", None), ("delegate:verifier-max", None),
                        ("delegate:verifier-max", "claude-opus-5-5")):
        inp = {"subagent_type": kind}
        if model:
            inp["model"] = model
        o = pre(tool, inp, **SESSION)
        check(f"guard: {tool} denies {kind} with model {model}, naming opus and fable",
              denied(o) and '"opus"' in why(o) and '"fable"' in why(o), why(o))

# ----------------------------------------------------------------- hand-back gate


def wreport(status="done", task="T1", summary="설정 파일 두 개를 찾았다", artifacts="none",
            verified="ls 로 두 파일이 있는 것을 확인했다", open_="없음"):
    return (f"STATUS: {status}\nTASK: {task}\nSUMMARY: {summary}\nARTIFACTS: {artifacts}\nVERIFIED: {verified}\n"
            f"OPEN: {open_}")


def vreport(task="T1", rnd="1", persp="requirements", verdict="PASS", status="done"):
    return (wreport(status=status, task=task, summary="완료 기준을 하나씩 확인했다", verified="a.py:12, pytest 12 passed")
            + f"\nVERDICT: {verdict}\nPERSPECTIVE: {persp}\nROUND: {rnd}")


def gate(message, agent_type="delegate:worker-low", aid="a1111111111111111", tool="SubagentHandback"):
    return hook("handback-gate", hook_event_name="PreToolUse", agent_type=agent_type, agent_id=aid, tool_name=tool,
                tool_input={"message": message})


worker_ok = {
    "the template with none and Korean": wreport(),
    "multi-line values and blank lines": "STATUS: done\n\nTASK: T2\nSUMMARY: 두 파일을 고쳤다\n\n"
                                         "ARTIFACTS:\n- C:/x/a.py\n- C:/x/b.py\n\nVERIFIED:\n  pytest: 12 passed\n\n"
                                         "OPEN: none\n",
    "values on the line after the label": "STATUS:\ndone\nTASK:\nT3\nSUMMARY:\n요약\nARTIFACTS:\nnone\n"
                                          "VERIFIED:\n확인\nOPEN:\nnone",
    "text before it and a code fence": "보고합니다.\n```\n" + wreport() + "\n```",
    "indented lines and mixed case values": "  STATUS: Done\n  TASK: t3\n" + wreport().split("\n", 2)[2],
    "another field order": "\n".join(reversed(wreport().splitlines())),
    "Windows line ends": wreport().replace("\n", "\r\n"),
    "status question": wreport(status="question", open_="어느 파일을 고칠지 알려 주세요"),
    "status blocked": wreport(status="blocked"), "status failed": wreport(status="failed"),
    "extra lines that look like verifier fields": wreport() + "\nVERDICT: PASS\nnote: x",
}
for label, msg in worker_ok.items():
    check(f"handback-gate: worker report accepted: {label}", gate(msg) is None, why(gate(msg)))
worker_bad = {
    "plain text": ("PONG", ["STATUS: missing", "TASK: missing", "SUMMARY: missing", "ARTIFACTS: missing",
                            "VERIFIED: missing", "OPEN: missing"]),
    "a status outside the list": (wreport(status="finished"), ["STATUS: 'finished' is not one of"]),
    "a status with extra words": (wreport(status="done (partial)"), ["STATUS: 'done (partial)'"]),
    "a Korean status": (wreport(status="완료"), ["STATUS: '완료' is not one of"]),
    "missing TASK and OPEN": ("\n".join(ln for ln in wreport().splitlines() if not ln.startswith(("TASK", "OPEN"))),
                              ["TASK: missing", "OPEN: missing"]),
    "STATUS twice": (wreport() + "\nSTATUS: done", ["STATUS: given 2 times"]),
    "TASK not T<n>": (wreport(task="task1"), ["TASK: 'task1' is not T<n>"]),
    "TASK T0": (wreport(task="T0"), ["TASK: 'T0'"]),
    "an empty OPEN": (wreport(open_=""), ["OPEN: empty"]),
    "an empty STATUS": (wreport(status=""), ["STATUS: empty"]),
    "an empty SUMMARY followed by the next label": (wreport(summary=""), ["SUMMARY: empty"]),
}
for label, (msg, expect) in worker_bad.items():
    o = gate(msg)
    r = why(o)
    check(f"handback-gate: worker report denied: {label}", denied(o) and all(e in r for e in expect)
          and "STATUS: done | question | blocked | failed" in r and "VERDICT" not in r, r)

verifier_ok = {
    "the template": vreport(),
    "R2, 'Side Effects', lower-case pass": vreport(rnd="R2", persp="Side Effects", verdict="pass"),
    "side_effects": vreport(persp="side_effects"),
    "STATUS blocked with FAIL": vreport(status="blocked", verdict="FAIL"),
    "every perspective": vreport(persp="facts"),
}
for label, msg in verifier_ok.items():
    for kind in ("delegate:verifier-xhigh", "delegate:verifier-max"):
        o = gate(msg, agent_type=kind)
        check(f"handback-gate: {kind} report accepted: {label}", o is None, why(o))
verifier_bad = {
    "the worker format only": (wreport(), ["VERDICT: missing", "PERSPECTIVE: missing", "ROUND: missing"]),
    "an unknown perspective": (vreport(persp="security"), ["PERSPECTIVE: 'security' is not one of"]),
    "ROUND 0": (vreport(rnd="0"), ["ROUND: '0'"]), "ROUND one": (vreport(rnd="one"), ["ROUND: 'one'"]),
    "VERDICT MAYBE": (vreport(verdict="MAYBE"), ["VERDICT: 'MAYBE' is not PASS or FAIL"]),
    "STATUS blocked with PASS": (vreport(status="blocked"), ["VERDICT: must be FAIL when STATUS is blocked"]),
    "STATUS question with PASS": (vreport(status="question"), ["VERDICT: must be FAIL when STATUS is question"]),
    "VERDICT twice": (vreport() + "\nVERDICT: FAIL", ["VERDICT: given 2 times"]),
}
for label, (msg, expect) in verifier_bad.items():
    o = gate(msg, agent_type="delegate:verifier-xhigh")
    r = why(o)
    check(f"handback-gate: verifier report denied: {label}", denied(o) and all(e in r for e in expect)
          and "VERDICT: PASS | FAIL" in r and "A STATUS other than done needs VERDICT: FAIL." in r, r)
check("handback-gate: leaves other agents' hand-backs alone", gate("PONG", agent_type="general-purpose") is None)
check("handback-gate: leaves a hand-back without agent_type alone", gate("PONG", agent_type=None) is None)
check("handback-gate: ignores other tools", gate("PONG", tool="Bash") is None)

# ----------------------------------------------------------------- hand-back post and the ledger


def post(message, agent_type, aid, sid=SID, success=True, tool="SubagentHandback"):
    return hook("handback-post", session_id=sid, hook_event_name="PostToolUse", agent_type=agent_type, agent_id=aid,
                tool_name=tool, tool_input={"message": message}, tool_response={"message": message, "success": success})


check("handback-post: prints nothing", post(wreport(), "delegate:worker-medium", "aw0") is None)
hb = [r for r in records(SID, "workers.jsonl") if r.get("ev") == "handback"]
check("handback-post: a worker hand-back is recorded", len(hb) == 1 and hb[0].get("agent_id") == "aw0"
      and hb[0].get("task") == "T1" and hb[0].get("status") == "done" and hb[0].get("valid") is True
      and hb[0].get("agent_type") == "delegate:worker-medium" and isinstance(hb[0].get("t"), float), hb)
check("handback-post: a worker hand-back writes no ledger line", records(SID, "ledger.jsonl") == [])
post(vreport(task="T7", rnd="R2", persp="Side Effects", verdict="pass"), "delegate:verifier-max", "av0")
led = records(SID, "ledger.jsonl")
check("handback-post: a verifier verdict goes to the ledger, normalized",
      len(led) == 1 and {k: led[0].get(k) for k in ("task", "round", "perspective", "verdict", "agent_id",
                                                    "agent_type")}
      == {"task": "T7", "round": 2, "perspective": "side-effects", "verdict": "PASS", "agent_id": "av0",
          "agent_type": "delegate:verifier-max"} and "time" in led[0], led)
post(wreport(), "delegate:verifier-xhigh", "av1")
check("handback-post: an invalid verifier report writes no ledger line", len(records(SID, "ledger.jsonl")) == 1)
check("handback-post: ... but its hand-back is recorded as not valid",
      [r.get("valid") for r in records(SID, "workers.jsonl") if r.get("agent_id") == "av1"] == [False])
n = len(records(SID, "workers.jsonl"))
post(vreport(), "delegate:verifier-xhigh", "av2", success=False)
post(vreport(), "general-purpose", "ag0")
post(vreport(), "delegate:verifier-xhigh", "av3", tool="Bash")
check("handback-post: nothing for a failed hand-back, other agents or other tools",
      len(records(SID, "workers.jsonl")) == n and len(records(SID, "ledger.jsonl")) == 1)

# ----------------------------------------------------------------- worker events and notifications


def start(aid, kind="delegate:worker-low", sid=SID_W):
    return hook("subagent-start", session_id=sid, hook_event_name="SubagentStart", agent_id=aid, agent_type=kind)


def stop(aid, kind="delegate:worker-low", sid=SID_W):
    return hook("subagent-stop", session_id=sid, hook_event_name="SubagentStop", agent_id=aid, agent_type=kind,
                agent_transcript_path=os.path.join(PROJ, sid, "subagents", f"agent-{aid}.jsonl"),
                last_assistant_message="", stop_hook_active=False)


def notification(tid, status=None, summary="", result=""):
    return ("<task-notification>\n" f"<task-id>{tid}</task-id>\n<tool-use-id>toolu_01</tool-use-id>\n"
            + (f"<status>{status}</status>\n" if status else "") + f"<summary>{summary}</summary>\n"
            + (f"<result>{result}</result>\n" if result else "") + "</task-notification>")


def prompt(p, sid=SID_W):
    return hook("prompt", session_id=sid, hook_event_name="UserPromptSubmit", prompt=p, **SESSION)


def events(sid=SID_W):
    return [(r.get("ev"), r.get("agent_id")) + ((r.get("status"),) if r.get("ev") in ("notified", "stale") else ())
            for r in records(sid, "workers.jsonl")]


def is_reminder(o):
    c = o.get("hookSpecificOutput", {}) if isinstance(o, dict) else {}
    t = c.get("additionalContext") or ""
    return c.get("hookEventName") == "UserPromptSubmit" and "user's language" in t and "Korean" in t and "\n" not in t


o = prompt(notification("aw1", "completed"))
check("prompt: nothing recorded before any worker exists (the reminder still comes)", is_reminder(o)
      and not os.path.exists(os.path.join(STATE, SID_W)), o)
silent = [start("aw1"), start("ag1", kind="general-purpose"), start("av1", kind="delegate:verifier-xhigh")]
st = records(SID_W, "workers.jsonl")
check("subagent-start: registers delegate workers and verifiers only", events() == [("start", "aw1"),
                                                                                  ("start", "av1")], events())
check("subagent-start: records agent_type and the worker's transcript path",
      st and st[0].get("agent_type") == "delegate:worker-low"
      and same_path(st[0].get("transcript"), os.path.join(PROJ, SID_W, "subagents", "agent-aw1.jsonl")), st[:1])
silent += [stop("aw1"), stop("ag1", kind="general-purpose"), stop("aw1")]
check("subagent-stop: before any hand-back it is a nudge (Claude Code restarts the agent), not an end",
      events()[-2:] == [("nudge", "aw1"), ("nudge", "aw1")], events())
post(wreport(), "delegate:worker-low", "aw1", sid=SID_W)
silent.append(stop("aw1"))
check("subagent-stop: after a hand-back it ends the worker", events()[-2:] == [("handback", "aw1"), ("stop", "aw1")],
      events())
silent += [start("aw1"), stop("aw1")]
check("subagent-stop: a hand-back before the latest start does not count (resumed, then nudged)",
      events()[-2:] == [("start", "aw1"), ("nudge", "aw1")], events())
silent += [hook("taskstop-post", session_id=SID_W, hook_event_name="PostToolUse", tool_name="TaskStop",
                tool_input={"task_id": "av1"}, tool_response={"task_id": "av1", "task_type": "local_agent"},
                **SESSION),
           hook("taskstop-post", session_id=SID_W, hook_event_name="PostToolUse", tool_name="TaskStop",
                tool_input={"task_id": "b00mmrowd"}, **SESSION)]
check("taskstop-post: marks a known worker ended, ignores other tasks", events()[-1] == ("taskstop", "av1")
      and ("taskstop", "b00mmrowd") not in events(), events())
for a in ("aw2", "aw3", "aw4", "aw5", "aw6", "aw7"):
    silent.append(start(a))
k = len(events())
notes = [
    prompt(notification("aw1", "completed", 'Agent "T1 설정 파일 찾기 · haiku/low" finished',
                        "This agent's report was delivered to you as a message")),
    prompt(notification("aw2", "killed", 'Agent "w2" was stopped by Claude')),
    prompt(notification("aw3", "completed", 'Agent "w3" stopped at its 2-turn limit (partial result; SendMessage to '
                                            'task-id to continue)', "NOTE: this agent stopped at its 2-turn limit")),
    prompt("\n  " + notification("aw4", "completed", 'Agent "w4" finished',
                                 "The subagent ended without delivering a report through SubagentHandback")),
    prompt(notification("b00mmrowd", None, 'Monitor event: "worker watchdog"') + "<event>STALL aw5 600 model</event>"),
    prompt(notification("b00mmrowd", "completed", 'Monitor "worker watchdog" stream ended')),
    prompt(notification("butk2ygcq", "completed", 'Background command "timer" completed (exit code 0)')),
    prompt(notification("aw6", "completed", 'Agent "w6" finished') + "\n"
           + notification("aw7", "killed", 'Agent "w7" was stopped by Claude')),
]
quoted = prompt("please summarize this: " + notification("aw5", "completed", "x"))
check("prompt: parses task-id and status of worker notifications only",
      events()[k:] == [("notified", "aw1", "completed"), ("notified", "aw2", "killed"),
                       ("notified", "aw3", "completed"), ("notified", "aw4", "completed"),
                       ("notified", "aw6", "completed"), ("notified", "aw7", "killed")], events()[k:])
nt = [r for r in records(SID_W, "workers.jsonl") if r.get("ev") == "notified"]
check("prompt: keeps the summary, UTF-8 intact", nt and nt[0].get("summary") == 'Agent "T1 설정 파일 찾기 · haiku/low" '
                                                                              "finished", nt[:1])
check("prompt: every notification turn of the session gets the one-line reply-language reminder",
      all(is_reminder(x) for x in notes), notes[:1])
check("prompt: a prompt that only quotes a notification gets nothing", quoted is None, quoted)
check("subagent-start, subagent-stop, taskstop-post: print nothing", all(x is None for x in silent))
check("prompt: nothing for a plain prompt", prompt("hello") is None and events()[-1] == ("notified", "aw7", "killed"))
o = prompt('<agent-message from="aw6">\n[Subagent hand-back] The text below is the report.\n</agent-message>')
check("prompt: an agent-message turn gets the reminder too", is_reminder(o), o)
check("prompt: no reminder in a plain session or for a subagent",
      hook("prompt", session_id=SID_W, hook_event_name="UserPromptSubmit", prompt=notification("zz", "completed"))
      is None and hook("prompt", session_id=SID_W, hook_event_name="UserPromptSubmit",
                       prompt='<agent-message from="x">', agent_type="delegate:session", agent_id="a1") is None)

# foreground spawns: the Agent tool returns (status completed) only once the worker has ended


def agent_post(aid, status, tool="Agent", resp=None, name=None):
    r = {"status": status, "agentId": aid, "agentType": "delegate:worker-low", "content": [{"type": "text", "text": "."}]}
    if status == "async_launched":
        r.update(isAsync=True, outputFile="x.output")
    inp = {"subagent_type": "delegate:worker-low", "model": "haiku", "description": "T1 x", "prompt": "p",
           "run_in_background": status == "async_launched"}
    if name:
        inp["name"] = name
    return hook("agent-post", session_id=SID_W, hook_event_name="PostToolUse", tool_name=tool, tool_input=inp,
                tool_response=r if resp is None else resp, **SESSION)


quiet = [start(a) for a in ("af1", "af2", "ab1")]
k = len(events())
quiet += [agent_post("ab1", "async_launched"), agent_post("af1", "completed"),
          agent_post("af2", "completed", tool="Task"), agent_post("zz9", "completed"),
          agent_post("af1", "completed", tool="TaskStop"), agent_post("af1", "completed", resp="not a dict"),
          agent_post("af1", "completed", resp={"status": "completed"})]
check("agent-post: a completed foreground spawn ends its worker (Agent and Task); async launches, unknown agents, "
      "other tools and odd responses change nothing", events()[k:] == [("returned", "af1"), ("returned", "af2")],
      events()[k:])

# TaskStop, then a resume before the "killed" notification arrived: that notification is about the stopped run


def taskstop(aid):
    return hook("taskstop-post", session_id=SID_W, hook_event_name="PostToolUse", tool_name="TaskStop",
                tool_input={"task_id": aid}, tool_response={"task_id": aid, "task_type": "local_agent"}, **SESSION)


def killed(*aids):
    return prompt("\n".join(notification(a, "killed", f'Agent "{a}" was stopped by Claude') for a in aids))


said = []
k = len(events())
quiet += [start("ak1"), taskstop("ak1"), start("ak1")]
said += [killed("ak1"),                                          # resumed before the notification came
         killed("ak1")]                                          # then the resumed run is stopped as well
quiet += [start("ak2"), taskstop("ak2")]
said.append(killed("ak2"))                                       # the usual order
quiet += [start("ak3"), taskstop("ak3")]
said.append(killed("ak3"))
quiet += [start("ak3"), taskstop("ak3")]
said.append(killed("ak3"))
quiet += [start("ak4"), taskstop("ak4"), start("ak4")]
said.append(killed("ak4", "ak4"))                                # both in one prompt
quiet += [start("ak5"), taskstop("ak5"), start("ak5")]
said.append(prompt(notification("ak5", "completed", 'Agent "ak5" finished')))    # only "killed" can be stale
check("prompt: a killed notification that arrives after the resume is recorded as stale, not as an end",
      events()[k:] == [("start", "ak1"), ("taskstop", "ak1"), ("start", "ak1"), ("stale", "ak1", "killed"),
                       ("notified", "ak1", "killed"),
                       ("start", "ak2"), ("taskstop", "ak2"), ("notified", "ak2", "killed"),
                       ("start", "ak3"), ("taskstop", "ak3"), ("notified", "ak3", "killed"), ("start", "ak3"),
                       ("taskstop", "ak3"), ("notified", "ak3", "killed"),
                       ("start", "ak4"), ("taskstop", "ak4"), ("start", "ak4"), ("stale", "ak4", "killed"),
                       ("notified", "ak4", "killed"),
                       ("start", "ak5"), ("taskstop", "ak5"), ("start", "ak5"), ("notified", "ak5", "completed")],
      events()[k:])
check("agent-post and the stale cases: the hooks print nothing, the prompts only the reminder",
      all(x is None for x in quiet) and all(is_reminder(x) for x in said))

# the session's SendMessage to an agent that has handed back while its run has not ended yet


def send(to, field="to", **ctx):
    return pre("SendMessage", {field: to, "message": "추가 지시", "summary": "follow-up"}, session_id=SID_W,
               **(ctx if ctx else SESSION))


quiet, said = [start("as1")], []
check("guard: SendMessage to a running agent that has not handed back (a status check) is allowed",
      send("as1") is None)
post(wreport(task="T3"), "delegate:worker-low", "as1", sid=SID_W)
o = send("as1")
check("guard: SendMessage to an agent that handed back but whose run has not ended is denied",
      denied(o) and "as1" in why(o) and "task notification" in why(o), why(o))
check("guard: ... also when the target is given as recipient", denied(send("as1", field="recipient")))
for label, form in (("with spaces around it", "  as1  "), ("with a trailing [ref]", "as1 [0517f0]"),
                    ("in upper case", "AS1")):
    check(f"guard: ... and when the target is written {label}", denied(send(form)), form)
check("guard: ... but left alone in a plain session and for a worker's own SendMessage",
      send("as1", agent_type=None) is None and send("as1", **WORKER) is None)
quiet.append(stop("as1"))
check("guard: its SubagentStop after the hand-back ends the wait", send("as1") is None)
said.append(prompt(notification("as1", "completed", 'Agent "as1" finished')))
check("guard: ... and so does the task notification", send("as1") is None)
quiet.append(start("as1"))
post(wreport(task="T3"), "delegate:worker-low", "as1", sid=SID_W)
check("guard: a resumed agent that handed back again is held back again", denied(send("as1")))
quiet.append(taskstop("as1"))
check("guard: a TaskStop after the hand-back keeps the wait until the killed notification", denied(send("as1")))
said.append(killed("as1"))
check("guard: ... which then ends it", send("as1") is None)
quiet.append(start("as2"))
post(wreport(task="T4"), "delegate:worker-low", "as2", sid=SID_W)
check("guard: a foreground worker that handed back is held back ...", denied(send("as2")))
quiet.append(agent_post("as2", "completed"))
check("guard: ... until its spawn has returned", send("as2") is None)
quiet += [agent_post("as3", "async_launched", name="T5-조사"), start("as3")]
post(wreport(task="T5"), "delegate:worker-low", "as3", sid=SID_W)
o = send("T5-조사")
check("guard: a SendMessage by spawn name is resolved to that agent (and held back)", denied(o) and "as3" in why(o),
      why(o))
check("guard: spawn names match without case, trimmed and without a trailing [ref]",
      denied(send("t5-조사")) and denied(send(" T5-조사 [a1b2c3] ")))
L1, L2, L3 = "a41dae4b2c9f01234", "a41dae4b2c9f09999", "b77c0de11112222"
quiet += [start(L1), start(L2), start(L3)]
post(wreport(task="T6"), "delegate:worker-low", L1, sid=SID_W)
post(wreport(task="T7"), "delegate:worker-low", L3, sid=SID_W)
check("guard: a unique id prefix of 7+ characters, or the id in upper case, names that agent (held back)",
      denied(send("a41dae4b2c9f012")) and denied(send(L1.upper())) and denied(send("b77c0de")))
check("guard: an ambiguous prefix, a prefix under 7 characters, or a prefix of a running agent is allowed",
      send("a41dae4") is None and send("b77c0d") is None and send("a41dae4b2c9f099") is None)
check("guard: SendMessage to main (in any case) or to an unknown agent is allowed",
      send("main") is None and send("Main") is None and send("nobody") is None)
check("SendMessage window: the hooks print nothing, the notification prompts only the reminder",
      all(x is None for x in quiet) and all(is_reminder(x) for x in said))

# ----------------------------------------------------------------- stop gate


def verdict(task, rnd, persp, v, aid):
    post(vreport(task=task, rnd=str(rnd), persp=persp, verdict=v, status="done" if v == "PASS" else "failed"),
         "delegate:verifier-xhigh" if rnd == 1 else "delegate:verifier-max", aid, sid=SID_L)


def stop_gate(msg, **ctx):
    return hook("stop-gate", session_id=SID_L, hook_event_name="Stop", last_assistant_message=msg,
                stop_hook_active=False, background_tasks=[], **(ctx if ctx else SESSION))


def blocked(o):
    return isinstance(o, dict) and o.get("decision") == "block" and str(o.get("reason", "")).startswith("delegate: ")


for args in (("T1", 1, "requirements", "PASS", "v11"), ("T1", 1, "behavior", "PASS", "v12"),
             ("T1", 1, "side-effects", "PASS", "v13"),
             ("T2", 1, "requirements", "PASS", "v21"), ("T2", 1, "behavior", "PASS", "v22"),
             ("T3", 1, "requirements", "PASS", "v31"), ("T3", 1, "behavior", "PASS", "v31"),
             ("T3", 1, "side-effects", "PASS", "v32"),
             ("T4", 1, "requirements", "PASS", "v41"), ("T4", 1, "behavior", "FAIL", "v42"),
             ("T4", 1, "side-effects", "PASS", "v43"), ("T4", 2, "requirements", "PASS", "v44"),
             ("T4", 2, "behavior", "PASS", "v45"), ("T4", 2, "side-effects", "PASS", "v46"),
             ("T5", 2, "requirements", "PASS", "v51"), ("T5", 2, "behavior", "PASS", "v52"),
             ("T5", 2, "side-effects", "PASS", "v53"), ("T5", 2, "conventions", "FAIL", "v54"),
             ("T6", 1, "requirements", "PASS", "vA"), ("T6", 1, "requirements", "PASS", "vB"),
             ("T6", 1, "behavior", "PASS", "vC"), ("T6", 1, "side-effects", "PASS", "vC"),
             ("T8", 1, "requirements", "PASS", "v81"), ("T8", 1, "requirements", "PASS", "v82"),
             ("T8", 1, "requirements", "PASS", "v83"),
             ("T9", 1, "requirements", "PASS", "v91"), ("T9", 1, "behavior", "FAIL", "v92"),
             ("T9", 1, "side-effects", "PASS", "v93"), ("T9", 2, "behavior", "PASS", "v92"),
             ("T9", 2, "requirements", "PASS", "v94"), ("T9", 2, "side-effects", "PASS", "v95"),
             ("T10", 1, "requirements", "PASS", "va1"), ("T10", 1, "behavior", "PASS", "va2"),
             ("T10", 1, "side-effects", "PASS", "va3"), ("T10", 1, "conventions", "PASS", "va4")):
    verdict(*args)
check("ledger: one line per verifier verdict", len(records(SID_L, "ledger.jsonl")) == 35)
check("stop-gate: allows a reply without a marker", stop_gate("작업을 시작했습니다.") is None)
check("stop-gate: allows a DONE line with 3 PASS on distinct perspectives from distinct verifiers",
      stop_gate("완료했습니다.\nDONE[T1]") is None)
check("stop-gate: allows DONE when the latest ROUND passes after an earlier FAIL", stop_gate("DONE[T4]") is None)
check("stop-gate: allows DONE with 4 PASS (a marker line with spaces around it)",
      stop_gate("결과는 위와 같습니다.\n\n  DONE[T10]  \n") is None)
o = stop_gate("DONE[T2]")
check("stop-gate: blocks 2 PASS and says what is missing", blocked(o) and "DONE[T2]" in o["reason"]
      and "counts 2 of the 3" in o["reason"] and "side-effects" in o["reason"], o)
o = stop_gate("DONE[T3]")
check("stop-gate: blocks when one verifier passed two perspectives", blocked(o) and "counts 2 of the 3" in o["reason"],
      o)
o = stop_gate("DONE[T5]")
check("stop-gate: blocks a FAIL in the latest ROUND and names it", blocked(o) and "ROUND 2 of T5 has FAIL" in o["reason"]
      and "conventions by v54" in o["reason"] and "ROUND 3" in o["reason"], o)
o = stop_gate("DONE[T6]")
check("stop-gate: counts a matching, not raw numbers (2 agents on requirements, 1 agent on two perspectives)",
      blocked(o) and "counts 2 of the 3" in o["reason"], o)
o = stop_gate("DONE[T8]")
check("stop-gate: blocks 3 PASS on one perspective", blocked(o) and "counts 1 of the 3" in o["reason"], o)
o = stop_gate("DONE[T9]")
check("stop-gate: a verifier reused from an earlier round does not count", blocked(o)
      and "does not count (v92)" in o["reason"] and "counts 2 of the 3" in o["reason"], o)
o = stop_gate("요약은 위와 같습니다.\nDONE[T11]")
check("stop-gate: blocks a DONE line for a task without verdicts", blocked(o)
      and "no verifier verdict for T11" in o["reason"], o)
for label, m in (("inside a sentence", "모두 통과해야 `DONE[T11]`을 적겠습니다."),
                 ("with more text on its line", "DONE[T11] 입니다."), ("in lower case", "DONE[t11]"),
                 ("as the format", "완료 표시는 `DONE[T<n>]` 형식입니다."), ("after a label", "T11: DONE[T11]"),
                 ("after an English word", "Done: DONE[T11]")):
    check(f"stop-gate: a marker {label} is a mention, not a claim", stop_gate(m) is None)
for m in ("**DONE[T15]**", "`DONE[T15]`", "- DONE[T15]", "DONE[T15].", "> DONE[T15]", "✅ DONE[T15]",
          "1. DONE[T15]", "~~DONE[T15]~~", "## DONE[T15]", "_DONE[T15]_!", "결과입니다.\n\n* `DONE[T15]`"):
    o = stop_gate(m)
    check(f"stop-gate: a line with only the marker and markup is a claim: {m!r}", blocked(o)
          and "DONE[T15]" in o["reason"], o)
o = stop_gate("DONE[T15] DONE[T16]")
check("stop-gate: two markers on one line are two claims", blocked(o) and "DONE[T15] is not" in o["reason"]
      and "DONE[T16] is not" in o["reason"], o)
o = stop_gate("T1 은 끝났고 T2 는 검증 중입니다.\nDONE[T1]\nDONE[T2]")
check("stop-gate: with several marker lines, blocks for the failing ones only", blocked(o) and "DONE[T2]" in o["reason"]
      and "DONE[T1] is not" not in o["reason"], o)
check("stop-gate: leaves a plain session alone", stop_gate("DONE[T2]", agent_type=None) is None)
check("stop-gate: leaves a subagent alone", stop_gate("DONE[T2]", agent_type="delegate:session", agent_id="a1") is None)
cap = [stop_gate("정리했습니다.\nDONE[T7]") for _ in range(5)]
cnt = {}
try:
    with open(os.path.join(STATE, SID_L, "counters.json"), encoding="utf-8") as f:
        cnt = json.load(f)
except (OSError, ValueError):
    pass
check("stop-gate: blocks a premature claim every time; it never lets one through", all(blocked(x) for x in cap), cap)
check("stop-gate: from the 4th block of a task on, the reason says to drop the line and tell the user",
      all("remove the DONE[T7] line" not in x["reason"] for x in cap[:3])
      and all("blocked 3 times already" in cap[3]["reason"] and "remove the DONE[T7] line" in x["reason"]
              and "tell the user" in x["reason"] and "no verifier verdict for T7" in x["reason"] for x in cap[3:]),
      [x["reason"][:160] for x in cap[2:4]])
check("stop-gate: counts the blocks per task in counters.json", cnt.get("stop_blocks", {}).get("T7") == 5
      and cnt.get("stop_blocks", {}).get("T2") == 2 and cnt.get("stop_blocks", {}).get("T11") == 1
      and "violations" not in cnt, cnt)


def worker_done(task, aid):
    """An accepted worker hand-back for task."""
    post(wreport(task=task), "delegate:worker-medium", aid, sid=SID_L)


# rework: FAIL in ROUND 1, the worker hands back again, fresh verifiers pass ROUND 2
worker_done("T12", "w12")
for args in (("T12", 1, "requirements", "PASS", "p121"), ("T12", 1, "behavior", "FAIL", "p122"),
             ("T12", 1, "side-effects", "PASS", "p123")):
    verdict(*args)
o = stop_gate("DONE[T12]")
check("stop-gate (freshness): verdicts after the worker's hand-back count (here a FAIL)", blocked(o)
      and "ROUND 1 of T12 has FAIL" in o["reason"], o)
worker_done("T12", "w12")
o = stop_gate("DONE[T12]")
check("stop-gate (freshness): after the rework hand-back, the verdicts before it do not count", blocked(o)
      and "all 3 verdict(s) for T12 were recorded before its latest worker hand-back" in o["reason"], o)
for args in (("T12", 2, "requirements", "PASS", "q121"), ("T12", 2, "behavior", "PASS", "q122"),
             ("T12", 2, "side-effects", "PASS", "q123")):
    verdict(*args)
check("stop-gate (freshness): fresh verifiers after the rework pass it", stop_gate("DONE[T12]") is None)

# a conversation rewind that reuses T13: the abandoned branch reached ROUND 2 with PASS
worker_done("T13", "w13a")
for args in (("T13", 1, "requirements", "FAIL", "r131"), ("T13", 1, "behavior", "PASS", "r132"),
             ("T13", 1, "side-effects", "PASS", "r133"), ("T13", 2, "requirements", "PASS", "r134"),
             ("T13", 2, "behavior", "PASS", "r135"), ("T13", 2, "side-effects", "PASS", "r136")):
    verdict(*args)
check("stop-gate (freshness): the abandoned branch alone would pass", stop_gate("DONE[T13]") is None)
worker_done("T13", "w13b")
o = stop_gate("DONE[T13]")
check("stop-gate (freshness): after the rewind's new hand-back, the abandoned branch's PASS rows do not count",
      blocked(o) and "all 6 verdict(s) for T13 were recorded before its latest worker hand-back" in o["reason"], o)
verdict("T13", 1, "requirements", "PASS", "s131")
o = stop_gate("DONE[T13]")
check("stop-gate (freshness): a partial new ROUND says how many verdicts do not count", blocked(o)
      and "counts 1 of the 3" in o["reason"] and "6 verdict(s) for T13 were recorded before" in o["reason"], o)
verdict("T13", 1, "behavior", "PASS", "s132")
verdict("T13", 1, "side-effects", "PASS", "s133")
check("stop-gate (freshness): the new branch's ROUND 1 passes, though the abandoned one reached ROUND 2",
      stop_gate("DONE[T13]") is None)

# a verifier that reported before the rework hand-back does not count again, even in the same ROUND number
worker_done("T14", "w14")
for args in (("T14", 1, "requirements", "PASS", "u141"), ("T14", 1, "behavior", "PASS", "u142"),
             ("T14", 1, "side-effects", "FAIL", "u143")):
    verdict(*args)
worker_done("T14", "w14")
for args in (("T14", 1, "requirements", "PASS", "u144"), ("T14", 1, "behavior", "PASS", "u145"),
             ("T14", 1, "side-effects", "PASS", "u143")):
    verdict(*args)
o = stop_gate("DONE[T14]")
check("stop-gate (freshness): a verifier from before the latest hand-back does not count", blocked(o)
      and "does not count (u143)" in o["reason"] and "counts 2 of the 3" in o["reason"], o)
verdict("T14", 1, "side-effects", "PASS", "u146")
check("stop-gate (freshness): ... a fresh one does", stop_gate("DONE[T14]") is None)
check("stop-gate (freshness): verifier hand-backs do not reset it, and other tasks' hand-backs do not touch it",
      stop_gate("DONE[T1]\nDONE[T4]\nDONE[T10]") is None)

# ----------------------------------------------------------------- robustness

for label, data in (("invalid JSON", b"{not json"), ("empty input", b""), ("a JSON list", b"[1, 2]"),
                    ("bytes that are not UTF-8", b'{"prompt": "\xff\xfe"}')):
    p = run_hook("guard", data)
    check(f"robustness: {label} -> exit 0, no output", p.returncode == 0 and not p.stdout, p.stdout[:100])
p = run_hook("no-such-mode", json.dumps(dict(SESSION, tool_name="Read")).encode())
check("robustness: an unknown mode -> exit 0, no output", p.returncode == 0 and not p.stdout)
check("robustness: no internal error so far", not os.path.exists(os.path.join(DATA, "error.log")))
os.makedirs(STATE, exist_ok=True)
with open(os.path.join(STATE, SID_E), "w") as f:                # a file where the state dir must go
    f.write("x")
p = run_hook("subagent-start", json.dumps({"session_id": SID_E, "agent_id": "a9", "agent_type": "delegate:worker-low",
                                           "transcript_path": os.path.join(PROJ, SID_E + ".jsonl")}).encode())
err = ""
try:
    with open(os.path.join(DATA, "error.log"), encoding="utf-8") as f:
        err = f.read()
except OSError:
    pass
check("robustness: an internal error -> exit 0, no output, logged to error.log",
      p.returncode == 0 and not p.stdout and "[subagent-start]" in err and err.count("\n") == 1, err)
check("every hook call exited 0 and printed nothing or one JSON object", not bad_calls, bad_calls[:3])

# ----------------------------------------------------------------- watchdog


class Proc:
    """A watchdog process with its stdout lines collected by a thread."""

    def __init__(self, argv, env):
        self.p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.q, self.all, self.err = queue.Queue(), [], None
        procs.append(self)
        threading.Thread(target=self.pump, daemon=True).start()
        self.drainer = threading.Thread(target=self.drain, daemon=True)
        self.drainer.start()

    def drain(self):
        self.err = self.p.stderr.read()

    def pump(self):
        for raw in self.p.stdout:
            line = raw.decode("utf-8").rstrip("\r\n")
            self.all.append(line)
            self.q.put(line)

    def take(self, n, timeout):
        out, end = [], time.time() + timeout
        while len(out) < n and time.time() < end:
            try:
                out.append(self.q.get(timeout=max(0.01, end - time.time())))
            except queue.Empty:
                break
        return out

    def wait(self, timeout):
        try:
            rc = self.p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None
        self.drainer.join(5)
        return rc


EVENT = re.compile(r"(STALL|STALL2) (\S+) (\d+) (\S+)|ACTIVE (\S+)")


def parse(lines):
    out = {}
    for ln in lines:
        m = EVENT.fullmatch(ln)
        if not m:
            out[("BAD", ln)] = None
        elif m.group(5):
            out[("ACTIVE", m.group(5))] = None
        else:
            out[(m.group(1), m.group(2))] = (int(m.group(3)), m.group(4))
    return out


def expect(name, lines, want):
    """want: {(kind, aid): (seconds, where) or None for ACTIVE}; seconds may run up to 20 s over."""
    got = parse(lines)
    ok = len(lines) == len(want) and set(got) == set(want) and all(
        v is None or (got[k][1] == v[1] and v[0] <= got[k][0] <= v[0] + 20) for k, v in want.items())
    check(name, ok, lines)


def transcript(sid, aid):
    return os.path.join(PROJ, sid, "subagents", f"agent-{aid}.jsonl")


def write_tr(path, entries, mtime=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def msg(role, *blocks):
    return {"type": role, "message": {"role": role, "content": list(blocks)}}


def text(s="."):
    return msg("assistant", {"type": "text", "text": s})


def use(i, name, **inp):
    return msg("assistant", {"type": "tool_use", "id": i, "name": name, "input": inp})


def result(i):
    return msg("user", {"type": "tool_result", "tool_use_id": i, "content": "ok"})


THINK = msg("assistant", {"type": "thinking", "thinking": "..."})
ATTACH = {"type": "attachment", "attachment": {"type": "x"}}


def add_state(sid, *recs):
    d = os.path.join(STATE, sid)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "workers.jsonl"), "a", encoding="utf-8", newline="\n") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")


def started(aid, t, sid=SID_D):
    return {"ev": "start", "agent_id": aid, "agent_type": "delegate:worker-low", "transcript": transcript(sid, aid),
            "t": t}


def ended(aid, ev="stop"):
    return {"ev": ev, "agent_id": aid, "t": time.time()}


try:
    T = lambda a: transcript(SID_D, a)                                          # noqa: E731
    t0 = time.time()
    write_tr(T("A"), [msg("user", {"type": "text", "text": "brief"}), THINK, text("working"), ATTACH], t0 - 700)
    write_tr(T("B"), [use("b1", "Read", file_path="x"), result("b1")], t0 - 1000)
    write_tr(T("C"), [use("c1", "Bash", command="sleep 590", timeout=600000)], t0 - 650)
    write_tr(T("D"), [THINK, use("d1", "Read", file_path="y"), ATTACH], t0 - 650)
    write_tr(T("E"), [text()], t0 - 5000)
    write_tr(T("G"), [text()], t0 - 2000)
    write_tr(T("H"), [use("h1", "Bash", command="sleep 599", timeout=600000), ATTACH], t0 - 1030)
    write_tr(T("I"), [use("i1", "Bash", command="make", timeout=600000), result("i1"), text("done")], t0 - 650)
    write_tr(T("J"), [use("j1", "PowerShell", command="Start-Sleep 800", timeout=900000)], t0 - 900)
    write_tr(T("R"), [text()], t0 - 2000)
    write_tr(T("L"), [text()], t0 - 5000)
    write_tr(T("S"), [text()], t0 - 2000)
    write_tr(T("N"), [text()], t0 - 700)
    # E ended; L ended by the return of its foreground spawn; G and R ended and were resumed (G 5 s ago, R 700 s
    # ago), both with an old transcript; S was stopped and resumed 700 s ago, and its late killed notification is stale;
    # N stopped four times without a hand-back (nudged each time), so it is still running
    add_state(SID_D, started("A", t0 - 1000), started("B", t0 - 2000), started("C", t0 - 1000),
              started("D", t0 - 1000), started("E", t0 - 1000), ended("E"), started("F", t0 - 610),
              started("G", t0 - 3000), ended("G"), started("G", t0 - 5), started("H", t0 - 2000),
              started("I", t0 - 1000), started("J", t0 - 2000), started("R", t0 - 3000), ended("R", "notified"),
              started("R", t0 - 700), started("L", t0 - 2000), ended("L", "returned"), started("S", t0 - 3000),
              ended("S", "taskstop"), started("S", t0 - 700), dict(ended("S", "stale"), status="killed"),
              started("N", t0 - 1000), ended("N", "nudge"), ended("N", "nudge"), ended("N", "nudge"),
              ended("N", "nudge"))
    wd = Proc([sys.executable, WATCHDOG, "--state-dir", STATE, "--session", SID_D, "--interval", "0.2",
               "--idle-grace", "1"], ENV)
    expect("watchdog: STALL after 600 s, STALL2 300 s later, the Bash-timeout extension, where = tool/model/none, "
           "a resumed worker counted from its restart, returned ends a worker, stale and nudge do not",
           wd.take(11, 20) + wd.take(99, 1.5),
           {("STALL", "A"): (700, "model"), ("STALL", "B"): (1000, "model"), ("STALL2", "B"): (1000, "model"),
            ("STALL", "D"): (650, "tool=Read"), ("STALL", "F"): (610, "none"),
            ("STALL", "H"): (1030, "tool=Bash/600s"), ("STALL2", "H"): (1030, "tool=Bash/600s"),
            ("STALL", "I"): (650, "model"), ("STALL", "R"): (700, "model"), ("STALL", "S"): (700, "model"),
            ("STALL", "N"): (700, "model")})
    # C (Bash 600 s) passes its threshold max(600, 600+120); D and B move again; K starts after the watchdog did
    t1 = time.time()
    os.utime(T("C"), (t1 - 730, t1 - 730))
    write_tr(T("D"), [result("d1")])
    write_tr(T("B"), [text("again")])
    add_state(SID_D, started("K", t1 - 700))
    expect("watchdog: STALL past the Bash timeout + 120 s, ACTIVE when a stalled worker moves, a new worker is watched",
           wd.take(4, 20) + wd.take(99, 1.5),
           {("STALL", "C"): (730, "tool=Bash/600s"), ("ACTIVE", "D"): None, ("ACTIVE", "B"): None,
            ("STALL", "K"): (700, "none")})
    add_state(SID_D, ended("A"), ended("B", "handback"), ended("C", "taskstop"), ended("D", "notified"),
              ended("F"), ended("G"), ended("H"), ended("I"), ended("J"), ended("K", "notified"), ended("R"),
              ended("S", "notified"), ended("N", "handback"))
    rc = wd.wait(15)
    time.sleep(0.2)
    check("watchdog: exits 0 once no worker is active for --idle-grace", rc == 0, rc)
    check("watchdog: printed only STALL, STALL2 and ACTIVE lines (15)", len(wd.all) == 15
          and all(EVENT.fullmatch(x) for x in wd.all), wd.all)
    log = ""
    try:
        with open(os.path.join(STATE, SID_D, "watchdog.log"), encoding="utf-8") as f:
            log = f.read()
    except OSError:
        pass
    check("watchdog: logs its events to watchdog.log", "STALL A " in log and "(exit: no active worker)" in log)

    # the exact session-start command, no workers at all: idle exit
    o = hook("session-start", session_id=SID_X, hook_event_name="SessionStart", source="startup", **SESSION)
    cmd = [ln for ln in o["hookSpecificOutput"]["additionalContext"].splitlines() if ln.startswith("python3 ")][0]
    argv = shlex.split(cmd)
    check("watchdog: the session-start command is python3 <watchdog> ... (runs as given)",
          argv[0] == "python3" and same_path(argv[1], WATCHDOG))
    t2 = time.time()
    wd = Proc([sys.executable] + argv[1:], dict(ENV, DELEGATE_IDLE_GRACE="1", DELEGATE_INTERVAL="0.2"))
    rc = wd.wait(15)
    time.sleep(0.2)
    check("watchdog: with no worker at all it exits after the idle grace, silently",
          rc == 0 and time.time() - t2 < 10 and wd.all == [], (rc, wd.all, round(time.time() - t2, 1)))
    check("watchdog: stderr stays empty", rc == 0 and wd.err == b"", wd.err)

    # hooks and watchdog together: SubagentStart registers the worker, env vars shorten the thresholds
    start("ai1", kind="delegate:worker-medium", sid=SID_I)
    write_tr(transcript(SID_I, "ai1"), [text("working")])
    o = hook("session-start", session_id=SID_I, hook_event_name="SessionStart", source="startup", **SESSION)
    cmd = [ln for ln in o["hookSpecificOutput"]["additionalContext"].splitlines() if ln.startswith("python3 ")][0]
    wd = Proc([sys.executable] + shlex.split(cmd)[1:],
              dict(ENV, DELEGATE_STALL="3", DELEGATE_STALL2="3", DELEGATE_INTERVAL="0.2", DELEGATE_IDLE_GRACE="1"))
    ev1 = wd.take(2, 20)
    check("watchdog + hooks: STALL and STALL2 for a worker SubagentStart registered (thresholds from env)",
          [x.rsplit(" ", 2)[0] for x in ev1] == ["STALL ai1", "STALL2 ai1"] and all(x.endswith(" model") for x in ev1),
          ev1)
    write_tr(transcript(SID_I, "ai1"), [use("x1", "Read", file_path="z")])
    ev2 = wd.take(1, 10)
    check("watchdog + hooks: ACTIVE when its transcript changes", ev2 == ["ACTIVE ai1"], ev2)
    stop("ai1", kind="delegate:worker-medium", sid=SID_I)           # no hand-back yet: a nudge
    ev3 = wd.take(1, 10)
    check("watchdog + hooks: a SubagentStop before the hand-back keeps the worker watched (a new STALL)",
          [x.rsplit(" ", 2)[0] for x in ev3] == ["STALL ai1"] and wd.p.poll() is None, (ev3, wd.p.poll()))
    post(wreport(), "delegate:worker-medium", "ai1", sid=SID_I)
    rc = wd.wait(15)
    time.sleep(0.2)
    check("watchdog + hooks: its hand-back ends the worker, then the watchdog exits", rc == 0
          and wd.all == ev1 + ev2 + ev3, (rc, wd.all))

    # one watchdog per session: a new one takes over and the old one ends quietly
    tq = time.time()
    write_tr(transcript(SID_T, "Q"), [text()], tq - 700)
    add_state(SID_T, started("Q", tq - 1000, sid=SID_T))
    argv = [sys.executable, WATCHDOG, "--state-dir", STATE, "--session", SID_T, "--interval", "0.2",
            "--idle-grace", "1"]
    w1 = Proc(argv, ENV)
    e1 = w1.take(1, 15)
    w2 = Proc(argv, ENV)
    e2 = w2.take(1, 15)
    rc1 = w1.wait(10)
    time.sleep(1)
    check("watchdog: a new watchdog for the session takes over; the old one ends quietly (exit 0, nothing more)",
          [x.rsplit(" ", 2)[0] for x in e1] == ["STALL Q"] and rc1 == 0 and w1.all == e1 and w1.err == b"",
          (e1, rc1, w1.all, w1.err))
    check("watchdog: ... and the new one reports and runs on", [x.rsplit(" ", 2)[0] for x in e2] == ["STALL Q"]
          and w2.p.poll() is None, (e2, w2.p.poll()))
    add_state(SID_T, ended("Q", "notified"))
    rc2 = w2.wait(15)
    time.sleep(0.2)
    log = ""
    try:
        with open(os.path.join(STATE, SID_T, "watchdog.log"), encoding="utf-8") as f:
            log = f.read()
    except OSError:
        pass
    check("watchdog: the new one ends as usual; the log shows the takeover", rc2 == 0 and w2.all == e2
          and log.count("a newer watchdog took over") == 1 and "(exit: no active worker)" in log, (rc2, w2.all, log))
finally:
    for pr in procs:
        if pr.p.poll() is None:
            pr.p.kill()

# ----------------------------------------------------------------- total

ms = 1000 * sum(times) / max(1, len(times))
print(f"selftest: {passed} passed, {len(failed)} failed ({len(times)} hook calls, {ms:.0f} ms each on average)")
if failed or "--keep" in sys.argv[1:]:
    print(f"kept: {TMP}")
else:
    shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if failed else 0)
