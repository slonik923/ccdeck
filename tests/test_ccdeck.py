"""Tests for ccdeck. Run: python3 -m unittest discover -s tests -v

Each test class gets a throwaway HOME with synthetic Claude Code transcripts and a
fake `gh` on PATH, so nothing touches your real ~/.claude.
"""
import argparse
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import shlex
import shutil
import stat
import sys
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="ccdeck-test-"))
os.environ["HOME"] = str(TMP)
os.environ.pop("CLAUDE_CONFIG_DIR", None)
os.environ["CCDECK_NOTIFY"] = "0"
os.environ["CCDECK_AUTOSTART"] = "0"

FAKE_GH = TMP / "bin" / "gh"
FAKE_GH.parent.mkdir(parents=True)
FAKE_GH.write_text("""#!/usr/bin/env python3
import json, sys
if sys.argv[1:3] == ["search", "prs"]:
    direct = "user-review-requested:@me" in sys.argv  # PR 7 asked a team, not you by name
    print(json.dumps([
        {"number": 6, "title": "Colleague's change", "url": "https://github.com/me/app/pull/6", "isDraft": False,
         "repository": {"name": "app", "nameWithOwner": "me/app"}, "author": {"login": "pat"}, "updatedAt": "2026-01-01T10:00:00Z"},
        {"number": 7, "title": "Mine, reviewer added me", "url": "https://github.com/me/app/pull/7", "isDraft": False,
         "repository": {"name": "app", "nameWithOwner": "me/app"}, "author": {"login": "me"}, "updatedAt": "2026-01-01T09:00:00Z"}][:1 if direct else 2]))
    sys.exit(0)
if sys.argv[1:3] == ["api", "graphql"]:
    def node(n, **kw):
        return dict({"url": f"https://github.com/me/app/pull/{n}", "number": n, "title": f"PR {n}", "isDraft": False,
                     "updatedAt": __import__("time").strftime("%Y-%m-%dT%H:%M:%SZ", __import__("time").gmtime()),
                     "headRepository": {"nameWithOwner": "me/app"}, "headRefName": f"b{n}", "baseRefName": "main",
                     "mergeStateStatus": "CLEAN", "reviewDecision": None, "author": {"login": "me"},
                     "repository": {"nameWithOwner": "me/app", "defaultBranchRef": {"name": "main"}},
                     "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "SUCCESS"}}}]},
                     "reviewThreads": {"pageInfo": {"hasNextPage": False}, "nodes": []}}, **kw)
    said = lambda who: {"isResolved": False, "comments": {"nodes": [{"author": {"login": who}}]}}
    print(json.dumps({"data": {"viewer": {"login": "me"}, "mine": {"nodes": [
        node(7, reviewThreads={"pageInfo": {"hasNextPage": False}, "nodes": [said("me"), said("bot"), {"isResolved": True}]}),
        node(8, baseRefName="b7", commits={"nodes": [{"commit": {"statusCheckRollup": None}}]}),
        node(9, reviewThreads={"pageInfo": {"hasNextPage": True}, "nodes": []})]}}}))
    sys.exit(0)
url = sys.argv[3]
n = int(url.rsplit("/", 1)[1])
fixtures = {
    1: {"state": "OPEN", "isDraft": True, "reviewDecision": "", "mergeable": "MERGEABLE",
        "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}]},
    2: {"state": "OPEN", "isDraft": False, "reviewDecision": "", "mergeable": "MERGEABLE",
        "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "FAILURE"}]},
    3: {"state": "MERGED", "isDraft": False, "reviewDecision": "APPROVED", "mergeable": "UNKNOWN",
        "statusCheckRollup": []},
    4: {"state": "OPEN", "isDraft": False, "reviewDecision": "CHANGES_REQUESTED", "mergeable": "MERGEABLE",
        "statusCheckRollup": []},
    5: {"state": "OPEN", "isDraft": False, "reviewDecision": "", "mergeable": "MERGEABLE",
        "statusCheckRollup": [{"status": "IN_PROGRESS", "conclusion": ""}], "headRefName": "feat/x"},
    6: {"state": "OPEN", "isDraft": False, "reviewDecision": "REVIEW_REQUIRED", "mergeable": "MERGEABLE",
        "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}]},
    7: {"state": "OPEN", "isDraft": False, "reviewDecision": "APPROVED", "mergeable": "MERGEABLE",
        "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}]},
}
d = dict(fixtures[n], number=n, title=f"PR {n}")
print(json.dumps(d))
""")
FAKE_GH.chmod(FAKE_GH.stat().st_mode | stat.S_IEXEC)
os.environ["PATH"] = f"{FAKE_GH.parent}:{os.environ['PATH']}"

loader = importlib.machinery.SourceFileLoader("ccdeck", str(ROOT / "ccdeck"))
spec = importlib.util.spec_from_loader("ccdeck", loader)
cc = importlib.util.module_from_spec(spec)
loader.exec_module(cc)

PROJ = TMP / ".claude" / "projects" / "-work-app"
WORK = TMP / "work" / "app"


def ts(minutes_ago):
    t = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return t.isoformat().replace("+00:00", "Z")


def user(text, m):
    return {"type": "user", "cwd": str(WORK), "gitBranch": "feat/x", "timestamp": ts(m),
            "message": {"role": "user", "content": text}}


def asst(blocks, m, **extra):
    return dict({"type": "assistant", "cwd": str(WORK), "timestamp": ts(m),
                 "message": {"role": "assistant", "content": blocks}}, **extra)


def text(t):
    return {"type": "text", "text": t}


def tool(tid, name, inp=None):
    return {"type": "tool_use", "id": tid, "name": name, "input": inp or {}}


def result(tid, m, content="ok"):
    return user([{"type": "tool_result", "tool_use_id": tid, "content": content}], m)


def write_session(sid, entries):
    PROJ.mkdir(parents=True, exist_ok=True)
    with open(PROJ / f"{sid}.jsonl", "w") as f:
        for e in entries:
            e.setdefault("sessionId", sid)
            f.write(json.dumps(e) + "\n")


PR = "https://github.com/me/app/pull/{}"


def build_fixtures():
    write_session("s-decision", [user("fix the bug", 300), asst([text("Fixed it. Should I also update the docs?")], 299),
                                 {"type": "ai-title", "aiTitle": "Fix the bug"}])
    write_session("s-question", [user("plan it", 200), asst([tool("t1", "AskUserQuestion")], 199)])
    write_session("s-plan", [user("plan it", 190), asst([tool("t1", "ExitPlanMode")], 189)])
    write_session("s-closed", [user("refactor", 100), asst([text("Done, tests pass.")], 99)])
    write_session("s-midtask", [user("refactor", 100), asst([tool("t9", "Bash", {"command": "make"})], 99)])
    write_session("s-error", [user("go", 50), asst([text("API Error: overloaded")], 49, isApiErrorMessage=True)])
    write_session("s-pr-ready", [user("ship", 80), asst([tool("b1", "Bash", {"command": "gh pr create"})], 79),
                                 result("b1", 79, PR.format(1)), asst([text("Opened the draft PR.")], 78)])
    for sid, n in (("s-pr-failing", 2), ("s-pr-merged", 3), ("s-pr-changes", 4), ("s-pr-review", 6), ("s-pr-approved", 7)):
        write_session(sid, [user("ship", 80), asst([tool("c1", "mcp__github__create_pull_request")], 79),
                            result("c1", 79, json.dumps({"url": PR.format(n)})), asst([text("PR: " + PR.format(n))], 78)])
    # no create call seen, but the PR's branch is the session's branch
    write_session("s-pr-pending", [user("ship", 80), asst([text("PR: " + PR.format(5))], 79)])
    # only mentions someone else's failing PR: that mustn't make it "CI failing"
    write_session("s-pr-mentioned", [user("why is CI red on " + PR.format(2) + "?", 80),
                                     asst([text("It's a flaky test in " + PR.format(2) + ".")], 79)])
    write_session("s-start", [
        user("Work on https://linear.app/acme/issue/ENG-7/fix-login using https://www.notion.so/acme/Login-spec-0123456789abcdef0123456789abcdef", 70),
        asst([tool("l1", "mcp__linear__get_issue", {"id": "ENG-9"})], 69),
        result("l1", 69, json.dumps({"identifier": "ENG-9", "title": "Old login bug", "url": "https://linear.app/acme/issue/ENG-9"})),
        asst([text("See also https://www.notion.so/acme/Auth-notes-fedcba9876543210fedcba9876543210")], 68)])
    write_session("s-links", [user("doc", 60), asst([
        tool("w1", "Write", {"file_path": str(WORK / "plan.md")}),
    ], 59), result("w1", 59), asst([text("Published https://claude.ai/code/artifact/abc-123 and "
                                         "https://linear.app/acme/issue/ENG-42. All set.")], 58)])
    write_session("s-empty", [{"type": "summary", "summary": "nothing"}])
    write_session("s-garbage", [user("hi", 40), asst([text("Hello.")], 39)])
    with open(PROJ / "s-garbage.jsonl", "a") as f:
        f.write('[1,2]\n"str"\n{"type":"user","message":"oops"}\nnot json\n')


def usage_msg(mid, inp, cw, cr, out, m, model="claude-opus-5-5", blocks=None):
    return {"type": "assistant", "cwd": str(WORK), "timestamp": ts(m), "requestId": "r" + mid,
            "message": {"id": mid, "role": "assistant", "model": model, "content": blocks or [text("ok")],
                        "usage": {"input_tokens": inp, "cache_creation_input_tokens": cw,
                                  "cache_read_input_tokens": cr, "output_tokens": out}}}


def build_usage_fixture():
    write_session("s-usage", [
        user("count", 30),
        usage_msg("m1", 10, 1000, 5000, 200, 29, blocks=[tool("t1", "Read")]),
        usage_msg("m1", 10, 1000, 5000, 200, 29, blocks=[text("same message, second block")]),  # duplicate id
        result("t1", 29),
        usage_msg("m2", 5, 500, 6000, 300, 28),
    ])
    sub = PROJ / "s-usage" / "subagents"
    sub.mkdir(parents=True, exist_ok=True)
    with open(sub / "agent-x.jsonl", "w") as f:
        f.write(json.dumps(usage_msg("s1", 1, 100, 0, 50, 28)) + "\n")
    write_session("s-old-model", [user("hi", 30), usage_msg("o1", 1, 0, 150000, 10, 29, model="claude-haiku-4-5")])


def by_id(rows):
    return {r["id"]: r for r in rows}



def registry_start(pid):
    """A process's start time as Claude Code's registry (procStart) saves it: /proc's start ticks on Linux,
    otherwise `ps` lstart in UTC (ccdeck's own reader gives local time)."""
    if cc.USE_PROC:
        return cc.proc_start_time(pid)
    import subprocess
    out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True,
                         env=dict(os.environ, TZ="UTC", LC_ALL="C")).stdout
    return " ".join(out.split()) or None

class ClassifyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        build_fixtures()
        rows = cc.collect(include_all=True, days=10 ** 6)
        cc.refresh_prs([u for r in rows for u in r["prs"]], force=True)
        cls.rows = by_id(cc.collect(include_all=True, days=10 ** 6))

    def state(self, sid):
        return self.rows[sid]["state"]

    def test_needs_you_states(self):
        self.assertEqual(self.state("s-decision"), "decision")
        self.assertEqual(self.state("s-question"), "question")
        self.assertEqual(self.state("s-plan"), "plan")
        self.assertEqual(self.state("s-error"), "error")

    def test_mentioned_pr_does_not_set_state(self):
        row = self.rows["s-pr-mentioned"]
        self.assertEqual(row["state"], "closed")
        self.assertEqual([(p["number"], p["own"]) for p in row["pr_details"]], [(2, False)])
        self.assertTrue(self.rows["s-pr-pending"]["pr_details"][0]["own"])  # same branch

    def test_primary_ticket_and_page(self):
        row = self.rows["s-start"]
        lin = {r["key"]: r["primary"] for r in row["linear_list"]}
        self.assertEqual(lin, {"ENG-7": True, "ENG-9": False})
        pages = {r["title"]: r["primary"] for r in row["refs"] if r["kind"] == "notion"}
        self.assertEqual(sorted(pages.values()), [False, True])
        self.assertTrue(next(r for r in row["refs"] if "Login-spec" in r["url"])["primary"])

    def test_pr_states(self):
        self.assertEqual(self.state("s-pr-ready"), "pr_draft")
        self.assertEqual(self.state("s-pr-review"), "pr_review")
        self.assertEqual(self.state("s-pr-approved"), "pr_approved")
        self.assertEqual(self.rows["s-pr-review"]["group"], "waiting")  # waiting on reviewers, like CI
        self.assertEqual(self.rows["s-pr-ready"]["group"], "yours")
        self.assertEqual(self.rows["s-pr-pending"]["group"], "waiting")

    def test_review_requests_skip_own_prs(self):
        items = cc.fetch_review_requests()
        self.assertEqual([(r["number"], r["author"]) for r in items], [(6, "pat"), (7, "me")])
        cc.save_json(cc.REVIEWS_FILE, {"fetched": time.time(), "items": items})
        # PR 6 and 7 are both own PRs of fixture sessions; one that isn't is listed
        self.assertEqual(cc.review_requests(list(self.rows.values())), [])
        self.assertEqual([r["number"] for r in cc.review_requests([])], [6, 7])
        cc.REVIEWS_FILE.unlink()

    @mock.patch.dict(os.environ, {"CCDECK_PR_PLAN": "1", "CCDECK_REVIEW_REQUESTS": "1"})
    def test_pr_plan_parse_stack_and_owner(self):
        self.addCleanup(lambda: cc.PR_PLAN_FILE.unlink(missing_ok=True))
        self.addCleanup(lambda: cc.REVIEWS_FILE.unlink(missing_ok=True))
        plan = cc.fetch_pr_plan()
        p7, p8, p9 = plan["prs"]
        self.assertEqual((p7["threads"], p7["threads_yours"], p7["checks"]), (1, 1, "passing"))  # resolved one ignored
        self.assertEqual(p8["checks"], "none")
        self.assertTrue(p9["threads_more"])
        cc.save_json(cc.PR_PLAN_FILE, dict(plan, fetched=time.time()))
        cc.save_json(cc.REVIEWS_FILE, {"scope": "all", "items": cc.fetch_review_requests(), "viewer": plan["viewer"]})
        sess = [{"id": "abcdef123456", "peer_name": "app-1", "alive": True, "pr_details": [{"url": p8["url"], "own": True}]}]
        rows = {r["number"]: r for r in cc.pr_plan(sess)["rows"]}
        self.assertEqual((rows[7]["step"], rows[7]["texts"]), (2, ["1 review comment to answer"]))
        self.assertEqual((rows[8]["step"], rows[8]["stacked_on"]), (4, 7))  # targets PR 7's branch
        self.assertEqual(rows[8]["session"], {"id": "abcdef123456", "name": "app-1", "alive": True})
        self.assertEqual((rows[9]["step"], rows[9]["texts"]), (4, ["Review status unknown: check GitHub"]))
        self.assertEqual((rows[6]["step"], rows[6]["texts"]), (3, ["Review it"]))  # a review request, not yours
        # review requests fetched as another GitHub account aren't listed next to this account's PRs
        cc.save_json(cc.REVIEWS_FILE, {"scope": "all", "items": cc.fetch_review_requests(), "viewer": "someone-else"})
        self.assertNotIn(6, {r["number"] for r in cc.pr_plan(sess)["rows"]})
        cc.save_json(cc.REVIEWS_FILE, {"scope": "all", "items": cc.fetch_review_requests(), "viewer": plan["viewer"]})
        self.assertIn(6, {r["number"] for r in cc.pr_plan(sess)["rows"]})
        cc.save_json(cc.REVIEWS_FILE, {"scope": "all", "items": cc.fetch_review_requests()})  # whose is unknown
        self.assertNotIn(6, {r["number"] for r in cc.pr_plan(sess)["rows"]})
        # a draft owned by a session: yours to look at while it waits for you, waiting while it works
        drafts = dict(plan, prs=[dict(p8, draft=True)])
        for state, step in (("idle", 2), ("question", 2), ("working", 4)):
            with self.subTest(state=state):
                self.assertEqual(cc.pr_plan([dict(sess[0], state=state)], drafts, [])["rows"][0]["step"], step)
        # who acts is a kind; a session can be named "you" and still be the session
        failing = dict(plan, prs=[dict(p8, checks="failing")])
        row = cc.pr_plan([dict(sess[0], peer_name="you")], failing, [])["rows"][0]
        self.assertEqual((row["actor"], row["who"]), ("session", "you"))
        # the join ignores case: GitHub names are case-insensitive
        shouty = [dict(sess[0], pr_details=[{"url": p8["url"].replace("github.com/me/app", "github.com/Me/App"), "own": True}])]
        row = next(r for r in cc.pr_plan(shouty, plan, [])["rows"] if r["number"] == 8)
        self.assertEqual(row["session"]["id"], "abcdef123456")

    @mock.patch.dict(os.environ, {"CCDECK_PR_PLAN": "0"})
    def test_plan_disabled(self):
        self.assertIsNone(cc.fetch_pr_plan())
        review = {"url": "https://github.com/me/app/pull/44", "number": 44}
        authored = {"url": "https://github.com/me/app/pull/9", "repo": "me/app", "head": "feat/x", "base": "main", "default_base": "main",
                    "number": 9, "title": "Mine", "draft": False, "checks": "passing", "merge": "clean", "review": "",
                    "threads": 0, "threads_yours": 0, "updated": time.time()}
        result = cc.pr_plan([], saved={"prs": [authored]}, reviews=[review])
        self.assertTrue(result["disabled"])
        self.assertEqual([r["number"] for r in result["rows"]], [44])  # a valid authored row is held back too
        with mock.patch.dict(os.environ, {"CCDECK_PR_PLAN": "1"}):
            self.assertEqual(sorted(r["number"] for r in cc.pr_plan([], saved={"prs": [authored]}, reviews=[review])["rows"]), [9, 44])

    @mock.patch.dict(os.environ, {"CCDECK_PR_PLAN": "1"})
    def test_plan_ownership_reviews_and_forks(self):
        plan = cc.fetch_pr_plan()
        a, b, _ = plan["prs"]
        sessions = [{"id": "running", "alive": True, "branch": b["head"],
                     "pr_details": [{"url": b["url"], "source": "claude"}]},
                    {"id": "closed", "alive": False, "branch": b["head"],
                     "pr_details": [{"url": b["url"], "source": "claude"}]}]
        rows = {r["number"]: r for r in cc.pr_plan(sessions, plan, cc.fetch_review_requests())["rows"]}
        self.assertEqual(rows[8]["session"]["id"], "running")
        self.assertIn(6, rows)
        a["head_repo"] = "fork/app"
        rows = {r["number"]: r for r in cc.pr_plan(sessions, plan, [])["rows"]}
        self.assertIsNone(rows[8]["stacked_on"])
        self.assertEqual(cc.pr_plan([], {"prs": [{}]}, [])["rows"], [])

    @mock.patch.dict(os.environ, {"CCDECK_PR_PLAN": "1"})
    def test_review_thread_pagination(self):
        node = {"url": "https://github.com/me/app/pull/1", "mergeStateStatus": "CLEAN", "reviewThreads": {
            "nodes": [{"isResolved": True}] * 100,
            "pageInfo": {"hasNextPage": True, "endCursor": "next"}}}
        first = {"data": {"viewer": {"login": "me"}, "mine": {"nodes": [node]}}}
        last = {"data": {"resource": {"reviewThreads": {"nodes": [{"isResolved": True}],
                "pageInfo": {"hasNextPage": False}}}}}
        responses = [type("R", (), {"returncode": 0, "stdout": json.dumps(data)}) for data in (first, last)]
        with mock.patch.object(cc.subprocess, "run", side_effect=responses) as run:
            plan = cc.fetch_pr_plan()
        self.assertEqual(run.call_count, 2)
        self.assertFalse(plan["prs"][0]["threads_more"])
        self.assertEqual(plan["prs"][0]["threads"], 0)
        self.assertEqual(cc.pr_step(plan["prs"][0], now=1)[0], 1)
        import subprocess
        with mock.patch.object(cc.subprocess, "run", side_effect=[responses[0], subprocess.TimeoutExpired("gh", 30)]):
            incomplete = cc.fetch_pr_plan()
        self.assertTrue(incomplete["prs"][0]["threads_more"])
        self.assertEqual(cc.pr_step(incomplete["prs"][0], now=1)[0], 4)


    def test_resume_focus_and_extra_arguments(self):
        from argparse import Namespace
        session = {"id": "exact", "cwd": "", "mark": {}}
        with mock.patch.object(cc, "resolve", return_value=session), \
             mock.patch.object(cc, "peer_sessions", return_value={"exact": {"pid": 11, "pids": [11, 12]}}), \
             mock.patch.object(cc, "pid_is_headless", return_value=False), \
             mock.patch.object(cc, "focus_terminal", side_effect=[None, "tmux"]) as focus, \
             mock.patch.object(cc.os, "execv") as execv:
            cc.cmd_resume(Namespace(id="exact", extra=[]))
            self.assertEqual(focus.call_args_list, [mock.call(11), mock.call(12)])
            execv.assert_not_called()
            with mock.patch.object(cc.shutil, "which", return_value="/fake/claude"):
                cc.cmd_resume(Namespace(id="exact", extra=["--continue"]))
            execv.assert_called_once_with("/fake/claude", ["/fake/claude", "--resume", "exact", "--continue"])
        with mock.patch.object(cc, "resolve", return_value=session), \
             mock.patch.object(cc, "peer_sessions", return_value={}), \
             mock.patch.object(cc, "running_claude_processes", return_value=[(21, "/tmp", ["claude", "--resume", "exact"])]), \
             mock.patch.object(cc, "focus_terminal", return_value=None) as focus, \
             mock.patch.object(cc.os, "execv") as execv:
            with self.assertRaises(SystemExit):
                cc.cmd_resume(Namespace(id="exact", extra=[]))
            focus.assert_called_once_with(21)
            execv.assert_not_called()
        # only a `claude -p` run has it in the registry: it isn't open, so resume starts it
        with mock.patch.object(cc, "resolve", return_value=session), \
             mock.patch.object(cc, "peer_sessions", return_value={"exact": {"pid": 31, "pids": [31]}}), \
             mock.patch.object(cc, "pid_is_headless", lambda p: p == 31), \
             mock.patch.object(cc, "running_claude_processes", return_value=[]), \
             mock.patch.object(cc, "focus_terminal") as focus, \
             mock.patch.object(cc.shutil, "which", return_value="/fake/claude"), \
             mock.patch.object(cc.os, "execv") as execv:
            cc.cmd_resume(Namespace(id="exact", extra=[]))
            focus.assert_not_called()
            execv.assert_called_once_with("/fake/claude", ["/fake/claude", "--resume", "exact"])

    def test_a_config_behind_an_unreadable_link_reads_as_missing(self):
        target = cc.DATA_DIR / "odd.json"
        with mock.patch.object(Path, "exists", side_effect=PermissionError("denied")):
            self.assertEqual(cc.load_json(target, {"fallback": 1}), {"fallback": 1})

    def test_private_cache(self):
        target = TMP / "private-cache" / "plan.json"
        cc.save_json(target, {"private": True})
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    def test_pr_step(self):
        now = time.time()
        base = {"draft": False, "checks": "passing", "merge": "clean", "review": "", "base": "main",
                "threads": 0, "threads_yours": 0, "threads_more": False, "updated": now}
        old = now - 20 * 86400
        cases = [  # (changes, stacked_on, session, expected)
            ({}, None, None, (1, ["Merge it"], "you")),
            ({"threads_yours": 2, "merge": "behind"}, None, None, (1, ["Resolve 2 threads", "Update the branch", "Merge it"], "you")),
            ({"checks": "failing", "threads": 1}, None, "s1", (2, ["CI failing", "1 review comment to answer"], "session")),
            ({"merge": "dirty", "review": "changes_requested"}, None, None, (2, ["Conflicts with main", "Changes requested"], "you")),
            ({"threads_more": True}, None, None, (4, ["Review status unknown: check GitHub"], None)),  # never Merge past one page
            ({"threads_more": True, "threads": 1}, None, None, (2, ["1 review comment to answer, maybe more"], "you")),
            ({"checks": "failing"}, 9, None, (2, ["CI failing"], "you")),  # a fix comes before waiting for the base PR
            ({}, 9, None, (4, ["After #9 merges"], None)),
            ({"queue": 2}, 9, None, (4, ["In the merge queue, #2"], None)),  # queued: its base already merged in the queue's view
            ({"auto_merge": True, "merge": "blocked"}, None, None, (4, ["Merges by itself when ready (auto-merge)"], None)),
            ({"auto_merge": True, "checks": "failing"}, None, None, (2, ["CI failing"], "you")),  # auto-merge won't fix CI
            ({"checks": "pending"}, None, None, (4, ["CI running"], None)),
            ({"merge": "blocked", "threads_yours": 1}, None, None, (2, ["Resolve 1 thread", "Waiting for a review or a required check"], "you")),
            ({"merge": "blocked"}, None, None, (4, ["Waiting for a review or a required check"], None)),
            ({"merge": "blocked", "review": "review_required"}, None, None, (4, ["Waiting for a review"], None)),
            ({"merge": "unknown"}, None, None, (4, ["GitHub is still checking whether it can merge"], None)),
            ({"draft": True}, None, "s1", (2, ["Draft: look it over, then mark it ready"], "you")),  # its session waits for you
            ({"draft": True}, None, None, (2, ["Draft: look it over, then mark it ready"], "you")),
            ({"draft": True, "updated": old}, None, None, (5, ["Draft. No activity for 20 days: close or pick up"], "you")),
            ({"draft": True, "updated": old}, None, "s1", (2, ["Draft: look it over, then mark it ready"], "you")),
            ({"merge": "blocked", "updated": old}, None, None, (5, ["No activity for 20 days: close or pick up"], "you")),
            ({"merge": "dirty", "updated": old}, None, None, (5, ["No activity for 20 days: close or pick up", "Conflicts with main"], "you")),
            ({"merge": "dirty", "updated": old}, None, "s1", (2, ["Conflicts with main"], "session")),
        ]
        for changes, stacked, session, expected in cases:
            with self.subTest(changes=changes, stacked=stacked, session=session):
                self.assertEqual(cc.pr_step(dict(base, **changes), stacked, session, now), expected)
        # only a session that is working right now keeps its draft off your list
        self.assertEqual(cc.pr_step(dict(base, draft=True, checks="failing"), None, "s1", now, working=True),
                         (4, ["Draft: the session is working on it"], None))
        self.assertEqual(cc.pr_step(dict(base, draft=True), None, None, now, working=True)[0], 2)
        # every combination lands in a step with text, and only Merge, Fix and Clean up ask someone to act
        import itertools
        for draft, checks, merge, review, threads, yours, more, upd, stacked, session, working in itertools.product(
                (False, True), ("passing", "failing", "pending", "none"),
                ("clean", "blocked", "behind", "dirty", "unstable", "unknown", "has_hooks", "draft"),
                ("", "approved", "changes_requested", "review_required"), (0, 2), (0, 1), (False, True),
                (now, old), (None, 9), (None, "s1"), (False, True)):
            p = dict(base, draft=draft, checks=checks, merge=merge, review=review, threads=threads,
                     threads_yours=yours, threads_more=more, updated=upd)
            step, texts, who = cc.pr_step(p, stacked, session, now, working)
            self.assertIn(who, ("you", "session", None))
            self.assertIn(step, (1, 2, 4, 5))
            self.assertTrue(texts and all(texts))
            self.assertEqual(who is None, step == 4)

    def test_plan_reads_queue_and_checks_failing_ci(self):
        """The plan keeps merge queue, auto-merge and requested reviews; a FAILURE rollup is checked
        against the full check list, which can say it passes (fake gh: #1 passes, #2 fails)."""
        node = {"url": "https://github.com/me/app/pull/1", "number": 1, "isInMergeQueue": True, "mergeQueueEntry": {"position": 3},
                "autoMergeRequest": {"enabledAt": "2026-01-01T00:00:00Z"}, "reviewRequests": {"totalCount": 2},
                "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "FAILURE"}}}]}}
        plan = cc.parse_pr_plan({"mine": {"nodes": [node, dict(node, url="https://github.com/me/app/pull/2", isInMergeQueue=False)]}})
        one, two = plan["prs"]
        self.assertEqual((one["queue"], one["auto_merge"], one["review_requests"], two["queue"]), (3, True, 2, 0))
        cc.verify_failing(plan, time.monotonic() + 30)
        self.assertEqual((one["checks"], one["failing"]), ("passing", []))
        self.assertEqual(two["checks"], "failing")
        cc.verify_failing(plan, time.monotonic() - 1)  # out of time: what GitHub said stays
        self.assertEqual(two["checks"], "failing")

    def test_review_requests_setting(self):
        """mine leaves out team requests; a cache fetched for another setting isn't shown; off fetches nothing."""
        self.addCleanup(lambda: cc.CONFIG_FILE.unlink(missing_ok=True))
        self.addCleanup(lambda: cc.REVIEWS_FILE.unlink(missing_ok=True))
        self.assertEqual(cc.review_scope(), "all")
        cc.save_json(cc.REVIEWS_FILE, {"scope": "all", "items": cc.fetch_review_requests()})
        cc.save_json(cc.CONFIG_FILE, {"review_requests": "mine"})
        self.assertEqual(cc.review_requests([]), [])  # fetched for "all": wait for the refetch
        items = cc.fetch_review_requests()
        self.assertEqual([r["number"] for r in items], [6])
        cc.save_json(cc.REVIEWS_FILE, {"scope": "mine", "items": items})
        self.assertEqual([r["number"] for r in cc.review_requests([])], [6])
        cc.save_json(cc.CONFIG_FILE, {"review_requests": "off"})
        self.assertIsNone(cc.fetch_review_requests())
        for bad in ("bogus", [], {}, ["mine"], 1):  # lists and dicts aren't hashable: they must not crash
            cc.save_json(cc.CONFIG_FILE, {"review_requests": bad})
            self.assertEqual(cc.review_scope(), "all", bad)
        with mock.patch.dict(os.environ, {"CCDECK_REVIEW_REQUESTS": "0"}):
            self.assertEqual(cc.review_scope(), "off")
        self.assertEqual(self.state("s-pr-failing"), "ci_failing")
        self.assertEqual(self.state("s-pr-merged"), "pr_merged")
        self.assertEqual(self.state("s-pr-changes"), "changes_requested")
        self.assertEqual(self.state("s-pr-pending"), "ci_pending")
        pr = self.rows["s-pr-ready"]["pr_details"][0]
        self.assertEqual((pr["number"], pr["draft"], pr["checks"]), (1, True, "passing"))

    def test_closed_states(self):
        self.assertEqual(self.state("s-closed"), "closed")
        self.assertEqual(self.state("s-midtask"), "closed_midtask")
        self.assertEqual(self.state("s-garbage"), "closed")

    def test_gh_stack_submit_prs(self):
        """`gh stack submit` prints PR numbers without links: the repo comes from the folder's git remote."""
        out = ("Pushing 3 branches...\n✓ Created PR #12 for feat/b\nPR #11 for feat/a is up to date\n"
               "Linked the stack on GitHub\nSee PR #9 for feat/z earlier")  # a sentence isn't a submit line
        write_session("s-stack", [user("submit the stack", 80), asst([tool("g1", "Bash", {"command": "gh stack submit --auto"})], 79),
                                  result("g1", 79, out), asst([text("Submitted.")], 78)])
        write_session("s-stack-view", [user("show it", 80), asst([tool("g2", "Bash", {"command": "gh pr list"})], 79),
                                       result("g2", 79, "✓ Created PR #13 for feat/c"), asst([text("Listed.")], 78)])
        info = cc.parse_transcript(PROJ / "s-stack.jsonl")
        other = cc.parse_transcript(PROJ / "s-stack-view.jsonl")
        self.assertEqual(info["prs"], [])  # numbers only: links come from the folder's remote, on every scan
        remotes = {str(WORK): "git@github.com:acme/app.git\n", "/elsewhere": "https://github.com/acme/other\n"}
        failing = [True]
        def git(argv, **kw):
            if failing[0]:
                raise cc.subprocess.TimeoutExpired(argv, 3)
            return type("R", (), {"stdout": remotes.get(argv[2], "")})
        cc._repo_cache.clear()
        self.addCleanup(cc._repo_cache.clear)
        with mock.patch.object(cc.subprocess, "run", side_effect=git), mock.patch.object(cc.time, "time", return_value=1000.0):
            self.assertEqual(cc.with_stack_prs(info)["prs"], [])  # git timed out
            failing[0] = False
            self.assertEqual(cc.with_stack_prs(info)["prs"], [])  # a failure is asked again only after a minute
        with mock.patch.object(cc.subprocess, "run", side_effect=git), mock.patch.object(cc.time, "time", return_value=1061.0):
            info = cc.with_stack_prs(info)
        self.assertEqual(sorted(info["prs"]), ["https://github.com/acme/app/pull/11", "https://github.com/acme/app/pull/12"])
        self.assertEqual(set(info["pr_sources"].values()), {"created"})
        # each result keeps the folder it ran in
        moved = dict(result("g3", 70, "✓ Created PR #4 for feat/d"), cwd="/elsewhere")
        write_session("s-two-repos", [user("submit", 80), asst([tool("g1", "Bash", {"command": "gh stack submit"})], 79), result("g1", 79, out),
                                      asst([tool("g3", "Bash", {"command": "cd /elsewhere && gh stack submit"})], 71), moved])
        with mock.patch.object(cc.subprocess, "run", side_effect=git):
            two = cc.with_stack_prs(cc.parse_transcript(PROJ / "s-two-repos.jsonl"))
        self.assertEqual(sorted(two["prs"]), ["https://github.com/acme/app/pull/11", "https://github.com/acme/app/pull/12",
                                              "https://github.com/acme/other/pull/4"])
        # one command, two folders: its number-only lines can't be split between them, so they're skipped
        both = "cd /elsewhere && gh stack submit; cd " + str(WORK) + " && gh stack submit"
        write_session("s-both", [user("submit both", 80), asst([tool("g4", "Bash", {"command": both})], 79),
                                 result("g4", 79, "✓ Created PR #5 for feat/e\n✓ Created PR #6 for feat/f"), asst([text("Done.")], 78)])
        with mock.patch.object(cc.subprocess, "run", side_effect=git):
            self.assertEqual(cc.with_stack_prs(cc.parse_transcript(PROJ / "s-both.jsonl"))["prs"], [])
        # cd -P resolves the link before `..`; plain cd collapses `..` as typed
        with tempfile.TemporaryDirectory() as root:
            root = os.path.realpath(root)
            (Path(root) / "repos" / "app" / "subdir").mkdir(parents=True)
            (Path(root) / "work").mkdir()
            (Path(root) / "work" / "link").symlink_to(Path(root) / "repos" / "app" / "subdir")
            self.assertEqual(cc.gh_stack_dirs(f"cd -P {root}/work/link/.. && gh stack submit", "/x"), [f"{root}/repos/app"])
            self.assertEqual(cc.gh_stack_dirs(f"cd {root}/work/link/.. && gh stack submit", "/x"), [f"{root}/work"])
            self.assertEqual(cc.gh_stack_dirs(f"cd -P -L {root}/work/link/.. && gh stack submit", "/x"), [f"{root}/work"])
            self.assertEqual(cc.gh_stack_dirs(f"cd -PL {root}/work/link/.. && gh stack submit", "/x"), [f"{root}/work"])
            self.assertEqual(cc.gh_stack_dirs(f"cd -LP {root}/work/link/.. && gh stack submit", "/x"), [f"{root}/repos/app"])
        # two spellings of one folder (a symlink) are one folder
        with tempfile.TemporaryDirectory() as root:
            real = Path(root) / "repo"
            real.mkdir()
            (Path(root) / "link").symlink_to(real)
            twice = f"cd {real} && gh stack submit; cd {Path(root) / 'link'} && gh stack submit"
            write_session("s-twice", [user("submit", 80), asst([tool("g5", "Bash", {"command": twice})], 79),
                                      result("g5", 79, "✓ Created PR #7 for feat/g"), asst([text("Done.")], 78)])
            self.assertEqual(cc.parse_transcript(PROJ / "s-twice.jsonl")["stack_prs"], [[str(real), 7]])
        for cmd, dirs in (("gh stack submit", ["/w/a"]), ("cd /w/b && gh stack submit", ["/w/b"]), ("cd sub && gh stack push", ["/w/a/sub"]),
                          ("cd /w/a && gh stack submit; cd /w/b && gh stack submit", ["/w/a", "/w/b"]), ("cd - && gh stack submit", [None]),
                          ("(cd /w/b && make)\ngh stack submit", ["/w/a"]), ("(cd /w/b && gh stack submit)", ["/w/b"]),
                          ("echo '('; cd /w/b; echo ')'; gh stack submit", ["/w/b"]),  # quoted parentheses are text
                          ("(cd /w/b && gh stack submit); gh stack submit", ["/w/b", "/w/a"]),  # `);` is two operators
                          ('cd "/w/my dir" && gh stack submit', ["/w/my dir"]), ("echo 'unclosed", []),
                          ("gh stack submit # don't wait", ["/w/a"]), ("echo a#b && gh stack push", ["/w/a"]),  # comments
                          ("gh \\\n  stack submit", ["/w/a"]),  # a backslash-newline joins the lines
                          ("cd -P /w/b && gh stack submit", ["/w/b"]), ("cd -- /w/b && gh stack submit", ["/w/b"])):
            with self.subTest(cmd=cmd):
                self.assertEqual(cc.gh_stack_dirs(cmd, "/w/a"), dirs)
        self.assertEqual(other["prs"], [])  # only gh stack's own output counts
        # Claude Code's pr-link entries name the session's PRs with links
        write_session("s-linked", [user("ship", 80), asst([text("Opened it.")], 79),
                                   {"type": "pr-link", "prNumber": 5, "prUrl": PR.format(5), "prRepository": "me/app"},
                                   {"type": "pr-link", "prUrl": "javascript:alert(1)"}, {"type": "pr-link", "prUrl": 7}])
        linked = cc.parse_transcript(PROJ / "s-linked.jsonl")
        self.assertEqual((linked["prs"], linked["pr_sources"]), ([PR.format(5)], {PR.format(5): "created"}))
        # only gh stack itself running counts: quoted text that names it prints something else
        for cmd, runs in (("gh stack submit --auto", True), ("cd /w && gh stack push", True), ("git status\ngh stack view", True),
                          ("CI=1 GH_HOST=github.com gh stack submit --auto", True), ("echo X=1 gh stack submit", False),
                          ("printf '%s\\n' 'gh stack submit' 'https://github.com/other/repo/pull/12'", False),
                          ("echo gh stack submit", False), ("echo 'unbalanced", False), ("gh pr view 12", False)):
            with self.subTest(cmd=cmd):
                self.assertEqual(cc.runs_gh_stack(cmd), runs)
        quoted = "printf '%s\\n' 'gh stack submit' 'https://github.com/other/repo/pull/12'"
        write_session("s-quoted", [user("print it", 80), asst([tool("q1", "Bash", {"command": quoted})], 79),
                                   result("q1", 79, "gh stack submit\nhttps://github.com/other/repo/pull/12"), asst([text("Done.")], 78)])
        self.assertEqual(cc.parse_transcript(PROJ / "s-quoted.jsonl")["pr_sources"], {})
        # an inherited GIT_DIR doesn't make one folder answer with another repo's remote
        with tempfile.TemporaryDirectory() as root:
            clean = {k: v for k, v in os.environ.items() if k not in cc.GIT_LOCATION_VARS}  # set up a and b, not the caller's repo
            for name in ("a", "b"):
                d = Path(root) / name
                d.mkdir()
                subprocess.run(["git", "init", "-q", str(d)], check=True, env=clean)
                subprocess.run(["git", "-C", str(d), "remote", "add", "origin", f"git@github.com:acme/{name}.git"], check=True, env=clean)
            cc._repo_cache.clear()
            with mock.patch.dict(os.environ, {"GIT_DIR": str(Path(root) / "b" / ".git")}):
                self.assertEqual(cc.github_repo(str(Path(root) / "a")), "acme/a")
            cc._repo_cache.clear()
        for remote, repo in (("https://github.com/acme/app.git", "acme/app"), ("https://github.com/acme/my.app", "acme/my.app"),
                             ("ssh://git@github.com/acme/app.git", "acme/app"), ("https://token@github.com/acme/app", "acme/app"),
                             ("https://notgithub.com/acme/app.git", None), ("git@notgithub.com:acme/app.git", None),
                             ("git@gitlab.com:acme/app.git", None), ("", None)):
            with self.subTest(remote=remote):
                m = cc.GITHUB_REMOTE_RE.fullmatch(remote)
                self.assertEqual(m.group(1) if m else None, repo)

    def test_compact_summary_isnt_a_prompt(self):
        """After /compact the session still waits for you: its summary isn't a question to answer."""
        summary = dict(user("This session is being continued from a previous conversation. See " + PR.format(9), 40), isCompactSummary=True)
        write_session("s-compacted", [user("ship it", 80), asst([text("Shipped.")], 79), summary])
        info = cc.parse_transcript(PROJ / "s-compacted.jsonl")
        self.assertEqual((info["last_role"], info["last_text"], info["prompts"]), ("assistant", "Shipped.", 2))
        self.assertEqual(info["prs"], [])  # links in the summary aren't ones you just shared
        refs = ("Spec https://www.notion.so/acme/Email-spec-" + "c" * 32 + ", ticket https://linear.app/acme/issue/ENG-7/queue, also ENG-9")
        noted = lambda i: (len(i["refs"]), sorted(i["linear"]), i["issue_keys"])
        write_session("s-compacted-refs", [user("ship it", 80), asst([text("Shipped.")], 79),
                                           dict(user("Continued. " + refs, 40), isCompactSummary=True)])
        self.assertEqual(noted(cc.parse_transcript(PROJ / "s-compacted-refs.jsonl")), (0, [], []))
        write_session("s-prompt-refs", [user("ship it", 80), asst([text("Shipped.")], 79), user(refs, 40)])  # the same text as a prompt
        self.assertEqual(noted(cc.parse_transcript(PROJ / "s-prompt-refs.jsonl")), (1, ["ENG-7"], ["ENG-9"]))
        self.assertEqual(cc.classify(info, None, True), "idle")
        write_session("s-continued", [summary])  # a session continued in a new file is still listed
        self.assertEqual(cc.parse_transcript(PROJ / "s-continued.jsonl")["prompts"], 1)

    def test_links_and_files(self):
        s = self.rows["s-links"]
        self.assertEqual(s["links"]["artifact"], ["https://claude.ai/code/artifact/abc-123"])
        self.assertEqual(s["links"]["linear"], ["https://linear.app/acme/issue/ENG-42"])
        self.assertEqual(s["files"], [str(WORK / "plan.md")])

    def test_empty_session_skipped_and_title(self):
        self.assertNotIn("s-empty", self.rows)
        self.assertEqual(self.rows["s-decision"]["display_title"], "Fix the bug")

    def test_default_list_hides_done(self):
        cc.apply_action("s-closed", "done")
        try:
            ids = {r["id"] for r in cc.collect()}
            self.assertNotIn("s-closed", ids)
            self.assertNotIn("s-pr-merged", ids)
        finally:
            cc.apply_action("s-closed", "undone")

    def test_park(self):
        cc.apply_action("s-closed", "park")
        try:
            self.assertEqual(by_id(cc.collect(True, 10 ** 6))["s-closed"]["state"], "parked")
        finally:
            cc.apply_action("s-closed", "unpark")


class PrCommandTest(unittest.TestCase):
    def run_ccdeck(self, *args, code=None):
        import subprocess
        with tempfile.TemporaryDirectory(prefix="ccdeck-command-") as home:
            env = dict(os.environ, HOME=home, CLAUDE_CONFIG_DIR=home + "/.claude",
                       CCDECK_PR_PLAN="1", CCDECK_REVIEW_REQUESTS="1", CCDECK_AUTOSTART="0")
            command = [sys.executable, "-c", code] if code else [sys.executable, str(ROOT / "ccdeck"), *args]
            return subprocess.run(command, capture_output=True, text=True, env=env, timeout=30)

    def test_prs_prints_plan_and_review_requests(self):
        result = self.run_ccdeck("prs")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PR 7", result.stdout)
        self.assertIn("Colleague's change", result.stdout)

    def test_command_paths_with_controlled_dependencies(self):
        setup = ("import runpy, sys; from unittest.mock import patch; "
                 "from argparse import Namespace; "
                 "m=runpy.run_path(" + repr(str(ROOT / "ccdeck")) + "); "
                 "g=m['cmd_prs'].__globals__; ")
        cases = [
            ("with patch.dict(g, save_json=lambda *a: (_ for _ in ()).throw(OSError('read only'))):\n"
             "    g['cmd_prs'](Namespace(sessions=False))", "PR 7", ""),
            ("with patch.dict(g, fetch_pr_plan=lambda: None):\n"
             "    g['cmd_prs'](Namespace(sessions=False))", "Colleague's change", "unavailable"),
            ("s={'id':'registered', 'cwd':'', 'mark':{}}\n"
             "with patch.dict(g, resolve=lambda ref:s, peer_sessions=lambda:{'registered':{'pid':77}}, "
             "focus_terminal=lambda pid:'test', **{}), patch.object(g['os'], 'execv', side_effect=AssertionError('must not exec')):\n"
             "    g['cmd_resume'](Namespace(id='registered', extra=[]))", "Switched", ""),
            ("s={'id':'registered', 'cwd':'', 'mark':{}}\n"
             "with patch.dict(g, resolve=lambda ref:s, peer_sessions=lambda:{'registered':{'pid':77}}), "
             "patch.object(g['shutil'], 'which', return_value='/fake/claude'), "
             "patch.object(g['os'], 'execv', side_effect=lambda *a:print('EXEC',a)):\n"
             "    g['cmd_resume'](Namespace(id='registered', extra=['--verbose']))", "EXEC", "")]
        for code, output, warning in cases:
            with self.subTest(output=output, warning=warning):
                result = self.run_ccdeck(code=setup + "\n" + code)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(output, result.stdout)
                self.assertIn(warning, result.stderr)

    def test_cli_uses_only_invocation_results(self):
        from argparse import Namespace
        import contextlib
        import io
        stale = {"url": "https://github.com/old/app/pull/99", "number": 99, "title": "old account"}
        for plan in (None, {"prs": [], "viewer": "new"}):
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(cc, "fetch_pr_plan", return_value=plan), \
                 mock.patch.object(cc, "fetch_review_requests", return_value=[]), \
                 mock.patch.object(cc, "collect", return_value=[]), \
                 mock.patch.object(cc, "load_json", return_value={"prs": [stale], "items": [stale]}), \
                 mock.patch.object(cc, "save_json", side_effect=AssertionError("CLI must not write shared caches")), \
                 mock.patch.dict(os.environ, {"CCDECK_PR_PLAN": "1"}), \
                 contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                cc.cmd_prs(Namespace(sessions=False))
            self.assertNotIn("old account", out.getvalue())
            if plan is None:
                self.assertIn("unavailable", err.getvalue())
                self.assertNotIn("No open PRs", out.getvalue())
            else:
                self.assertIn("No open PRs", out.getvalue())

    def test_plan_polling_is_independent(self):
        dash = cc.Dashboard.__new__(cc.Dashboard)
        dash.wake = threading.Event()
        dash.lock = threading.Lock()
        dash.snapshot = {"sessions": []}
        with mock.patch.object(cc, "refresh_pr_plan", return_value={"prs": []}) as plan, \
             mock.patch.object(cc.time, "sleep", side_effect=StopIteration), \
             mock.patch.object(cc, "fetch_review_requests", return_value=[]), \
             mock.patch.object(cc, "save_json"):
            with self.assertRaises(StopIteration):
                dash.plan_loop()
            self.assertTrue(dash.wake.is_set())
            plan.assert_called_once()
            plan.reset_mock()
            with self.assertRaises(StopIteration):
                dash.pr_loop()
            plan.assert_not_called()

    def test_resume_uses_only_verified_interactive_hook(self):
        from argparse import Namespace
        session = {"id": "hook-session", "cwd": "", "mark": {}}
        for changes, focused in (({}, True), ({"headless": True}, False), ({"status": "ended"}, False)):
            hook = dict({"pid": 71, "pid_start": 123, "status": "idle"}, **changes)
            with mock.patch.object(cc, "resolve", return_value=session), \
                 mock.patch.object(cc, "peer_sessions", return_value={}), \
                 mock.patch.object(cc, "running_claude_processes", return_value=[]), \
                 mock.patch.object(cc, "load_states", return_value={session["id"]: hook}), \
                 mock.patch.object(cc, "pid_alive", return_value=True), \
                 mock.patch.object(cc, "focus_terminal", return_value=None) as focus, \
                 mock.patch.object(cc.os, "execv") as execv, \
                 mock.patch.object(cc.shutil, "which", return_value="/fake/claude"):
                if focused:
                    with self.assertRaises(SystemExit):
                        cc.cmd_resume(Namespace(id=session["id"], extra=[]))
                    focus.assert_called_once_with(71)
                    execv.assert_not_called()
                else:
                    cc.cmd_resume(Namespace(id=session["id"], extra=[]))
                    focus.assert_not_called()
                    execv.assert_called_once()

    @mock.patch.dict(os.environ, {"CCDECK_PR_PLAN": "1"})
    def test_thread_pages_share_one_time_budget(self):
        """Slow extra pages stop at the budget: the PRs not read stay "maybe more", never Merge."""
        node = lambda n: {"url": f"https://github.com/me/app/pull/{n}", "number": n, "mergeStateStatus": "CLEAN",
                          "reviewThreads": {"nodes": [], "pageInfo": {"hasNextPage": True, "endCursor": "c"}}}
        first = {"data": {"viewer": {"login": "me"}, "mine": {"nodes": [node(n) for n in range(5)]}}}
        clock = [0.0]
        timeouts = []
        def run(argv, **kw):
            if "-f" in argv and any(a.startswith("cursor=") for a in argv):
                timeouts.append(kw["timeout"])
                clock[0] += 30  # each page times out after its full wait
                raise cc.subprocess.TimeoutExpired(argv, kw["timeout"])
            return type("R", (), {"returncode": 0, "stdout": json.dumps(first), "stderr": ""})
        with mock.patch.object(cc.shutil, "which", return_value="/fake/gh"), \
             mock.patch.object(cc.subprocess, "run", side_effect=run), \
             mock.patch.object(cc.time, "monotonic", lambda: clock[0]):
            plan = cc.fetch_pr_plan()
        self.assertEqual(timeouts, [30, 15])  # 45 s in all, not 30 s per PR
        self.assertEqual(len(plan["prs"]), 5)  # every row is still listed
        self.assertTrue(all(p["threads_more"] for p in plan["prs"]))
        self.assertTrue(all(cc.pr_step(p, now=1)[0] != 1 for p in plan["prs"]))

    def test_refresh_is_ready_only_once_saved(self):
        with tempfile.TemporaryDirectory() as root:
            data = Path(root)
            with mock.patch.object(cc, "DATA_DIR", data), \
                 mock.patch.object(cc, "PR_PLAN_FILE", data / "pr-plan.json"), \
                 mock.patch.object(cc, "fetch_pr_plan", return_value={"prs": [], "viewer": "me"}), \
                 mock.patch.dict(os.environ, {"CCDECK_PR_PLAN": "1"}):
                with mock.patch.object(cc, "save_json", side_effect=OSError("read-only")):
                    self.assertIsNotNone(cc.refresh_pr_plan())  # the CLI still gets the fresh plan
                    self.assertEqual(cc._plan_state, "failed")  # the dashboard, reading the file, doesn't
                cc.refresh_pr_plan()
                self.assertEqual(cc._plan_state, "ready")

    def test_refresh_serializes_observations(self):
        with tempfile.TemporaryDirectory() as root:
            data = Path(root)
            entered, release = threading.Event(), threading.Event()
            calls = []
            def fetch():
                calls.append(len(calls))
                if len(calls) == 1:
                    entered.set()
                    release.wait(5)
                return {"prs": [], "viewer": "me"}
            real_flock, tried = cc.fcntl.flock, threading.Event()
            def flock(f, op):  # notes the second refresh reaching the lock, then really locks
                if threading.current_thread() is second:
                    tried.set()
                return real_flock(f, op)
            with mock.patch.object(cc, "DATA_DIR", data), \
                 mock.patch.object(cc, "PR_PLAN_FILE", data / "pr-plan.json"), \
                 mock.patch.object(cc, "fetch_pr_plan", side_effect=fetch), \
                 mock.patch.object(cc.fcntl, "flock", flock), \
                 mock.patch.dict(os.environ, {"CCDECK_PR_PLAN": "1"}):
                first = threading.Thread(target=cc.refresh_pr_plan)
                second = threading.Thread(target=cc.refresh_pr_plan)
                first.start()
                self.assertTrue(entered.wait(5))
                second.start()
                try:
                    self.assertTrue(tried.wait(5))  # it got to the lock while the first fetch is blocked...
                    time.sleep(0.2)  # ...and without the lock it would be fetching by now
                    self.assertEqual(len(calls), 1)
                finally:
                    release.set()
                    first.join(5)
                    second.join(5)
                self.assertEqual(len(calls), 2)
                self.assertFalse(first.is_alive() or second.is_alive())
                self.assertEqual(stat.S_IMODE(data.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE((data / "pr-plan.json").stat().st_mode), 0o600)


class ReportTest(unittest.TestCase):
    """`ccdeck report`: a session's own account of its work, and the goals built from it."""
    A, B, C = ("aaaaaaaa-0000-4000-8000-00000000000" + x for x in "abc")

    def setUp(self):
        shutil.rmtree(cc.REPORTS_DIR, ignore_errors=True)
        self.addCleanup(shutil.rmtree, cc.REPORTS_DIR, True)
        shutil.rmtree(cc.GOALS_DIR, ignore_errors=True)
        self.addCleanup(shutil.rmtree, cc.GOALS_DIR, True)
        marks = mock.patch.object(cc, "done_session_ids", lambda: set())
        marks.start()
        self.addCleanup(marks.stop)

    def run_report(self, *args, sid=None):
        env = dict(os.environ, CLAUDE_CODE_SESSION_ID=sid or self.A)
        return subprocess.run([sys.executable, str(ROOT / "ccdeck"), "report", *args], capture_output=True, text=True, env=env, timeout=30)

    def test_command_merges_and_validates(self):
        r = self.run_report("--next", "Waiting for review", "--waiting", "review", "--priority", "high", "--goal", "Ship 1.2")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.run_report("--todo", "Delete the old docs on prod", "--kind", "hands-on", "--size", "small")
        self.assertIn("to-do 1: Delete the old docs on prod  [hands-on]  [small]", r.stdout)
        self.assertIn("goal: Ship 1.2", r.stdout)  # merged, not replaced
        for bad, msg in ((["--waiting", "someone"], "--waiting is"), (["--after", "not-a-url"], "--after takes"),
                         (["--kind", "review"], "describe a --todo"), (["--done", "3"], "no to-do 3"),
                         (["--todo", "x", "--url", "javascript:alert(1)"], "--url must")):
            with self.subTest(bad=bad):
                r = self.run_report(*bad)
                self.assertNotEqual(r.returncode, 0)
                self.assertIn(msg, r.stderr)
        r = self.run_report("--next", "x", sid="../../etc/passwd")
        self.assertIn("not a session id", r.stderr)
        self.assertEqual([f.name for f in cc.REPORTS_DIR.glob("*.json")], [self.A + ".json"])
        self.assertIn("to-do 1:", self.run_report().stdout)  # no options: prints the report
        self.assertNotIn("to-do", self.run_report("--done", "1").stdout)
        self.assertIn("cleared", self.run_report("--clear").stdout)
        self.assertFalse((cc.REPORTS_DIR / (self.A + ".json")).exists())
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_SESSION_ID"}
        r = subprocess.run([sys.executable, str(ROOT / "ccdeck"), "report", "--next", "x"], capture_output=True, text=True, env=env, timeout=30)
        self.assertIn("runs inside a Claude Code session", r.stderr)  # the test runner has no Claude process above it

    def test_goals_are_reused(self):
        cc.update_report(self.A, {"goal": "Ship 1.2"})
        rep, notes = cc.update_report(self.B, {"goal": "ship   1.2"})
        self.assertEqual((rep["goal"], notes), ("Ship 1.2", []))  # the existing spelling
        rep, notes = cc.update_report(self.C, {"goal": "1.2 Ship"})
        self.assertEqual(rep["goal"], "1.2 Ship")  # saved as given, with a hint
        self.assertIn('Did you mean "Ship 1.2"?', notes[0])
        self.assertIsNone(cc.similar_goal("Billing export", cc.goals_in_use(cc.load_reports())))
        out = self.run_report("--goals").stdout
        self.assertEqual(out.splitlines()[0], "Ship 1.2  (2 sessions)")
        self.assertEqual(list(cc.goals_in_use(cc.load_reports(), done={self.A, self.B})), ["1.2 ship"])  # done sessions' goals are over

    def test_sanitize(self):
        self.assertIsNone(cc.sanitize_report([1]))
        r = cc.sanitize_report({"next": "a\n  b" + "x" * 500, "waiting": "<b>", "priority": "urgent", "goal": 5, "updated": True,
                                "after": ["https://github.com/a/b/pull/1", "javascript:x", 3] * 5,
                                "todos": [{"text": "t%d" % i, "kind": "nap", "url": "file:///etc"} for i in range(20)] + ["x", {"text": " "}]})
        self.assertEqual(len(r["next"]), cc.REPORT_TEXT)
        self.assertTrue(r["next"].startswith("a b"))
        self.assertEqual((r["waiting"], r["priority"], r["goal"], r["updated"]), ("", "normal", "", 0))
        self.assertEqual(r["after"], ["https://github.com/a/b/pull/1"] * 5)
        self.assertEqual(len(r["todos"]), cc.REPORT_TODOS)
        self.assertEqual(dict(r["todos"][0], id=None), {"id": None, "text": "t10", "kind": "", "size": "", "url": "", "done_at": 0})  # the newest
        self.assertRegex(r["todos"][0]["id"], "^[0-9a-f]{8}$")  # a file without ids gets stable ones
        self.assertEqual(r["todos"], cc.sanitize_report({"todos": r["todos"]})["todos"])
        self.assertEqual(cc.sanitize_report({"updated": float("nan")})["updated"], 0)  # NaN would break the API's JSON
        self.assertEqual(cc.sanitize_report({"updated": 10 ** 400})["updated"], 0)  # too large for a float
        odd = cc.sanitize_report({"todos": [{"text": "Check \ud800", "id": "0000aaaa"}, {"text": "Other", "id": "0000aaaa"}]})
        self.assertEqual(odd["todos"][0]["text"], "Check ?")  # an unpaired surrogate
        self.assertEqual(len({x["id"] for x in odd["todos"]}), 2)  # a repeated id is made unique
        ticked = cc.sanitize_report({"todos": [{"text": "d%d" % i, "done_at": 1000 + i} for i in range(30)]
                                     + [{"text": "o", "done_at": x} for x in (True, float("inf"), -5, "1")]})
        self.assertEqual([t["text"] for t in ticked["todos"] if t["done_at"]], ["d%d" % i for i in range(10, 30)])  # the latest ticks
        self.assertEqual(len(cc.open_todos(ticked)), 4)  # a done_at that isn't a time is open
        cc.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        (cc.REPORTS_DIR / "not.a-session.json").write_text('{"next": "x"}')
        (cc.REPORTS_DIR / (self.A + ".json")).write_text("not json")
        self.assertEqual(cc.load_reports(), {})

    def test_todo_done_by_id(self):
        for url in ("https://a.example/1", "https://a.example/2"):  # same text, different to-dos
            cc.update_report(self.A, {}, {"text": "Check it", "kind": "", "size": "", "url": url})
        first, second = cc.load_reports()[self.A]["todos"]
        self.assertNotEqual(first["id"], second["id"])
        cc.tick_todo(self.A, first["id"])
        cc.tick_todo(self.A, first["id"])  # a repeated click keeps it ticked, and the other one open
        ticked, still = cc.load_reports()[self.A]["todos"]
        self.assertEqual((ticked["id"], still), (first["id"], second))
        self.assertGreater(ticked["done_at"], 0)
        with self.assertRaises(ValueError):
            cc.tick_todo(self.A, "0000ffff")
        cc.tick_todo(self.A, first["id"], done=False)
        self.assertEqual(cc.load_reports()[self.A]["todos"], [first, second])

    def test_ticked_todos_dont_count(self):
        add = lambda text: cc.update_report(self.A, {}, {"text": text, "kind": "", "size": "", "url": ""})
        add("first")
        cc.tick_todo(self.A, cc.load_reports()[self.A]["todos"][0]["id"])
        for i in range(cc.REPORT_TODOS):
            rep, notes = add("t%d" % i)
        self.assertEqual((len(rep["todos"]), notes), (cc.REPORT_TODOS + 1, []))  # ten open and the ticked one
        with self.assertRaises(ValueError):  # unticking would make eleven open
            cc.tick_todo(self.A, rep["todos"][0]["id"], done=False)
        self.assertIn("1: t0", cc.format_report(rep))  # --done N counts open to-dos
        self.assertIn("done by the user: first", cc.format_report(rep))
        rep, _ = cc.update_report(self.A, {}, done_todo=1)
        self.assertEqual([t["text"] for t in rep["todos"]][:2], ["first", "t1"])  # the session's --done removes; your tick stays
        rep, notes = add("t10")
        rep, notes = add("t11")
        self.assertIn("oldest was dropped", notes[0])
        self.assertEqual([t["text"] for t in rep["todos"]][:2], ["first", "t2"])

    def test_goals_command(self):
        cc.update_report(self.A, {"goal": "Ship 1.2", "next": "Waiting for the review"},
                         {"text": "Check the preview", "kind": "validate", "size": "small", "url": ""})
        cc.update_report(self.A, {}, {"text": "Tag the release", "kind": "", "size": "", "url": ""})
        cc.tick_todo(self.A, cc.load_reports()[self.A]["todos"][1]["id"])
        sessions = [{"id": self.A, "alive": True, "title": "Release 1.2", "report": cc.load_reports()[self.A]}]
        out = io.StringIO()
        with mock.patch.object(cc, "collect", lambda **kw: sessions), mock.patch.object(cc, "pr_plan", lambda s: {"rows": []}), \
                contextlib.redirect_stdout(out):
            cc.cmd_goals(argparse.Namespace(all=False, done=False))
            cc.cmd_goals(argparse.Namespace(all=False, done=True))
        first, second = out.getvalue().split("Ship 1.2")[1:]
        self.assertIn("next: Waiting for the review", first)
        self.assertIn("[ ] Check the preview", first)
        self.assertNotIn("Tag the release", first)
        self.assertIn("1 done to-do hidden (--done)", first)
        self.assertIn("[x] Tag the release", second)

    def run_plan(self, *args, sid=None):
        env = dict(os.environ, CLAUDE_CODE_SESSION_ID=sid or self.A)
        return subprocess.run([sys.executable, str(ROOT / "ccdeck"), "plan", *args], capture_output=True, text=True, env=env, timeout=30)

    def test_plan_commands(self):
        G = ("--goal", "Fix DC bugs")
        r = self.run_plan(*G, "add", "Wrong vesting date", "--url", "https://linear.app/acme/issue/VOY-12/vesting", "--size", "small")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Added P1.", r.stdout)
        self.run_plan(*G, "add", "Rounding in exports", "--after", "P1")
        self.run_plan(*G, "add", "Copy for the banner", "--at", "1")
        plan = cc.load_plans()["fix dc bugs"]
        self.assertEqual([t["id"] for t in plan["items"]], ["P3", "P1", "P2"])
        self.assertEqual((plan["items"][2]["after"], plan["items"][1]["size"]), (["P1"], "small"))
        self.assertTrue(plan["items"][0]["cwd"].startswith("/"))  # where Start opens the session
        self.run_plan(*G, "move", "p2", "--to", "1")
        self.run_plan(*G, "set", "P3", "--blocked", "client copy", "--owner", "Dana", "--pr", PR.format(5))
        self.run_plan(*G, "drop", "P1")
        plan = cc.load_plans()["fix dc bugs"]
        self.assertEqual([t["id"] for t in plan["items"]], ["P2", "P3", "P1"])
        p3 = plan["items"][1]
        self.assertEqual((p3["status"], p3["blocked_on"], p3["owner"], p3["prs"]), ("blocked", "client copy", "Dana", [PR.format(5)]))
        self.assertEqual(plan["items"][2]["status"], "dropped")
        for bad, msg in ((["move", "P9", "--to", "1"], "no item P9"), (["set", "P2", "--after", "P2"], "after itself"),
                         (["add", "x", "--after", "P7"], "no item P7"), (["add", "x", "--url", "ftp://x"], "--url must"),
                         (["set", "P2", "--pr", "nope"], "--pr takes"), (["set", "P2", "--title", " "], "needs a title")):
            with self.subTest(bad=bad):
                r = self.run_plan(*G, *bad)
                self.assertNotEqual(r.returncode, 0)
                self.assertIn(msg, r.stderr)
        self.assertIn("has no plan", self.run_plan("--goal", "Other goal", "done", "P1").stderr)
        self.assertIn("Which goal?", self.run_plan("done", "P1").stderr)  # this session reported no goal
        cc.update_report(self.A, {"goal": "fix dc bugs"})
        self.assertEqual(cc.load_reports()[self.A]["goal"], "Fix DC bugs")  # the plan's spelling
        self.assertEqual(self.run_plan("done", "P2").returncode, 0)  # the session's goal
        self.assertEqual(cc.load_plans()["fix dc bugs"]["items"][0]["status"], "done")
        self.assertIn("Fix DC bugs", self.run_report("--goals").stdout)  # a goal with a plan is in use
        self.assertIn("Closed", self.run_plan(*G, "close").stdout)
        self.assertNotIn("Fix DC bugs", cc.goals_in_use({}))

    def test_report_item(self):
        cc.change_plan("Ship 2.0", lambda p: cc.plan_add(p, "Migrate the queue"), create=True)
        with self.assertRaises(ValueError):
            cc.update_report(self.A, {"item": "P1"})  # no goal yet
        with self.assertRaises(ValueError):
            cc.update_report(self.A, {"goal": "Ship 2.0", "item": "P4"})
        rep, _ = cc.update_report(self.A, {"goal": "Ship 2.0", "item": "p1"})
        self.assertEqual(rep["item"], "P1")
        cc.update_report(self.A, {"item": "P1"})
        self.assertEqual(cc.load_plans()["ship 2.0"]["items"][0]["sessions"], [self.A])  # once
        self.assertIn("item: P1", cc.format_report(rep))
        self.assertEqual(cc.update_report(self.A, {"item": ""})[0]["item"], "")
        cc.update_report(self.A, {"item": "P1"})
        self.assertEqual(cc.update_report(self.A, {"goal": "ship  2.0"})[0]["item"], "P1")  # the same goal, spelled differently
        self.assertEqual(cc.update_report(self.A, {"goal": "Billing"})[0]["item"], "")  # P1 was the old goal's
        self.assertEqual(cc.sanitize_report({"item": "../x"})["item"], "")

    def test_item_state(self):
        item = lambda **kw: dict({"status": "", "owner": "", "blocked_on": ""}, **kw)
        pr = lambda state, step=None, *texts: {"state": state, "step": step, "texts": list(texts)}
        live = lambda g="waiting": {"alive": True, "group": g}
        closed = {"alive": False, "group": "later"}
        cases = [
            (item(status="dropped"), [pr("merged")], [live()], [], ("dropped", None)),
            (item(status="done"), [pr("open", 2)], [], [], ("done", None)),  # marked by hand
            (item(), [pr("merged"), pr("merged")], [live()], [], ("done", None)),  # every PR merged
            (item(), [pr("merged"), pr("closed")], [], [], ("done", None)),  # a closed PR was given up on
            (item(), [pr("merged"), pr("open", 4)], [], [], ("review", None)),  # a follow-up is still open
            (item(status="blocked", blocked_on="copy"), [], [live()], [], ("working", "session")),  # live wins over a block
            (item(), [], [live("needs")], [], ("you", "you")),
            (item(), [pr("open", 2, "Changes requested")], [live()], [], ("fix", "session")),
            (item(), [pr("open", 4), pr("open", 1)], [live()], [], ("merge", "you")),  # the most urgent PR
            (item(owner="Sam"), [pr("open", 2)], [], [], ("theirs", "Sam")),  # their PR isn't yours to fix
            (item(status="blocked", blocked_on="copy"), [], [closed], [], ("blocked", None)),
            (item(), [], [], ["P1"], ("blocked", None)),
            (item(), [pr("open", 2)], [], [], ("fix", "you")),
            (item(), [], [closed], [], ("paused", "you")),
            (item(), [], [], [], ("queued", None)),
        ]
        for it, prs, sessions, after, (state, who) in cases:
            with self.subTest(item=it, prs=prs, sessions=sessions):
                got = cc.item_state(it, prs, sessions, after)
                self.assertEqual((got[0], got[2]), (state, who))
                self.assertIn(got[0], cc.ITEM_STATES)
        self.assertEqual(cc.item_state(item(status="blocked", blocked_on="client copy"), [], [], [])[1], "client copy")
        self.assertEqual(cc.item_state(item(), [], [], ["P1", "P2"])[1], "After P1, P2")
        self.assertEqual(cc.item_state(item(owner="Sam"), [], [], [])[1:], ("", "Sam"))
        self.assertEqual(cc.item_state(item(), [pr("open", 2, "CI failing", "Conflicts")], [], [])[1], "CI failing; Conflicts")

    def test_plan_place_and_dependencies(self):
        plan = cc.sanitize_plan({"goal": "G", "items": [{"id": "P1", "title": "a"}, {"id": "P2", "title": "b", "status": "done"},
                                                        {"id": "P3", "title": "c", "after": ["P1"]}]})
        cc.plan_place(plan, "P1", "P3", after=True)  # the page shows P1, P3: down puts P1 after P3, past the hidden P2
        self.assertEqual([t["id"] for t in plan["items"]], ["P2", "P3", "P1"])
        cc.plan_place(plan, "P1", "P3", after=False)
        self.assertEqual([t["id"] for t in plan["items"]], ["P2", "P1", "P3"])
        plan["items"][1]["prs"] = [PR.format(1)]
        state = lambda cache: {t["id"]: t for t in cc.plan_view(plan, [], [], cache)}["P3"]
        self.assertEqual(state({PR.format(1): {"data": {"state": "open"}}})["waiting_on"], ["P1"])
        p3 = state({PR.format(1): {"data": {"state": "merged"}}})
        self.assertEqual((p3["waiting_on"], p3["state"]), ([], "queued"))  # P1's PR merged: P1 is done, P3 can start

    def test_pr_given_by_hand_wins(self):
        plan = cc.sanitize_plan({"goal": "G", "items": [{"id": "P2", "title": "a", "url": "https://linear.app/acme/issue/ENG-2/a"},
                                                        {"id": "P3", "title": "b", "prs": [PR.format(4)]}]})
        sessions = [{"id": self.A, "alive": False, "group": "later", "report": None,
                     "pr_details": [{"url": PR.format(4), "own": True, "state": "open", "title": "ENG-2 and more", "head": "x"}]}]
        by = {t["id"]: t for t in cc.plan_view(plan, sessions, [], {})}
        self.assertEqual(([p["url"] for p in by["P2"]["prs"]], [p["url"] for p in by["P3"]["prs"]]), ([], [PR.format(4)]))

    def test_sanitize_plan(self):
        self.assertIsNone(cc.sanitize_plan({"items": []}))  # no goal
        p = cc.sanitize_plan({"goal": "G", "next_id": 2, "closed": float("nan"), "items": [
            {"id": "P1", "title": "a", "url": "javascript:x", "status": "weird", "sessions": ["../x", "s1"], "cwd": "rel/dir"},
            {"id": "P1", "title": "dup"}, {"id": "Q2", "title": "bad id"}, {"id": "P3", "title": " "}, {"id": "P5", "title": "b", "after": ["P1", "nope"]}]})
        self.assertEqual([t["id"] for t in p["items"]], ["P1", "P5"])
        self.assertEqual((p["items"][0]["url"], p["items"][0]["status"], p["items"][0]["sessions"], p["items"][0]["cwd"]), ("", "", ["s1"], ""))
        self.assertEqual((p["items"][1]["after"], p["next_id"], p["closed"]), (["P1"], 6, 0))  # ids are never reused
        cc.GOALS_DIR.mkdir(parents=True, exist_ok=True)
        (cc.GOALS_DIR / "wrongname.json").write_text(json.dumps({"goal": "G", "items": []}))  # not where G's plan lives
        (cc.GOALS_DIR / "x.json").write_text("not json")
        self.assertEqual(cc.load_plans(), {})

    def test_plan_links_prs_and_sessions(self):
        plan = cc.sanitize_plan({"goal": "Ship 2.0", "created": 100, "items": [
            {"id": "P1", "title": "Queue", "url": "https://linear.app/acme/issue/ENG-7/queue", "sessions": [self.A]},
            {"id": "P2", "title": "Retry", "url": "https://linear.app/acme/issue/ENG-8/retry", "sessions": [self.A], "after": ["P1"]},
            {"id": "P3", "title": "Docs", "prs": [PR.format(9)]}]})
        rep = cc.sanitize_report({"goal": "ship 2.0", "item": "P2", "next": "Fix the review"})
        sessions = [{"id": self.A, "alive": True, "group": "waiting", "report": rep, "pr_details": [
            {"url": PR.format(1), "own": True, "state": "merged", "title": "ENG-7: queue", "head": "x"},  # ENG-7 in the title: P1's
            {"url": PR.format(2), "own": True, "state": "open", "title": "Retry", "head": "me/eng-8-retry"},
            {"url": PR.format(3), "own": True, "state": "open", "title": "No key", "head": "misc"},  # the session's item: P2
            {"url": PR.format(4), "own": False, "state": "open", "title": "ENG-8 mentioned", "head": "y"}]}]
        rows = [{"url": PR.format(2), "number": 2, "title": "Retry", "head": "me/eng-8-retry", "step": 2, "texts": ["Changes requested"], "session": {"id": self.A}, "updated": 150}]
        cache = {PR.format(9): {"data": {"state": "closed", "title": "Docs"}}}
        seen = []
        with mock.patch.object(cc, "item_state", lambda t, prs, ss, w: seen.append((t["id"], prs, ss, w)) or ("working", "", None)):
            view = cc.plan_view(plan, sessions, rows, cache)
        by = {t["id"]: t for t in view}
        self.assertEqual([p["url"] for p in by["P1"]["prs"]], [PR.format(1)])
        self.assertEqual([p["url"] for p in by["P2"]["prs"]], [PR.format(2), PR.format(3)])
        self.assertEqual([(p["url"], p["state"]) for p in by["P3"]["prs"]], [(PR.format(9), "closed")])  # given by hand: from the cache
        self.assertEqual((by["P1"]["sessions"], by["P2"]["sessions"]), ([], [self.A]))  # the session works P2 now
        self.assertEqual((by["P2"]["next"], by["P2"]["waiting_on"], by["P2"]["rank"]), ("Fix the review", [], 2))  # P1: its only PR merged
        p2 = next(x for x in seen if x[0] == "P2")
        self.assertEqual(p2[1][0]["step"], 2)  # an open PR in the PR plan has its step
        self.assertEqual(p2[2], [{"alive": True, "group": "waiting"}])
        self.assertIn("--item P2", by["P2"]["start"])
        board = cc.goal_board(sessions, rows, now=200, plans={"ship 2.0": plan}, cache=cache)
        self.assertEqual((board[0]["goal"], board[0]["summary"].split(",")[0]), ("Ship 2.0", "1/3 done"))  # P1: its only PR merged
        plan["closed"] = 1
        self.assertFalse(cc.goal_board([], [], now=200, plans={"ship 2.0": plan}))  # a closed plan with no sessions: gone

    def test_goal_board(self):
        now = time.time()
        rep = lambda **kw: cc.sanitize_report(dict({"updated": now - 100}, **kw))
        sessions = [
            {"id": "s1", "alive": True, "report": rep(goal="Ship 1.2", priority="low", todos=[
                {"text": "Submit to the stores"}, {"text": "Tag it", "done_at": now - 9}, {"text": "Bump it", "done_at": now - 5}])},
            {"id": "s2", "alive": False, "report": rep(goal="ship 1.2", priority="high")},
            {"id": "s3", "alive": False, "report": rep(goal="Billing", todos=[{"text": "old"}]), "mark": {"done": True}},
            {"id": "s4", "alive": True, "report": rep()},
            {"id": "s5", "alive": True, "report": None},
            {"id": "s6", "alive": True, "report": rep(goal="Docs", priority="high")},
        ]
        rows = [{"url": PR.format(1), "step": 1, "updated": now - 900, "session": {"id": "s2"}},
                {"url": PR.format(2), "step": 4, "updated": now - 50, "session": {"id": "s2"}},
                {"url": PR.format(3), "step": 3, "updated": now, "session": None}]
        board = cc.goal_board(sessions, rows, now=now)
        self.assertEqual([g["goal"] for g in board], ["Ship 1.2", "Docs", "Other", "Billing"])  # same priority: oldest first
        ship = board[0]
        self.assertEqual((ship["priority"], [s["id"] for s in ship["sessions"]], [p["url"] for p in ship["prs"]]),
                         ("high", ["s1", "s2"], [PR.format(1), PR.format(2)]))
        self.assertEqual(ship["summary"], "2 PRs: 1 to merge, 1 waiting, 1 to-do for you")
        self.assertEqual([(x["text"], x["sid"]) for x in ship["todos"]], [("Submit to the stores", "s1")])
        self.assertEqual([x["text"] for x in ship["todos_done"]], ["Bump it", "Tag it"])  # the latest tick first
        self.assertEqual((board[3]["open"], board[3]["todos"], board[3]["summary"]), (False, [], "Done"))  # a done session's to-dos are over
        self.assertTrue(board[2]["other"])
        self.assertEqual(board[3]["since"], now - 100)  # a done goal: its last report, so snapshots don't change by themselves
        self.assertEqual(cc.goal_board(sessions, rows, now=now + 60)[3]["since"], now - 100)

    def test_dashboard_and_server(self):
        write_session(self.A, [user("ship it", 20), asst([tool("r1", "Bash", {"command": "gh pr create"})], 19),
                               result("r1", 19, PR.format(70)), asst([text("Opened it.")], 18)])
        self.addCleanup(lambda: (PROJ / (self.A + ".jsonl")).unlink())
        cc.update_report(self.A, {"goal": "Ship 1.2", "next": "Waiting for the review", "after": [PR.format(8)]},
                         {"text": "Check the preview", "kind": "validate", "size": "small", "url": ""})
        plan = {"fetched": time.time(), "viewer": "me", "prs": [dict(url=PR.format(70), repo="me/app", head="report-b", base="main", default_base="main",
                number=70, title="PR 70", draft=False, checks="passing", merge="clean", review="", threads=0, threads_yours=0, updated=time.time())]}
        with mock.patch.object(cc, "load_json", lambda path, d, orig=cc.load_json: plan if path == cc.PR_PLAN_FILE else orig(path, d)):
            dash = cc.Dashboard(0)
            dash.refresh()
        snap = dash.snapshot
        s = next(x for x in snap["sessions"] if x["id"] == self.A)
        self.assertEqual((s["report"]["goal"], s["report_stale"]), ("Ship 1.2", False))
        row = next(r for r in snap["plan"]["rows"] if r["number"] == 70)
        self.assertEqual((row["goal"], row["session_next"], row["after"]), ("Ship 1.2", "Waiting for the review", [PR.format(8)]))
        goal = snap["goals"][0]
        self.assertEqual((goal["goal"], goal["summary"]), ("Ship 1.2", "1 PR: 1 to merge, 1 to-do for you"))
        tid = goal["todos"][0]["id"]
        self.assertEqual(dash.handle_action({"id": self.A, "action": "todo_done", "todo": "0000ffff"})[0], 409)
        self.assertEqual(dash.handle_action({"id": self.A, "action": "todo_done", "todo": 0})[0], 409)
        self.assertEqual(dash.handle_action({"id": self.A, "action": "todo_done", "todo": tid})[0], 200)
        self.assertEqual(cc.open_todos(cc.load_reports()[self.A]), [])
        dash.refresh()
        self.assertEqual([t["id"] for t in dash.snapshot["goals"][0]["todos_done"]], [tid])
        self.assertEqual(dash.handle_action({"id": self.A, "action": "todo_undone", "todo": tid})[0], 200)
        self.assertEqual(cc.open_todos(cc.load_reports()[self.A])[0]["id"], tid)
        cc.change_plan("Ship 1.2", lambda p: [cc.plan_add(p, "One"), cc.plan_add(p, "Two")], create=True)
        for op, target, order in (("down", "P2", ["P2", "P1"]), ("up", "P2", ["P1", "P2"]), ("done", None, ["P1", "P2"])):
            self.assertEqual(dash.handle_action({"action": "plan_item", "goal": "ship 1.2", "item": "P1", "op": op, "target": target})[0], 200)
            self.assertEqual([t["id"] for t in cc.load_plans()["ship 1.2"]["items"]], order)
        self.assertEqual(cc.load_plans()["ship 1.2"]["items"][0]["status"], "done")
        self.assertEqual(dash.handle_action({"action": "plan_item", "goal": "ship 1.2", "item": "P9", "op": "done"})[0], 409)
        self.assertEqual(dash.handle_action({"action": "plan_item", "goal": "ship 1.2", "item": "P1", "op": "rm"})[0], 400)
        self.assertEqual(dash.handle_action({"action": "plan_item", "goal": "ship 1.2", "item": "P1", "op": "up", "target": "P1"})[0], 409)
        self.assertEqual([t["id"] for t in dash.snapshot["goals"][0]["plan"]], ["P1", "P2"])
        self.assertIn("queued", dash.snapshot["item_states"])
        cc.REPORTS_DIR.joinpath(self.A + ".json").write_text(json.dumps(dict(cc.load_reports()[self.A], updated=time.time() - 30 * 3600)))
        dash.refresh()
        self.assertTrue(next(x for x in dash.snapshot["sessions"] if x["id"] == self.A)["report_stale"])


class UsageTest(unittest.TestCase):
    def test_tokens_counted_once_and_context(self):
        build_usage_fixture()
        rows = by_id(cc.collect(True, 10 ** 6))
        s = cc.session_json(rows["s-usage"])
        self.assertEqual(s["usage"], {"input": 15, "cache_write": 1500, "cache_read": 11000, "output": 500})
        self.assertEqual(s["sub_usage"]["output"], 50)
        self.assertEqual(s["context"], 5 + 500 + 6000)
        self.assertEqual(s["context_limit"], 1_000_000)
        self.assertEqual(s["model"], "claude-opus-5-5")
        old = cc.session_json(rows["s-old-model"])
        self.assertEqual(old["context_limit"], 200_000)

    def test_fmt_tokens(self):
        self.assertEqual([cc.fmt_tokens(n) for n in (0, 999, 92_400, 1_000_000, 15_500_000)],
                         ["0", "999", "92k", "1M", "15.5M"])


class LinksTest(unittest.TestCase):
    """Linear issues, Notion pages and artifacts: shared by you, created in the session, or mentioned."""

    @classmethod
    def setUpClass(cls):
        notion_url = "https://www.notion.so/acme/Webhook-spec-" + "a" * 32
        created_notion = "https://www.notion.so/" + "b" * 32
        entries = [
            user("Implement the webhook from " + notion_url + " and keep UTF-8 output", 30),
            asst([tool("l1", "mcp__Linear__get_issue", {"id": "ENG-42"})], 29),
            result("l1", 29, json.dumps({"identifier": "ENG-42", "title": "Stripe webhooks",
                                         "url": "https://linear.app/acme/issue/ENG-42/stripe-webhooks"})),
            asst([tool("n1", "mcp__Notion__notion-create-pages", {"pages": [{}]})], 28),
            result("n1", 28, json.dumps({"pages": [{"id": "b" * 32, "url": created_notion, "title": "Rollout plan"}]})),
            asst([tool("a1", "Artifact", {"file_path": "/tmp/x.html", "title": "Queue comparison"})], 27),
            result("a1", 27, "Published https://claude.ai/artifact/abc123XYZ (private)"),
            asst([tool("q1", "Artifact", {"action": "quickstart", "intent": "other"})], 27),
            result("q1", 27, "Types: Data https://claude.ai/artifact/TypeData1 and Docs https://claude.ai/artifact/TypeDocs1"),
            asst([tool("t1", "Artifact", {"type_url": "https://claude.ai/artifact/TypeData1", "title": "Burn-down"})], 27),
            result("t1", 27, "Created https://claude.ai/artifact/ourDash9 from https://claude.ai/artifact/TypeData1"),
            asst([text("Done. See ENG-42 and the plan. Also OPS-9 is unrelated.")], 26),
        ]
        for e in entries:
            e["gitBranch"] = "juraj/eng-57-email-queue"
        write_session("s-refs", entries)
        cls.s = by_id(cc.collect(True, 10 ** 6))["s-refs"]
        cls.refs = {r["url"]: r for r in cls.s["refs"]}
        cls.linear = {r["key"]: r for r in cls.s["linear_list"]}
        cls.notion_url, cls.created_notion = notion_url, created_notion

    def test_link_you_shared_gets_title_from_slug(self):
        r = self.refs[self.notion_url]
        self.assertEqual((r["kind"], r["source"], r["title"]), ("notion", "you", "Webhook spec"))

    def test_created_links_get_titles_from_tool_results(self):
        self.assertEqual((self.refs[self.created_notion]["source"], self.refs[self.created_notion]["title"]), ("created", "Rollout plan"))
        art = self.refs["https://claude.ai/artifact/abc123XYZ"]
        self.assertEqual((art["kind"], art["source"], art["title"]), ("artifact", "created", "Queue comparison"))

    def test_artifact_types_and_lookups_are_not_ours(self):
        """Quickstart lists other accounts' Artifact types; a type_url publish names the type it used."""
        self.assertNotIn("https://claude.ai/artifact/TypeData1", self.refs)
        self.assertNotIn("https://claude.ai/artifact/TypeDocs1", self.refs)
        dash = self.refs["https://claude.ai/artifact/ourDash9"]
        self.assertEqual((dash["source"], dash["title"]), ("created", "Burn-down"))

    def test_linear_from_tool_branch_and_mentions(self):
        self.assertEqual(self.linear["ENG-42"]["title"], "Stripe webhooks")
        self.assertEqual(self.linear["ENG-42"]["url"], "https://linear.app/acme/issue/ENG-42")
        b = self.linear["ENG-57"]  # from the branch; team ENG is known, workspace learned from ENG-42
        self.assertTrue(b["primary"])
        self.assertEqual((b["source"], b["url"]), ("branch", "https://linear.app/acme/issue/ENG-57"))
        self.assertEqual(self.s["linear_list"][0]["key"], "ENG-57")

    def test_unknown_teams_are_not_issues(self):
        self.assertNotIn("UTF-8", self.linear)
        self.assertNotIn("OPS-9", self.linear)

    def test_env_teams(self):
        os.environ["CCDECK_LINEAR_TEAMS"] = "OPS"
        try:
            s = by_id(cc.collect(True, 10 ** 6))["s-refs"]
            self.assertIn("OPS-9", {r["key"] for r in s["linear_list"]})
        finally:
            del os.environ["CCDECK_LINEAR_TEAMS"]


class HookTest(unittest.TestCase):
    def run_hook(self, payload):
        import io
        old = sys.stdin
        sys.stdin = io.StringIO(json.dumps(payload))
        try:
            cc.cmd_hook(None)
        finally:
            sys.stdin = old

    def test_hook_clears_the_notification_you_acted_on(self):
        """Approving prompt A and getting prompt B between two dashboard scans reads as "permission" both
        times; the hook's PostToolUse is what says A was answered."""
        build_fixtures()
        shutil.rmtree(cc.INBOX_DIR, ignore_errors=True)
        cc.inbox_add("s-closed", "permission", "Needs approval", "Claude wants to use Bash")
        keep = cc.inbox_add("s-midtask", "permission", "Needs approval", "Claude wants to use Edit")
        self.run_hook({"session_id": "s-closed", "hook_event_name": "PermissionRequest", "tool_name": "Bash"})
        self.assertEqual(len(cc.inbox_items()), 2)  # asking again isn't acting on it
        with mock.patch.object(cc, "wait_for_approval", lambda *a: None):
            self.run_hook({"session_id": "s-closed", "hook_event_name": "PostToolUse", "tool_name": "Bash"})
        self.assertEqual([d["id"] for d in cc.inbox_items()], [keep])

    def test_session_end_preserves_pr_notifications_and_clears_prompts(self):
        build_fixtures()
        shutil.rmtree(cc.INBOX_DIR, ignore_errors=True)
        states = cc.PR_STATES | {"question", "permission", "plan", "decision"}
        kept = set()
        for state in states:
            sid = "s-end-" + state
            nid = cc.inbox_add(sid, state, "Needs you", state)
            if state in cc.PR_STATES:
                kept.add(nid)
            self.run_hook({"session_id": sid, "hook_event_name": "SessionEnd", "reason": "exit"})
        self.assertEqual({d["id"] for d in cc.inbox_items()}, kept)

    def test_question_notification_uses_pending_question_text(self):
        build_fixtures()
        long_q = "Which database should we use? " + "Context. " * 120  # past the 600-character summary
        question = long_q + "\n\nAnd which region?"
        write_session("s-question", [user("plan it", 200),
                      asst([text("Let me ask you."), tool("t1", "AskUserQuestion",
                            {"questions": [{"question": long_q}, {"question": "And which region?"}]})], 199)])
        row = by_id(cc.collect(True, 10 ** 6))["s-question"]
        row["alive"] = True
        row["hook_message"] = "Generic hook message"
        dash = cc.Dashboard(0)
        dash.prev_states = {}
        with mock.patch.object(cc, "notify") as notify:
            dash._notify_changes([row])
        self.assertEqual(notify.call_args.kwargs["text"], question)  # the bell: every question, whole
        self.assertEqual(notify.call_args.args[1], cc.one_line(question, 160))  # the popup: one short line
        self.assertLessEqual(len(row["pending_input"]), 600)  # the waiting-state summary is unchanged

    def test_new_permission_between_scans_gets_a_fresh_notification(self):
        build_fixtures()
        sid = "s-closed"
        (cc.STATE_DIR / f"{sid}.json").unlink(missing_ok=True)
        # pid 123 is made up, but it can be a real, live process: don't leave it for later tests, pass or fail
        self.addCleanup((cc.STATE_DIR / f"{sid}.json").unlink, missing_ok=True)
        dash = cc.Dashboard(0)
        def scan():
            rows = cc.collect(True, 10 ** 6)
            dash._notify_changes(rows)
        with mock.patch.object(cc, "pid_alive", return_value=True), \
                mock.patch.object(cc, "wait_for_approval", return_value=None), \
                mock.patch.object(cc, "daemon_running", return_value=True):
            self.run_hook({"session_id": sid, "hook_event_name": "UserPromptSubmit"})
            cc.save_json(cc.STATE_DIR / f"{sid}.json", dict(cc.load_json(cc.STATE_DIR / f"{sid}.json", {}), pid=123))
            scan()  # first scan never replays notifications
            self.run_hook({"session_id": sid, "hook_event_name": "PermissionRequest", "tool_name": "Bash"})
            scan()
            first = next(d for d in cc.inbox_items() if d["sid"] == sid)
            self.run_hook({"session_id": sid, "hook_event_name": "PostToolUse", "tool_name": "Bash"})
            self.run_hook({"session_id": sid, "hook_event_name": "PermissionRequest", "tool_name": "Edit"})
            scan()  # both dashboard scans see permission
            second = next(d for d in cc.inbox_items() if d["sid"] == sid)
            self.assertNotEqual(first["id"], second["id"])
            self.assertIn("Edit", second["body"])
            scan()
            self.assertEqual(next(d for d in cc.inbox_items() if d["sid"] == sid)["id"], second["id"])
            cc.inbox_clear(second["id"])  # dismissal must survive another scan of the same prompt
            scan()
            self.assertFalse(any(d["sid"] == sid for d in cc.inbox_items()))

    def test_closing_doesnt_bring_back_a_dismissed_pr_notification(self):
        dash = cc.Dashboard(0)
        dash.prev_states = {"s-pr": ("ci_failing", None)}  # notified, then dismissed
        row = {"id": "s-pr", "state": "ci_failing", "group": "fix", "alive": False, "notification_epoch": 123.0}
        with mock.patch.object(cc, "notify") as notify:
            dash._notify_changes([row])  # SessionEnd moved the epoch; the PR's state didn't change
        notify.assert_not_called()

    def test_odd_question_input_doesnt_break_the_scan(self):
        write_session("s-oddq", [user("plan it", 200), asst([tool("t1", "AskUserQuestion", {"questions": 1})], 199)])
        self.assertIsNone(cc.parse_transcript(PROJ / "s-oddq.jsonl")["pending_question"])  # no TypeError

    def test_each_question_is_its_own_notification(self):
        """A question is its tool call: an answer's epoch alone doesn't repeat it, and the next question with
        the same epoch (both read between two hook events) still gets its own."""
        dash = cc.Dashboard(0)
        row = lambda tid, epoch: {"id": "s-q", "state": "question", "group": "needs", "alive": True,
                                  "pending_id": tid, "notification_epoch": epoch, "pending_question": tid}
        sent = []
        with mock.patch.object(cc, "notify", lambda *a, **k: sent.append(k["text"])):
            dash.prev_states = {"s-q": ("working", None)}
            for r in (row("A", 1), row("A", 2), row("B", 2)):
                dash._notify_changes([r])
        self.assertEqual(sent, ["A", "B"])

    def test_a_held_inbox_lock_doesnt_hold_up_the_hook(self):
        import fcntl
        cc.INBOX_DIR.mkdir(parents=True, exist_ok=True)
        with open(cc.INBOX_DIR / ".lock", "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)  # another hook, stuck while holding it
            t = time.monotonic()
            with self.assertRaises(OSError):  # notify() catches it: the popup goes out without a bell entry
                cc.inbox_add("s-lock", "idle", "Your turn", "")
            self.assertLess(time.monotonic() - t, 3)

    def test_refreshes_take_turns(self):
        """The scan loop and an action refreshing at once: the older one mustn't finish last."""
        import threading
        dash, running, overlap = cc.Dashboard(0), [], []
        def collect(**kw):
            running.append(1)
            if len(running) > 1:
                overlap.append(1)
            time.sleep(0.05)
            running.pop()
            return []
        with mock.patch.object(cc, "collect", collect), mock.patch.object(cc, "inbox_items", lambda: []), \
                mock.patch.object(cc, "review_requests", lambda s: []), mock.patch.object(cc, "hooks_active", lambda: True):
            threads = [threading.Thread(target=dash.refresh) for _ in range(4)]
            for th in threads:
                th.start()
            for th in threads:
                th.join()
        self.assertEqual(overlap, [])

    def test_closed_pending_question_withdraws_the_desktop_notification(self):
        build_fixtures()
        sid = "s-question"
        nid = cc.inbox_add(sid, "question", "Question", "Which one?")
        def click(title, body, urgent, wanted):
            self.assertTrue(wanted())
            rows = cc.collect(True, 10 ** 6)
            row = by_id(rows)[sid]
            self.assertEqual((row["pending_tool"], row["state"], row["alive"]),
                             ("AskUserQuestion", "question", False))
            dash = cc.Dashboard(0)
            dash.prev_states = {sid: ("question", 123)}  # last scan saw the open question
            dash._notify_changes(rows)
            self.assertFalse(wanted())
            return False, None
        with mock.patch.object(cc, "notification_click", side_effect=click):
            cc.cmd_notify_click(type("Args", (), dict(sid=sid, id=nid, title="Question", body="Which one?", urgent=True)))

    def test_unwritable_inbox_keeps_the_notification_and_the_hook(self):
        build_fixtures()
        (cc.STATE_DIR / "s-noinbox.json").unlink(missing_ok=True)
        sent = []
        def fail(*a):
            raise PermissionError("inbox/.lock")
        with mock.patch.object(cc, "inbox_add", fail), mock.patch.object(cc, "IS_MAC", False), \
                mock.patch.object(cc, "notify_enabled", lambda c: True), mock.patch.object(cc, "daemon_running", lambda: False), \
                mock.patch.object(cc.importlib.util, "find_spec", lambda n: object()), \
                mock.patch.object(cc, "desktop_env", lambda: {}), \
                mock.patch.object(cc.subprocess, "Popen", lambda cmd, **kw: sent.append(cmd) or mock.MagicMock()):
            self.run_hook({"session_id": "s-noinbox", "hook_event_name": "Notification",
                           "notification_type": "permission_prompt", "message": "Claude needs your permission to use Bash"})
        (click,) = [c for c in sent if "notify-click" in c]  # the desktop notification still went out
        self.assertNotIn("--id", click)  # nothing in the inbox for the click helper to watch
        self.assertEqual(cc.load_json(cc.STATE_DIR / "s-noinbox.json", {}).get("status"), "permission")  # state saved

    def test_live_states_from_hooks(self):
        build_fixtures()
        sid = "s-closed"
        # Pretend this test process is the Claude process for the session.
        state_file = cc.STATE_DIR / f"{sid}.json"
        self.run_hook({"session_id": sid, "hook_event_name": "UserPromptSubmit", "cwd": str(WORK)})
        st = json.loads(state_file.read_text())
        st["pid"], st["pid_start"] = os.getpid(), cc.proc_start_time(os.getpid())
        state_file.write_text(json.dumps(st))
        self.assertEqual(by_id(cc.collect(True, 10 ** 6))[sid]["state"], "working")

        self.run_hook({"session_id": sid, "hook_event_name": "Notification",
                       "notification_type": "permission_prompt", "message": "Claude needs your permission to use Bash"})
        row = by_id(cc.collect(True, 10 ** 6))[sid]
        self.assertEqual((row["state"], row["alive"]), ("permission", True))
        self.assertIn("permission", row["hook_message"])

        self.run_hook({"session_id": sid, "hook_event_name": "Stop"})
        self.assertEqual(by_id(cc.collect(True, 10 ** 6))[sid]["state"], "idle")

        self.run_hook({"session_id": sid, "hook_event_name": "SessionEnd", "reason": "exit"})
        self.assertEqual(by_id(cc.collect(True, 10 ** 6))[sid]["state"], "closed")

        # a second process resumed the same session and is still open
        saved = cc.running_claude_processes
        cc.running_claude_processes = lambda: [(1, str(WORK), ["claude", "--resume", sid])]
        try:
            self.assertTrue(by_id(cc.collect(True, 10 ** 6))[sid]["alive"])
        finally:
            cc.running_claude_processes = saved

    def test_print_resume_does_not_mark_session_alive(self):
        build_fixtures()
        (cc.STATE_DIR / "s-closed.json").unlink(missing_ok=True)  # only the -p process may speak for it
        for flag in ("-p", "--print"):
            with mock.patch.object(cc, "peer_sessions", return_value={}), \
                    mock.patch.object(cc, "running_claude_processes", return_value=[
                        (1, str(WORK), ["claude", flag, "--resume", "s-closed"]) ]):
                self.assertFalse(by_id(cc.collect(True, 10 ** 6))["s-closed"]["alive"])

    def test_a_headless_run_doesnt_speak_for_the_session(self):
        """claude -p --resume <id> fires the same hooks: they mustn't open the session, mark it working, end it,
        or take over the record of the interactive process that has it open."""
        build_fixtures()
        sid, me, headless_pid = "s-closed", os.getpid(), 999_999
        path = cc.STATE_DIR / f"{sid}.json"
        path.unlink(missing_ok=True)
        self.addCleanup(path.unlink, missing_ok=True)
        def hook(event, sender, **kw):
            with mock.patch.object(cc, "find_claude_ancestor", return_value=sender), \
                    mock.patch.object(cc, "pid_is_headless", lambda p: p == headless_pid):
                self.run_hook(dict({"session_id": sid, "hook_event_name": event, "cwd": str(WORK)}, **kw))
        def row():
            with mock.patch.object(cc, "peer_sessions", return_value={}), \
                    mock.patch.object(cc, "running_claude_processes", return_value=[]):
                return by_id(cc.collect(True, 10 ** 6))[sid]
        hook("UserPromptSubmit", headless_pid)
        self.assertFalse(path.exists())                       # headless first: nothing recorded
        self.assertFalse(row()["alive"])
        hook("UserPromptSubmit", me)
        hook("Stop", me)
        self.assertEqual((row()["alive"], row()["state"]), (True, "idle"))
        for event, kw in (("UserPromptSubmit", {}), ("SessionEnd", {"reason": "other"})):  # a -p run on the open session
            before = path.read_text()
            hook(event, headless_pid, **kw)
            self.assertEqual(path.read_text(), before, event)  # not a field changed
        self.assertEqual((row()["alive"], row()["state"]), (True, "idle"))
        # the interactive process is gone: a -p run still doesn't take the record over
        cc.save_json(path, dict(json.loads(path.read_text()), pid=999_998, pid_start=None))
        before = path.read_text()
        hook("UserPromptSubmit", headless_pid)
        self.assertEqual(path.read_text(), before)

    def test_a_duplicate_event_looks_up_no_process(self):
        build_fixtures()
        sid = "s-closed"
        self.addCleanup((cc.STATE_DIR / f"{sid}.json").unlink, missing_ok=True)
        cc.save_json(cc.STATE_DIR / f"{sid}.json", {"session_id": sid, "event": "Stop", "status": "stopped", "ts": 1000.0})
        with mock.patch.object(cc.time, "time", return_value=1000.5), \
                mock.patch.object(cc, "find_claude_ancestor", side_effect=AssertionError("looked up")):
            self.run_hook({"session_id": sid, "hook_event_name": "Stop"})

    def test_resumed_session_isnt_held_back_by_the_ended_hook(self):
        build_fixtures()
        sid = "s-midtask"  # its transcript ends on a tool entry, so an open session isn't idle
        self.run_hook({"session_id": sid, "hook_event_name": "UserPromptSubmit", "cwd": str(WORK)})
        self.run_hook({"session_id": sid, "hook_event_name": "SessionEnd", "reason": "exit"})
        saved = cc.running_claude_processes
        cc.running_claude_processes = lambda: [(2, str(WORK), ["claude", "--resume", sid])]
        try:
            row = by_id(cc.collect(True, 10 ** 6))[sid]
            self.assertTrue(row["alive"])
            self.assertNotEqual(row["state"], "idle")  # what the ended hook's status would give
            self.assertEqual(row["state"], cc.classify(dict(row), None, True))  # as if there were no hook record
        finally:
            cc.running_claude_processes = saved

    def test_peer_registry_gives_name_and_liveness(self):
        build_fixtures()
        reg = cc.CLAUDE_DIR / "sessions"
        reg.mkdir(parents=True, exist_ok=True)
        me = os.getpid()
        (reg / f"{me}.json").write_text(json.dumps({"pid": me, "procStart": registry_start(me),
                                                    "sessionId": "s-closed", "name": "work-2d"}))
        (reg / "1.json").write_text(json.dumps({"pid": 1, "procStart": "stale", "sessionId": "s-midtask", "name": "old"}))
        try:
            rows = by_id(cc.collect(True, 10 ** 6))
            self.assertEqual((rows["s-closed"]["peer_name"], rows["s-closed"]["alive"]), ("work-2d", True))
            self.assertIsNone(rows["s-midtask"]["peer_name"])  # reused pid: not that session any more
        finally:
            shutil.rmtree(reg)

    def test_mac_registry_start_time_is_utc(self):
        """Without /proc, `ps` gives local time. Claude Code's registry saves UTC (utc=True); ccdeck saves local
        time, which must only match exactly: read as UTC, it would match a process started an offset later."""
        saved_tz = os.environ.get("TZ")
        os.environ["TZ"] = "Europe/Bratislava"  # UTC+1 in November; the bug only shows off UTC
        time.tzset()
        try:
            with mock.patch.object(cc, "USE_PROC", False), \
                    mock.patch.object(cc, "proc_start_time", lambda pid: "Thu Nov 12 09:23:03 2026"):
                self.assertTrue(cc.pid_alive(7, "Thu Nov 12 09:23:03 2026"))              # ccdeck's own, local
                self.assertTrue(cc.pid_alive(7, "Thu Nov 12 08:23:03 2026", utc=True))    # the registry's, UTC
                self.assertFalse(cc.pid_alive(7, "Thu Nov 12 09:23:03 2026", utc=True))   # local string read as UTC
                self.assertFalse(cc.pid_alive(7, "Thu Nov 12 08:23:03 2026"))             # local: an hour off
                self.assertFalse(cc.pid_alive(7, "Thu Nov 12 07:23:03 2026", utc=True))   # another process
                self.assertFalse(cc.pid_alive(7, "stale", utc=True))
            # a reused pid that started one UTC offset after a saved local time is not that process
            with mock.patch.object(cc, "USE_PROC", False), \
                    mock.patch.object(cc, "proc_start_time", lambda pid: "Thu Nov 12 10:23:03 2026"):
                self.assertFalse(cc.pid_alive(7, "Thu Nov 12 09:23:03 2026"))
            with mock.patch.object(cc, "USE_PROC", False), \
                    mock.patch.object(cc, "proc_start_time", lambda pid: "Sun Mar 1 00:30:00 2026"):
                self.assertTrue(cc.pid_alive(7, "Sat Feb 28 23:30:00 2026", utc=True))   # across a day and month
            # clocks going back repeat an hour: 08:00 and 09:00 UTC are both 01:00 in Los Angeles that night,
            # and `ps` can't say which; a registry record of either moment is that process
            os.environ["TZ"] = "America/Los_Angeles"
            time.tzset()
            with mock.patch.object(cc, "USE_PROC", False), \
                    mock.patch.object(cc, "proc_start_time", lambda pid: "Sun Nov 1 01:00:00 2020"):
                self.assertTrue(cc.pid_alive(7, "Sun Nov 1 08:00:00 2020", utc=True))
                self.assertTrue(cc.pid_alive(7, "Sun Nov 1 09:00:00 2020", utc=True))
                self.assertFalse(cc.pid_alive(7, "Sun Nov 1 10:00:00 2020", utc=True))
        finally:
            if saved_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = saved_tz
            time.tzset()

    def test_registry_record_without_start_time_is_ignored(self):
        build_fixtures()
        reg = cc.CLAUDE_DIR / "sessions"
        reg.mkdir(parents=True, exist_ok=True)
        me = os.getpid()
        (reg / f"{me}.json").write_text(json.dumps({"pid": me, "sessionId": "s-closed", "name": "work-2d"}))
        try:
            self.assertEqual(cc.peer_sessions(), {})  # the pid is alive, but it may not be that session any more
        finally:
            shutil.rmtree(reg)

    def test_malformed_registry_records_are_skipped(self):
        reg = cc.CLAUDE_DIR / "sessions"
        reg.mkdir(parents=True, exist_ok=True)
        me, start = os.getpid(), registry_start(os.getpid())
        for i, rec in enumerate(({"pid": me, "procStart": start, "sessionId": ["A"]},
                                 {"pid": me, "procStart": start, "sessionId": ""},
                                 {"pid": str(me), "procStart": start, "sessionId": "B"},
                                 [1, 2])):
            (reg / f"{i}.json").write_text(json.dumps(rec))
        (reg / "ok.json").write_text(json.dumps({"pid": me, "procStart": start, "sessionId": "C", "name": {"x": 1}}))
        try:
            self.assertEqual(cc.peer_sessions(), {"C": {"name": None, "pid": me, "pids": [me]}})
        finally:
            shutil.rmtree(reg)

    def test_two_processes_on_one_registered_session_leave_no_slot(self):
        # both processes in the folder run registered session B; closed, hookless sessions there stay closed
        build_fixtures()
        me, parent = os.getpid(), os.getppid()
        saved = cc.peer_sessions, cc.running_claude_processes
        cc.peer_sessions = lambda: {"s-closed": {"name": "b", "pid": me, "pids": [me, parent]}}
        cc.running_claude_processes = lambda: [(me, str(WORK), ["claude"]), (parent, str(WORK), ["claude"])]
        try:
            rows = by_id(cc.collect(True, 10 ** 6))
            self.assertEqual({k for k, r in rows.items() if r["alive"]}, {"s-closed"})
        finally:
            cc.peer_sessions, cc.running_claude_processes = saved

    def test_two_processes_on_one_session_both_count(self):
        reg = cc.CLAUDE_DIR / "sessions"
        reg.mkdir(parents=True, exist_ok=True)
        me, parent = os.getpid(), os.getppid()
        for pid in (me, parent):
            (reg / f"{pid}.json").write_text(json.dumps({"pid": pid, "procStart": registry_start(pid), "sessionId": "B"}))
        try:
            self.assertEqual(sorted(cc.peer_sessions()["B"]["pids"]), sorted([me, parent]))
        finally:
            shutil.rmtree(reg)

    def test_a_hook_whose_process_now_runs_another_session_is_stale(self):
        # this process wrote A's hook (asking for permission), then /resume switched it to B
        build_fixtures()
        a, b, me = "s-midtask", "s-closed", os.getpid()
        self.run_hook({"session_id": a, "hook_event_name": "UserPromptSubmit", "cwd": str(WORK)})
        self.run_hook({"session_id": a, "hook_event_name": "Notification",
                       "notification_type": "permission_prompt", "message": "Claude needs your permission to use Bash"})
        state_file = cc.STATE_DIR / f"{a}.json"
        st = json.loads(state_file.read_text())
        st["pid"], st["pid_start"] = me, cc.proc_start_time(me)
        state_file.write_text(json.dumps(st))
        saved = cc.peer_sessions, cc.running_claude_processes
        cc.peer_sessions = lambda: {b: {"name": "b", "pid": me, "pids": [me]}}
        cc.running_claude_processes = lambda: [(me, str(WORK), ["claude"])]
        try:
            rows = by_id(cc.collect(True, 10 ** 6))
            self.assertTrue(rows[b]["alive"])
            self.assertFalse(rows[a]["alive"])
            self.assertNotEqual(rows[a]["state"], "permission")
        finally:
            cc.peer_sessions, cc.running_claude_processes = saved
            state_file.unlink(missing_ok=True)  # it names this live process: don't leak it into other tests

    def test_a_process_that_switched_sessions_counts_once(self):
        # pid 1 started with --resume A, then switched to B (/resume); pid 2 runs hookless C in the same folder
        build_fixtures()
        saved = cc.peer_sessions, cc.running_claude_processes
        cc.peer_sessions = lambda: {"s-closed": {"name": "b", "pid": 1, "pids": [1]}}
        cc.running_claude_processes = lambda: [(1, str(WORK), ["claude", "--resume", "s-midtask"]), (2, str(WORK), ["claude"])]
        try:
            rows = by_id(cc.collect(True, 10 ** 6))
            self.assertTrue(rows["s-closed"]["alive"])     # B: the registry says pid 1 runs it
            self.assertFalse(rows["s-midtask"]["alive"])   # A: pid 1's stale --resume argument
            live = [k for k, r in rows.items() if r["alive"]]
            self.assertEqual(len(live), 2, live)           # pid 2's session is still guessed open
        finally:
            cc.peer_sessions, cc.running_claude_processes = saved

    def test_resumed_elsewhere_drops_the_ended_processs_permission(self):
        build_fixtures()
        sid = "s-closed"
        self.run_hook({"session_id": sid, "hook_event_name": "UserPromptSubmit", "cwd": str(WORK)})
        self.run_hook({"session_id": sid, "hook_event_name": "Notification",
                       "notification_type": "permission_prompt", "message": "Claude needs your permission to use Bash"})
        state_file = cc.STATE_DIR / f"{sid}.json"
        st = json.loads(state_file.read_text())
        st["pid"], st["pid_start"] = 1, "gone"  # the process that asked has ended
        state_file.write_text(json.dumps(st))
        saved = cc.running_claude_processes
        cc.running_claude_processes = lambda: [(2, str(WORK), ["claude", "--resume", sid])]
        try:
            row = by_id(cc.collect(True, 10 ** 6))[sid]
            self.assertTrue(row["alive"])
            self.assertNotEqual(row["state"], "permission")
            self.assertIsNone(row["hook_message"])
        finally:
            cc.running_claude_processes = saved

    def test_bad_payloads_ignored(self):
        for p in ([1], "x", {"session_id": "../../etc"}, {}):
            self.run_hook(p)
        self.assertFalse((cc.DATA_DIR / "etc.json").exists())


class ApprovalHookTest(unittest.TestCase):
    """The PermissionRequest hook, run as Claude Code runs it: a subprocess reading JSON on stdin."""

    SID = "s-approve"

    def setUp(self):
        import subprocess
        self.subprocess = subprocess
        shutil.rmtree(cc.REQ_DIR, ignore_errors=True)
        cc.save_json(cc.DAEMON_FILE, {"pid": os.getpid(), "port": 0, "ts": time.time()})
        self.env = dict(os.environ, CCDECK_APPROVALS="1", CCDECK_APPROVAL_WAIT="15", CCDECK_AUTOSTART="0")

    def tearDown(self):
        for p in getattr(self, "procs", []):
            if p.poll() is None:
                p.kill()
        try:
            cc.DAEMON_FILE.unlink()
        except OSError:
            pass

    def hook(self, payload, env=None):
        p = self.subprocess.Popen([sys.executable, str(ROOT / "ccdeck"), "hook"], env=env or self.env,
                                  stdin=self.subprocess.PIPE, stdout=self.subprocess.PIPE, text=True)
        self.procs = getattr(self, "procs", []) + [p]
        p.stdin.write(json.dumps(payload))
        p.stdin.close()
        return p

    def ask(self, command="npm run deploy", **kw):
        return self.hook(dict({"session_id": self.SID, "hook_event_name": "PermissionRequest",
                               "cwd": str(WORK), "tool_name": "Bash",
                               "tool_input": {"command": command}}, **kw))

    def wait_request(self):
        for _ in range(100):
            reqs = cc.open_requests(self.SID)
            if reqs:
                return reqs[0]
            time.sleep(0.05)
        self.fail("no request file appeared")

    def answer(self, req, decision, h=None):
        cc.write_private(cc.REQ_DIR / f"{req['id']}.answer.json",
                         {"id": req["id"], "input_hash": h or req["input_hash"], "decision": decision})

    def finish(self, p, timeout=5):
        out = p.stdout.read()
        p.stdout.close()
        p.wait(timeout=timeout)
        return out.strip()

    def test_allow_from_dashboard(self):
        p = self.ask()
        req = self.wait_request()
        self.assertEqual((req["tool_name"], req["tool_input"]), ("Bash", {"command": "npm run deploy"}))
        self.assertEqual(oct(os.stat(cc.REQ_DIR / f"{req['id']}.json").st_mode & 0o777), "0o600")
        self.answer(req, {"behavior": "allow", "updatedInput": {"command": "rm -rf /"}})
        out = json.loads(self.finish(p))
        # Only behavior is passed on: the dashboard can't rewrite the command.
        self.assertEqual(out, {"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                                      "decision": {"behavior": "allow"}}})
        self.assertEqual(list(cc.REQ_DIR.iterdir()), [])

    def test_deny_with_message(self):
        p = self.ask()
        req = self.wait_request()
        self.answer(req, {"behavior": "deny", "message": "not on a Friday"})
        out = json.loads(self.finish(p))
        self.assertEqual(out["hookSpecificOutput"]["decision"], {"behavior": "deny", "message": "not on a Friday"})

    def test_answer_for_other_input_is_ignored(self):
        p = self.ask()
        req = self.wait_request()
        self.answer(req, {"behavior": "allow"}, h="0" * 64)
        self.assertEqual(self.finish(p), "")

    def test_answered_in_terminal_closes_request(self):
        p = self.ask()
        self.wait_request()
        done = self.hook({"session_id": self.SID, "hook_event_name": "PostToolUse", "tool_name": "Bash",
                          "tool_input": {"command": "npm run deploy"}, "tool_response": {}})
        self.finish(done)
        t = time.time()
        self.assertEqual(self.finish(p), "")
        self.assertLess(time.time() - t, 3)

    def test_denied_in_terminal_closes_on_next_prompt(self):
        p = self.ask()
        self.wait_request()
        self.finish(self.hook({"session_id": self.SID, "hook_event_name": "UserPromptSubmit", "prompt": "no"}))
        self.assertEqual(self.finish(p), "")

    def test_off_by_default_and_without_daemon(self):
        env = dict(self.env)
        del env["CCDECK_APPROVALS"]
        t = time.time()
        self.assertEqual(self.finish(self.hook({"session_id": self.SID, "hook_event_name": "PermissionRequest",
                                                "tool_name": "Bash", "tool_input": {"command": "ls"}}, env=env)), "")
        cc.DAEMON_FILE.unlink()
        self.assertEqual(self.finish(self.ask()), "")
        self.assertLess(time.time() - t, 3)
        self.assertEqual(cc.open_requests(self.SID), [])

    def test_valid_decision(self):
        req = {"id": "a", "input_hash": "h"}
        ok = lambda d: cc.valid_decision({"id": "a", "input_hash": "h", "decision": d}, req)
        self.assertIsNone(ok({"behavior": "ask"}))
        self.assertIsNone(ok("allow"))
        self.assertEqual(ok({"behavior": "deny", "interrupt": True}), {"behavior": "deny", "interrupt": True})
        self.assertIsNone(cc.valid_decision({"id": "b", "input_hash": "h", "decision": {"behavior": "allow"}}, req))


class InstallTest(unittest.TestCase):
    def setUp(self):
        cc.SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)

    def args(self):
        import argparse
        return argparse.Namespace(no_hooks=False, no_service=True, port=7799)

    def test_install_keeps_other_settings_and_uninstalls(self):
        cc.SETTINGS_FILE.write_text(json.dumps({"model": "opus", "note": "žluťoučký",
                                                "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "mine"}]}]}}))
        cc.cmd_install(self.args())
        cc.cmd_install(self.args())  # idempotent
        s = json.loads(cc.SETTINGS_FILE.read_text())
        self.assertEqual(s["note"], "žluťoučký")
        self.assertEqual(len(s["hooks"]["Stop"]), 2)
        self.assertTrue(cc.hooks_installed())
        cc.cmd_uninstall(None)
        s = json.loads(cc.SETTINGS_FILE.read_text())
        self.assertEqual(s["hooks"], {"Stop": [{"hooks": [{"type": "command", "command": "mine"}]}]})

    def test_refuses_unreadable_settings(self):
        cc.SETTINGS_FILE.write_text('{"model": "x", "bad')
        with self.assertRaises(SystemExit):
            cc.cmd_install(self.args())
        self.assertEqual(cc.SETTINGS_FILE.read_text(), '{"model": "x", "bad')
        cc.SETTINGS_FILE.unlink()


class PluginInstallTest(unittest.TestCase):
    """`ccdeck install` run from Claude Code's plugin cache hands over to a stable copy."""

    def plugin_copy(self, version):
        root = TMP / ".claude" / "plugins" / "cache" / "ccdeck" / "ccdeck" / version
        shutil.rmtree(root, ignore_errors=True)
        (root / "web").mkdir(parents=True)
        src = (ROOT / "ccdeck").read_text().replace('VERSION = "%s"' % cc.VERSION, 'VERSION = "%s"' % version)
        (root / "ccdeck").write_text(src)
        (root / "ccdeck").chmod(0o755)
        for f in (ROOT / "web").iterdir():
            shutil.copy(f, root / "web" / f.name)
        return root

    def run_ccdeck(self, script, *args, stdin=None):
        import subprocess
        env = dict(os.environ, CCDECK_AUTOSTART="0")
        env.pop("CCDECK_FROM_PLUGIN", None)
        return subprocess.run([sys.executable, str(script), *args], input=stdin, capture_output=True, text=True, env=env, timeout=30)

    def tearDown(self):
        shutil.rmtree(cc.STABLE_DIR, ignore_errors=True)
        link = TMP / ".local" / "bin" / "ccdeck"
        if link.is_symlink():
            link.unlink()

    def test_install_from_plugin_uses_stable_copy(self):
        root = self.plugin_copy("9.1.0")
        r = self.run_ccdeck(root / "ccdeck", "install", "--no-hooks", "--no-service")
        self.assertEqual(r.returncode, 0, r.stderr)
        link = TMP / ".local" / "bin" / "ccdeck"
        # resolve(): on macOS the temp HOME lives under /var, a symlink to /private/var
        self.assertEqual(Path(os.readlink(link)).resolve(), (cc.STABLE_DIR / "ccdeck").resolve())
        self.assertEqual((cc.STABLE_DIR / "VERSION").read_text().strip(), "9.1.0")
        self.assertTrue((cc.STABLE_DIR / "web" / "index.html").exists())

    def test_plugin_update_refreshes_stable_copy(self):
        self.run_ccdeck(self.plugin_copy("9.1.0") / "ccdeck", "install", "--no-hooks", "--no-service")
        newer = self.plugin_copy("9.2.0")
        self.run_ccdeck(newer / "ccdeck", "hook",
                        stdin=json.dumps({"session_id": "s-up", "hook_event_name": "SessionStart"}))
        self.assertEqual((cc.STABLE_DIR / "VERSION").read_text().strip(), "9.2.0")
        self.assertIn('VERSION = "9.2.0"', (cc.STABLE_DIR / "ccdeck").read_text())

    def test_hooks_report_newer_version_to_daemon(self):
        newer = self.plugin_copy("9.3.0")
        self.run_ccdeck(newer / "ccdeck", "hook",
                        stdin=json.dumps({"session_id": "s-newer", "hook_event_name": "SessionStart"}))
        st = json.loads((cc.STATE_DIR / "s-newer.json").read_text())
        self.assertEqual(st["ccdeck"]["version"], "9.3.0")
        found = cc.newer_install()
        self.assertEqual(found[0], "9.3.0")
        self.assertEqual(Path(found[1]).resolve(), (newer / "ccdeck").resolve())
        # a state file that claims a version its script doesn't have is ignored
        st["ccdeck"]["version"] = "99.0.0"
        st["ccdeck"]["script"] = str(ROOT / "ccdeck")
        (cc.STATE_DIR / "s-newer.json").write_text(json.dumps(st))
        self.assertIsNone(cc.newer_install())
        (cc.STATE_DIR / "s-newer.json").unlink()

    def test_adopt_stable_copy_moves_service(self):
        saved = (cc.running_from_plugin, cc.service_installed, cc.systemctl)
        calls = []
        cc.running_from_plugin = lambda: True
        cc.service_installed = lambda: True
        cc.systemctl = lambda *a: calls.append(a)
        cc.UNIT_FILE.parent.mkdir(parents=True, exist_ok=True)
        old = "/root/.claude/plugins/cache/ccdeck/ccdeck/0.4.0/ccdeck"
        cc.UNIT_FILE.write_text(f"[Service]\nExecStart=/usr/bin/python3 {old} serve --port 7799\n")
        try:
            cc.adopt_stable_copy()
            self.assertIn(f"ExecStart=/usr/bin/python3 {cc.STABLE_DIR / 'ccdeck'} serve --port 7799", cc.UNIT_FILE.read_text())
            self.assertIn(("daemon-reload",), calls)
            self.assertTrue((cc.STABLE_DIR / "ccdeck").exists())
        finally:
            cc.running_from_plugin, cc.service_installed, cc.systemctl = saved
            cc.UNIT_FILE.unlink()


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        build_fixtures()
        cls.dash = cc.Dashboard(0)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), cls.dash.make_handler())
        cls.dash.port = cls.httpd.server_address[1]
        cls.dash.refresh()
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.dash.port}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def req(self, path, token=True, host=None, data=None):
        r = urllib.request.Request(self.base + path, data=json.dumps(data).encode() if data is not None else None)
        if token:
            r.add_header("X-CC-Token", self.dash.token)
        if host:
            r.add_header("Host", host)
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_inbox_keeps_only_what_wasnt_acted_on(self):
        shutil.rmtree(cc.INBOX_DIR, ignore_errors=True)
        rows = [{"id": "a", "state": "permission"}, {"id": "b", "state": "idle"}, {"id": "c", "state": "question"}]
        na = cc.inbox_add("a", "permission", "Needs approval · a", "Bash: rm -rf build")
        cc.inbox_add("b", "idle", "Your turn · b", "Done.")
        cc.inbox_add("c", "question", "Question · c", "Which one?")
        cc.inbox_add("gone", "idle", "Your turn · gone", "")
        cc.inbox_sweep(rows)
        self.assertEqual({d["sid"] for d in cc.inbox_items()}, {"a", "b", "c"})  # the gone session's is dropped
        rows[1]["state"] = "working"  # you replied in b
        rows[2]["state"] = "closed"   # c was closed
        cc.inbox_sweep(rows)
        cq = cc.inbox_add("c", "question", "Question · c", "Which one?")
        rows[2].update(state="question", alive=False)  # closed with the question unanswered: same state
        cc.inbox_sweep(rows)
        self.assertNotIn(cq, [d["id"] for d in cc.inbox_items()])
        ci = cc.inbox_add("c", "ci_failing", "CI failing · c", "")
        rows[2]["state"] = "ci_failing"  # its PR failed after it closed: news about the PR, it stays
        cc.inbox_sweep(rows)
        self.assertIn(ci, [d["id"] for d in cc.inbox_items()])
        cc.inbox_clear(ci)
        self.assertEqual([d["id"] for d in cc.inbox_items()], [na])
        na2 = cc.inbox_add("a", "permission", "Needs approval · a", "Bash: make deploy")
        self.assertEqual([d["id"] for d in cc.inbox_items()], [na2])  # one per session, the newest

        # dismissing through the API (the fixture's sessions are closed: keep the sweep, tested above, out of it)
        shutil.rmtree(cc.INBOX_DIR, ignore_errors=True)
        sweep = mock.patch.object(cc, "inbox_sweep", lambda rows: None)
        sweep.start()
        self.addCleanup(sweep.stop)
        st = {x["id"]: x["state"] for x in json.loads(self.req("/api/sessions")[1])["sessions"]}
        n1, n2 = cc.inbox_add("s-closed", st["s-closed"], "One", ""), cc.inbox_add("s-midtask", st["s-midtask"], "Two", "")
        self.assertEqual(self.req("/api/action", token=False, data={"action": "dismiss", "nid": n1})[0], 403)
        self.assertEqual(self.req("/api/action", data={"action": "dismiss", "nid": "../token"})[0], 200)  # ignored
        self.assertTrue(cc.TOKEN_FILE.exists())
        self.assertEqual(self.req("/api/action", data={"action": "dismiss", "nid": n1})[0], 200)
        snap = json.loads(self.req("/api/sessions")[1])
        self.assertEqual([n["id"] for n in snap["notifications"]], [n2])
        self.assertEqual(self.req("/api/action", data={"action": "dismiss_all"})[0], 200)
        self.assertEqual(cc.inbox_items(), [])

    def test_page_has_token(self):
        code, body = self.req("/", token=False)
        self.assertEqual(code, 200)
        self.assertIn(self.dash.token.encode(), body)

    def test_stylesheet_served(self):
        code, body = self.req("/ccdeck.css", token=False)
        self.assertEqual(code, 200)
        self.assertIn(b'data-theme="alpine-gm3a"', body)
        self.assertEqual(self.req("/../ccdeck", token=False)[0], 404)

    def test_api_requires_token_and_host(self):
        self.assertEqual(self.req("/api/sessions", token=False)[0], 403)
        self.assertEqual(self.req("/api/sessions", host="evil.example:80")[0], 403)
        code, body = self.req("/api/sessions")
        self.assertEqual(code, 200)
        snap = json.loads(body)
        self.assertIn("s-decision", {s["id"] for s in snap["sessions"]})

    def test_actions(self):
        self.assertEqual(self.req("/api/action", token=False, data={"id": "s-closed", "action": "done"})[0], 403)
        self.assertEqual(self.req("/api/action", data={"id": "nope", "action": "done"})[0], 404)
        self.assertEqual(self.req("/api/action", data={"id": "s-closed", "action": "rm -rf"})[0], 400)
        self.assertEqual(self.req("/api/action", data={"id": "s-closed", "action": "note", "text": "QA"})[0], 200)
        snap = json.loads(self.req("/api/sessions")[1])
        self.assertEqual({s["id"]: s for s in snap["sessions"]}["s-closed"]["mark"]["note"], "QA")
        self.req("/api/action", data={"id": "s-closed", "action": "note", "text": ""})

    def test_settings_from_the_dashboard(self):
        """The settings panel reads settings from the snapshot and saves them with set_setting."""
        self.addCleanup(lambda: cc.CONFIG_FILE.unlink(missing_ok=True))
        snap = json.loads(self.req("/api/sessions")[1])
        self.assertEqual(snap["settings"]["review_requests"], {"value": "all", "locked": None})
        self.assertEqual(snap["settings"]["notifications"]["desktop"]["locked"], "CCDECK_NOTIFY=0")  # set by the tests
        set_ = lambda key, value: self.req("/api/action", data={"action": "set_setting", "key": key, "value": value})
        code, body = set_("review_requests", "mine")
        self.assertEqual((code, json.loads(body)["settings"]["review_requests"]["value"]), (200, "mine"))
        self.assertEqual(json.loads(self.req("/api/sessions")[1])["settings"]["review_requests"]["value"], "mine")
        self.assertEqual(set_("approvals", True)[0], 200)
        self.assertEqual(set_("notifications.desktop", False)[0], 200)
        self.assertEqual(cc.load_json(cc.CONFIG_FILE, {}),
                         {"review_requests": "mine", "approvals": True, "notifications": {"desktop": False}})
        for key, value in (("review_requests", "team"), ("review_requests", []), ("review_requests", {}),
                           ("approvals", "yes"), ("notifications.sms", True), ("token", "x"), (None, 1)):
            self.assertEqual(set_(key, value)[0], 400, key)
        self.assertEqual(self.req("/api/action", token=False, data={"action": "set_setting", "key": "approvals", "value": True})[0], 403)

    def test_concurrent_settings_changes_all_kept(self):
        """Each change reads config.json and saves it whole: without the lock, one would undo another."""
        self.addCleanup(lambda: cc.CONFIG_FILE.unlink(missing_ok=True))
        cc.CONFIG_FILE.unlink(missing_ok=True)
        real_load = cc.load_json
        def slow_load(path, default):  # widen the gap between reading and saving
            v = real_load(path, default)
            time.sleep(0.05)
            return v
        changes = [("review_requests", "mine"), ("approvals", True), ("notifications.desktop", False)] * 3
        with mock.patch.object(cc, "load_json", slow_load):
            threads = [threading.Thread(target=cc.set_setting, args=c) for c in changes]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(cc.load_json(cc.CONFIG_FILE, {}),
                         {"review_requests": "mine", "approvals": True, "notifications": {"desktop": False}})

    def test_resume_of_an_open_session_never_starts_a_second_one(self):
        saved = cc.peer_sessions, cc.focus_terminal, cc.open_terminal
        opened = []
        cc.peer_sessions = lambda: {"s-closed": {"name": "work-2d", "pid": 1, "pids": [1]}}
        cc.open_terminal = lambda *a: opened.append(a) or "xterm"
        try:
            cc.focus_terminal = lambda pid: "gnome-terminal"
            code, body = self.req("/api/action", data={"id": "s-closed", "action": "resume"})
            self.assertEqual((code, json.loads(body)["focused"]), (200, "gnome-terminal"))
            cc.focus_terminal = lambda pid: None
            code, body = self.req("/api/action", data={"id": "s-closed", "action": "resume"})
            self.assertEqual(code, 409)
            self.assertIn("@work-2d", json.loads(body)["error"])
            # the name chip's focus: tries every tab in turn, never starts Claude when the tab is gone
            cc.peer_sessions = lambda: {"s-closed": {"name": "work-2d", "pid": 1, "pids": [1, 2]}}
            cc.focus_terminal = lambda pid: "tmux" if pid == 2 else None
            code, body = self.req("/api/action", data={"id": "s-closed", "action": "focus"})
            self.assertEqual((code, json.loads(body)["focused"]), (200, "tmux"))
            cc.focus_terminal = lambda pid: "gnome-terminal"
            code, body = self.req("/api/action", data={"id": "s-closed", "action": "focus"})
            self.assertEqual((code, json.loads(body)["focused"]), (200, "gnome-terminal"))
            cc.peer_sessions = lambda: {}
            code, body = self.req("/api/action", data={"id": "s-closed", "action": "focus"})
            self.assertEqual(code, 409)
            self.assertIn("isn't open any more", json.loads(body)["error"])
            self.assertEqual(opened, [])
        finally:
            cc.peer_sessions, cc.focus_terminal, cc.open_terminal = saved

    def test_notification_click_goes_to_the_tab_even_if_the_inbox_cant_be_written(self):
        focused = []
        def fail(nid):
            raise PermissionError("inbox")
        with mock.patch.object(cc, "peer_sessions", lambda: {"s-closed": {"name": "w", "pid": 42, "pids": [42]}}), \
                mock.patch.object(cc, "focus_terminal", lambda pid, token: focused.append(pid)), \
                mock.patch.object(cc, "notification_click", lambda *a: (True, "tok")), \
                mock.patch.object(cc, "inbox_clear", fail), \
                mock.patch.object(sys, "argv", ["ccdeck", "notify-click", "s-closed", "--id", "n1", "--", "t", "b"]):
            cc.main()
        self.assertEqual(focused, [42])

    def test_notification_click_goes_to_the_sessions_tab(self):
        saved = cc.peer_sessions, cc.focus_terminal, cc.notification_click, sys.argv
        focused = []
        cc.peer_sessions = lambda: {"s-closed": {"name": "work-2d", "pid": 42, "pids": [42]}}
        cc.focus_terminal = lambda pid, token: focused.append((pid, token))
        def boom(*a):
            raise RuntimeError("no session bus")
        try:
            for click, sid in ((lambda *a: (True, "tok"), "s-closed"), (lambda *a: (False, None), "s-closed"),
                               (lambda *a: (True, None), "gone"), (boom, "s-closed")):  # *a: title, body, urgent, wanted
                cc.notification_click = click
                sys.argv = ["ccdeck", "notify-click", sid, "--", "-title", "body"]
                cc.main()
            self.assertEqual(focused, [(42, "tok")])
        finally:
            cc.peer_sessions, cc.focus_terminal, cc.notification_click, sys.argv = saved

    def test_mac_notification_click_runs_focus(self):
        """With terminal-notifier, a click runs `ccdeck focus <sid>`; if it fails (not allowed: exit 3) or is
        missing, the notification goes out through osascript instead (not clickable, but not lost)."""
        def send(which, code):
            calls = []
            def run(cmd, **kw):
                calls.append(cmd)
                if code == "timeout":  # subprocess.run kills it before raising: nothing was delivered
                    raise cc.subprocess.TimeoutExpired(cmd, kw.get("timeout"))
                return mock.Mock(returncode=code)
            with mock.patch.object(cc, "IS_MAC", True), mock.patch.object(cc, "notify_enabled", lambda c: True), \
                    mock.patch.object(cc.subprocess, "run", run), \
                    mock.patch.object(cc.subprocess, "Popen", lambda cmd, **kw: calls.append(cmd)), \
                    mock.patch.object(cc.shutil, "which", lambda b: which if b == "terminal-notifier" else None):
                cc.notify("Permission needed · Deploy", "Bash: deploy.sh", urgent=True, sid="s-closed")
            return calls
        calls = send("/opt/bin/terminal-notifier", 0)
        self.assertEqual(len(calls), 1)
        cmd = calls[0]
        self.assertEqual(cmd[0], "/opt/bin/terminal-notifier")
        self.assertEqual(cmd[cmd.index("-group") + 1], "ccdeck-s-closed")
        self.assertEqual(cmd[cmd.index("-sound") + 1], "Glass")
        self.assertEqual(shlex.split(cmd[cmd.index("-execute") + 1])[-2:], ["focus", "s-closed"])
        self.assertEqual([c[0] for c in send("/opt/bin/terminal-notifier", 3)], ["/opt/bin/terminal-notifier", "osascript"])
        self.assertEqual([c[0] for c in send("/opt/bin/terminal-notifier", "timeout")], ["/opt/bin/terminal-notifier", "osascript"])
        self.assertEqual([c[0] for c in send(None, 0)], ["osascript"])

    def test_notification_setup_on_mac(self):
        """`ccdeck notifications setup`: a hint without terminal-notifier, macOS's prompt when it was never
        asked, System Settings when it's denied."""
        def setup(which, auths, interactive=False, version=(3, 1)):
            opened, out = [], []
            auths = iter(auths)
            with mock.patch.object(cc, "IS_MAC", True), mock.patch.object(cc, "terminal_notifier_version", lambda: version), \
                    mock.patch.object(cc.shutil, "which", lambda b: which if b == "terminal-notifier" else None), \
                    mock.patch.object(cc, "terminal_notifier_authorization", lambda: next(auths)), \
                    mock.patch.object(cc, "terminal_notifier_app", lambda: Path("/Apps/terminal-notifier.app")), \
                    mock.patch.object(cc.subprocess, "run", lambda cmd, **kw: opened.append(cmd)), \
                    mock.patch.object(cc, "open_path", lambda t: opened.append(t)), \
                    mock.patch.object(cc.time, "sleep", lambda s: None), \
                    mock.patch("builtins.print", lambda *a, **k: out.append(" ".join(map(str, a)))):
                cc.setup_notifications(interactive)
            return opened, "\n".join(out)
        opened, out = setup(None, [])
        self.assertIn("brew install terminal-notifier", out)
        self.assertEqual(opened, [])
        opened, out = setup("/opt/bin/terminal-notifier", ["not requested yet", "authorized"], interactive=True)
        self.assertEqual(opened[0][:4], ["open", "-W", "-a", "/Apps/terminal-notifier.app"])
        self.assertIn("clickable", out)
        opened, out = setup("/opt/bin/terminal-notifier", ["denied"], interactive=True)
        self.assertIn("System Settings", out)
        self.assertEqual(opened, [cc.NOTIFY_SETTINGS_URL])
        opened, out = setup("/opt/bin/terminal-notifier", ["denied"])  # not interactive: say it, open nothing
        self.assertIn("System Settings", out)
        self.assertEqual(opened, [])
        # 2.x has no -diagnose: ask for an upgrade instead of reading "not allowed" into its silence
        opened, out = setup("/opt/bin/terminal-notifier", [], version=(2, 0))
        self.assertIn("brew upgrade terminal-notifier", out)
        self.assertNotIn("System Settings", out)
        self.assertEqual(opened, [])

    def test_terminal_notifier_app_behind_homebrews_script(self):
        root = TMP / "tn-brew"
        app = root / "Cellar" / "terminal-notifier" / "3.1.0" / "terminal-notifier.app"
        (app / "Contents" / "MacOS").mkdir(parents=True, exist_ok=True)
        script = root / "Cellar" / "terminal-notifier" / "3.1.0" / "bin" / "terminal-notifier"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(f'#!/bin/bash\nexec "{app}/Contents/MacOS/terminal-notifier" "$@"\n')
        with mock.patch.object(cc.shutil, "which", lambda b: str(script)):
            self.assertEqual(cc.terminal_notifier_app().resolve(), app.resolve())  # /var is /private/var on macOS

    def test_focus_command(self):
        focused = []
        cc.inbox_add("s-closed", "permission", "Needs approval", "Bash: deploy")
        keep = cc.inbox_add("other", "idle", "Your turn", "")
        with mock.patch.object(cc, "peer_sessions", lambda: {"s-closed": {"name": "w", "pid": 42, "pids": [42]}}), \
                mock.patch.object(cc, "focus_terminal", lambda pid: focused.append(pid) or "Terminal"), \
                mock.patch.object(sys, "argv", ["ccdeck", "focus", "s-closed"]):
            cc.main()
        self.assertEqual(focused, [42])
        self.assertEqual([d["id"] for d in cc.inbox_items()], [keep])  # clicking it on macOS is acting on it
        with mock.patch.object(cc, "peer_sessions", lambda: {}), mock.patch.object(sys, "argv", ["ccdeck", "focus", "gone"]):
            with self.assertRaises(SystemExit):
                cc.main()

    def test_activation_token_goes_to_gnome_terminal_first(self):
        saved = cc.gnome_terminal_window
        cc.gnome_terminal_window = lambda: ("/org/gnome/Terminal/window/3", 2)
        env = {"GNOME_TERMINAL_SCREEN": "/org/gnome/Terminal/screen/" + "a" * 8 + "_" + "b" * 4 + "_" + "c" * 4 + "_" + "d" * 4 + "_" + "e" * 12}
        try:
            if not shutil.which("gdbus"):
                self.skipTest("needs gdbus")
            (label, cmds), = cc.focus_commands(env, None, "gnome-shell//1-2-host_TIME3")
            self.assertEqual(label, "gnome-terminal")
            self.assertEqual(cmds[0][cmds[0].index("-o") + 1], "/org/gnome/Terminal/window/3")
            self.assertIn("<'gnome-shell//1-2-host_TIME3'>", cmds[0][-1])
            self.assertEqual(cmds[0][cmds[0].index("--") + 1:-1], ["active-tab", "<2>"])  # the tab already active: no change
            self.assertIn("ActivateResult", " ".join(cmds[1]))
            (_, cmds), = cc.focus_commands(env, None, "x'] evil")  # not token-shaped: no window call
            self.assertEqual(len(cmds), 1)
            (_, cmds), = cc.focus_commands(env, None)
            self.assertEqual(len(cmds), 1)
        finally:
            cc.gnome_terminal_window = saved


class InboxClickTest(unittest.TestCase):
    def test_concurrent_notifications_for_one_session_leave_one_entry(self):
        shutil.rmtree(cc.INBOX_DIR, ignore_errors=True)
        threads = [threading.Thread(target=cc.inbox_add, args=("s7", "idle", f"n{i}", "")) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len([d for d in cc.inbox_items() if d["sid"] == "s7"]), 1)
        shutil.rmtree(cc.INBOX_DIR, ignore_errors=True)

    def test_the_inbox_keeps_the_whole_message(self):
        long = "Should I roll back the canary?\n\n" + "Details. " * 2000  # well past the dashboard's message limit
        cc.notify("Question · deploy", cc.one_line(long, 160), sid="s9", state="question", text=long)
        (entry,) = [d for d in cc.inbox_items() if d["sid"] == "s9"]
        self.assertEqual(entry["body"], long)  # CCDECK_NOTIFY=0 here: no popup, the entry is still kept
        cc.inbox_clear(entry["id"])

    def test_click_clears_the_entry_and_a_gone_entry_withdraws_the_popup(self):
        saved = cc.peer_sessions, cc.focus_terminal, cc.notification_click, sys.argv
        cc.peer_sessions = lambda: {}
        cc.focus_terminal = lambda pid, token: None
        seen = []
        try:
            nid = cc.inbox_add("s1", "idle", "Your turn · s1", "Done.")
            def click(title, body, urgent, wanted):
                seen.append(wanted())  # entry still there: keep showing it
                return True, None
            cc.notification_click = click
            sys.argv = ["ccdeck", "notify-click", "s1", "--id", nid, "--", "t", "b"]
            cc.main()
            self.assertEqual(seen, [True])
            self.assertFalse((cc.INBOX_DIR / f"{nid}.json").exists())  # clicking it is acting on it
            nid = cc.inbox_add("s1", "idle", "Your turn · s1", "Done.")
            def dismissed_elsewhere(title, body, urgent, wanted):
                cc.inbox_clear(nid)  # e.g. you replied in the session
                seen.append(wanted())
                return False, None
            cc.notification_click = dismissed_elsewhere
            sys.argv = ["ccdeck", "notify-click", "s1", "--id", nid, "--", "t", "b"]
            cc.main()
            self.assertEqual(seen, [True, False])  # the helper withdraws the popup once wanted() is False
        finally:
            cc.peer_sessions, cc.focus_terminal, cc.notification_click, sys.argv = saved


class NotificationSettingTest(unittest.TestCase):
    def setUp(self):
        self.env = os.environ.pop("CCDECK_NOTIFY", None)
        cc.CONFIG_FILE.unlink(missing_ok=True)

    def tearDown(self):
        os.environ["CCDECK_NOTIFY"] = self.env or "0"
        cc.CONFIG_FILE.unlink(missing_ok=True)

    def run_cmd(self, *words):
        cc.cmd_notifications(type("A", (), {"words": list(words)}))

    def test_on_off_per_channel_and_env_override(self):
        self.assertTrue(cc.notify_enabled("desktop"))  # default on
        self.run_cmd("desktop", "off")
        self.assertFalse(cc.notify_enabled("desktop"))
        self.run_cmd("on")  # all channels
        self.assertTrue(cc.notify_enabled("desktop"))
        os.environ["CCDECK_NOTIFY"] = "0"
        self.assertFalse(cc.notify_enabled("desktop"))
        with self.assertRaises(SystemExit):
            self.run_cmd("pager", "on")
        with self.assertRaises(SystemExit):
            self.run_cmd("maybe")

    def test_bad_config_values_count_as_defaults(self):
        for bad in ({"notifications": None}, {"notifications": [1]}, [1, 2]):
            cc.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
            cc.CONFIG_FILE.write_text(json.dumps(bad))
            self.assertTrue(cc.notify_enabled("desktop"))
            self.run_cmd("desktop", "off")  # repairs it
            self.assertFalse(cc.notify_enabled("desktop"))


class PsProcessTest(unittest.TestCase):
    """The macOS code path (no /proc): process facts from one cached `ps` snapshot."""

    def setUp(self):
        self.saved = cc.USE_PROC
        cc.USE_PROC = False
        cc._ps_cache["t"] = 0.0

    def tearDown(self):
        cc.USE_PROC = self.saved
        cc._ps_cache["t"] = 0.0

    def test_parse_ps(self):
        rows = cc.parse_ps("  101     1 Thu Nov 12 17:31:28 2026 /Users/me/.local/bin/claude --resume x\n"
                           "  202   101 Thu Nov 12 17:31:29 2026 /bin/sh -c ccdeck hook\nnoise\n")
        self.assertEqual(rows[101], (1, "Thu Nov 12 17:31:28 2026", "/Users/me/.local/bin/claude --resume x"))
        self.assertEqual(rows[202][0], 101)
        self.assertEqual(len(rows), 2)

    @unittest.skipUnless(shutil.which("ps"), "needs ps")
    def test_liveness_from_ps(self):
        start = cc.proc_start_time(os.getpid())
        self.assertTrue(start)
        self.assertTrue(cc.pid_alive(os.getpid(), start))
        self.assertFalse(cc.pid_alive(os.getpid(), "Mon Jan  1 00:00:00 2001"))
        self.assertEqual(cc.proc_ppid(os.getpid()), os.getppid())
        self.assertTrue(cc.proc_cmdline(os.getpid()))
        self.assertIsInstance(cc.running_claude_processes(), list)

    def test_ps_preserves_prompt_argument_boundaries(self):
        sessions = {"s": {"id": "s", "cwd": "/w", "last_ts": 1}}
        for args, expected in [(["claude", "explain -p usage"], {"s"}),
                               (["claude", "explain --print usage"], {"s"}),
                               (["claude", "-p", "query"], set()),
                               (["claude", "--print", "query"], set())]:
            with self.subTest(args=args), mock.patch.object(cc, "ps_table", return_value={1: (0, "", " ".join(args))}), \
                    mock.patch.object(cc, "_process_argv", return_value=args), \
                    mock.patch.object(cc.shutil, "which", return_value="/usr/bin/lsof"), \
                    mock.patch.object(cc.subprocess, "run", return_value=mock.Mock(stdout="p1\nn/w\n")):
                procs = cc._running_claude_ps()
                self.assertEqual(procs, [(1, "/w", args)])
                self.assertEqual(cc.guess_live_sessions(sessions, {}, procs, set()), expected)

    def test_an_empty_cmdline_reads_as_unreadable(self):
        with mock.patch.object(cc.sys, "platform", "linux"), mock.patch.object(cc.Path, "read_bytes", return_value=b""):
            self.assertEqual(cc._process_argv(123), [])
        with mock.patch.object(cc.sys, "platform", "linux"), mock.patch.object(cc.Path, "read_bytes", return_value=b"claude\0-c\0"):
            self.assertEqual(cc._process_argv(123), ["claude", "-c"])

    def test_unreadable_arguments_dont_hide_an_open_session(self):
        with mock.patch.object(cc, "ps_table", return_value={1: (0, "", "claude --resume s")}), \
                mock.patch.object(cc, "_process_argv", return_value=[]), \
                mock.patch.object(cc.shutil, "which", return_value="/usr/bin/lsof"), \
                mock.patch.object(cc.subprocess, "run", return_value=mock.Mock(stdout="p1\nn/w\n")):
            self.assertEqual(cc._running_claude_ps(), [(1, "/w", ["claude", "--resume", "s"])])

    def test_mac_argv_reads_nul_delimited_arguments(self):
        import ctypes
        import struct
        args = [b"claude", b"", b"explain -p usage"]
        data = struct.pack("i", len(args)) + b"/bin/claude\0\0" + b"\0".join(args) + b"\0ENV=value\0"
        def sysctl(mib, count, buf, size, new, newlen):
            self.assertEqual(list(mib), [1, 49, 123])
            ctypes.memmove(buf, data, len(data))
            size._obj.value = len(data)
            return 0
        with mock.patch.object(cc.sys, "platform", "darwin"), \
                mock.patch("ctypes.CDLL", return_value=mock.Mock(sysctl=sysctl)):
            self.assertEqual(cc._process_argv(123), [a.decode() for a in args])
        with mock.patch.object(cc.sys, "platform", "darwin"), \
                mock.patch("ctypes.CDLL", return_value=mock.Mock(sysctl=lambda *a: -1)):
            self.assertEqual(cc._process_argv(123), [])

    def test_claude_detection(self):
        self.assertTrue(cc.is_claude_cmd(["/Users/me/.local/share/claude/versions/2.1.321", "--resume"]))
        self.assertTrue(cc.is_claude_cmd(["claude"]))
        self.assertFalse(cc.is_claude_cmd(["/bin/zsh"]))


class ResumedIdsTest(unittest.TestCase):
    def test_only_the_resume_argument_counts(self):
        procs = [(1, "/w", ["claude", "--resume", "a"]), (2, "/w", ["claude", "-r", "b"]),
                 (3, "/w", ["claude", "--resume=c"]), (4, "/w", ["claude", "-p", "look at session d and --resume"]),
                 (5, "/w", ["claude", "summarize", "e"]), (6, "/w", ["claude", "--resume"]),
                 (7, "/w", ["claude", "-p", "--resume", "d"]),
                 (8, "/w", ["claude", "--print", "--resume=e"]),
                 (9, "/w", ["claude", "--", "--resume=f"]), (10, "/w", ["claude", "--", "-r", "g"]),
                 (11, "/w", ["claude", "--resume=h", "--", "--resume=i"])]
        self.assertEqual(cc.resumed_ids(procs), {"a", "b", "c", "h"})  # after -- comes the prompt

    def test_after_double_dash_comes_the_prompt(self):
        self.assertTrue(cc.is_print_argv(["claude", "-c", "-p", "q"]))
        self.assertFalse(cc.is_print_argv(["claude", "--", "-p"]))
        self.assertTrue(cc.is_print_argv(["claude", "-p", "--", "-x"]))
        self.assertEqual([p[0] for p in cc.interactive_processes([(1, "/w", ["claude", "--", "-p"])])], [1])

    def test_ps_words_dont_say_headless(self):
        with mock.patch.object(cc, "_process_argv", return_value=[]), \
                mock.patch.object(cc, "proc_cmdline", return_value=["claude", "-p", "q"]):
            self.assertFalse(cc.pid_is_headless(1))

    def test_a_timed_out_ps_is_reused_for_a_second(self):
        calls = []
        def run(*a, **kw):
            calls.append(1)
            raise cc.subprocess.TimeoutExpired("ps", 2)
        clock = iter([100.0, 102.0, 102.5])  # the call starts at 100 and times out at 102
        with mock.patch.object(cc.subprocess, "run", run), mock.patch.object(cc.time, "time", lambda: next(clock)), \
                mock.patch.dict(cc._ps_cache, {"t": 0.0, "rows": {}}):
            cc.ps_table(); cc.ps_table()
        self.assertEqual(len(calls), 1)

    def test_headless_pid(self):
        with mock.patch.object(cc, "_process_argv", lambda p: {1: ["claude", "-pc", "q"], 2: ["claude", "-c"]}.get(p, [])):
            self.assertEqual([cc.pid_is_headless(p) for p in (1, 2, None)], [True, False, False])

    def test_print_flag_in_a_cluster(self):
        for a in ("-p", "--print", "-pc", "-cp"):
            self.assertTrue(cc.is_print_flag(a), a)
        for a in ("-c", "--permission-mode", "-p=x", "-1p", "explain -p usage", "p"):
            self.assertFalse(cc.is_print_flag(a), a)
        procs = [(1, "/w", ["claude", "-pc", "query"]), (2, "/w", ["claude", "-c"])]
        self.assertEqual([p[0] for p in cc.interactive_processes(procs)], [2])


class GuessLiveTest(unittest.TestCase):
    def test_headless_processes_do_not_keep_older_sessions_alive(self):
        sessions = {k: {"id": k, "cwd": "/w/app", "last_ts": ts}
                    for k, ts in [("old", 1), ("new", 2), ("registered", 3)]}
        for flag in ("-p", "--print"):
            with self.subTest(flag=flag):
                headless = [(1, "/w/app", ["claude", flag, "summarize"])]
                self.assertEqual(cc.guess_live_sessions(sessions, {}, headless, set()), set())
                procs = headless + [(2, "/w/app", ["claude"]), (3, "/w/app", ["claude"]),
                                    (4, "/w/other", ["claude"]), (5, None, ["claude"])]
                self.assertEqual(cc.guess_live_sessions(sessions, {}, procs, set(), {3}, {"registered"}),
                                 {"new"})

    def test_only_eligible_processes_use_slots(self):
        sessions = {k: {"id": k, "cwd": "/w", "last_ts": ts}
                    for k, ts in [("old", 1), ("new", 2)]}
        me = os.getpid()
        for flag in ("-p", "--print"):
            procs = [(me, "/w", ["claude", flag, "--resume", "old"]), (2, "/w", ["claude"])]
            self.assertEqual(cc.guess_live_sessions(sessions, {}, procs, cc.resumed_ids(procs)), {"new"})
            hooks = {"old": {"pid": me, "pid_start": cc.proc_start_time(me)}}
            self.assertEqual(cc.guess_live_sessions(sessions, hooks, procs, set()), {"new"})
        procs = [(me, "/w", ["claude"])]
        hooks = {"old": {"pid": me, "pid_start": cc.proc_start_time(me), "status": "ended"}}
        self.assertEqual(cc.guess_live_sessions(sessions, hooks, procs, set()), {"new"})

    def test_n_processes_mark_n_newest_sessions_open(self):
        mk = lambda sid, ts: {"id": sid, "cwd": "/w/app", "last_ts": ts}
        sessions = {k: mk(k, ts) for k, ts in [("old", 1), ("mid", 2), ("new", 3), ("resumed", 0)]}
        procs = [(1, "/w/app", ["claude"]), (2, "/w/app", ["claude"]), (3, "/w/app", ["claude", "--resume", "resumed"])]
        live = cc.guess_live_sessions(sessions, {}, procs, {"resumed"})
        self.assertEqual(live, {"new", "mid"})  # 3 processes, one of them is the resumed session
        procs[0] = (os.getpid(), "/w/app", ["claude"])
        hooks = {"new": {"pid": os.getpid(), "pid_start": cc.proc_start_time(os.getpid())}}
        self.assertEqual(cc.guess_live_sessions(sessions, hooks, procs, {"resumed"}), {"mid"})

    def test_a_switched_process_takes_one_slot(self):
        # pid P ran A (its hook still names P), then /resume switched it to B (the registry names P);
        # hookless C runs in a second process in the same folder
        me, start = os.getpid(), cc.proc_start_time(os.getpid())
        sessions = {k: {"id": k, "cwd": "/w/app", "last_ts": ts} for k, ts in [("A", 1), ("B", 2), ("C", 3)]}
        hooks = {"A": {"pid": me, "pid_start": start}}
        procs = [(me, "/w/app", ["claude"]), (2, "/w/app", ["claude"])]
        self.assertEqual(cc.guess_live_sessions(sessions, hooks, procs, set(), {me}, {"B"}), {"C"})

    def test_headless_runs_arent_sessions(self):
        """`claude -p` and SDK runs stay off the dashboard; one resumed in a terminal comes back."""
        build_fixtures()
        hl = lambda e, ep: dict(e, entrypoint=ep)
        write_session("s-headless", [hl(user("/qta", 30), "sdk-cli"), hl(asst([text("Do you want me to run it?")], 29), "sdk-cli")])
        write_session("s-sdk", [hl(user("summarize", 30), "sdk-ts"), hl(asst([text("Done.")], 29), "sdk-ts")])
        write_session("s-resumed", [hl(user("try it", 30), "sdk-cli"), hl(asst([text("Ok.")], 29), "sdk-cli"),
                                    hl(user("now interactively", 10), "cli"), hl(asst([text("Sure.")], 9), "cli")])
        write_session("s-desktop", [hl(user("hi", 30), "claude-desktop"), hl(asst([text("Hello.")], 29), "claude-desktop")])
        ids = set(by_id(cc.collect(True, 10 ** 6)))
        self.assertFalse({"s-headless", "s-sdk"} & ids)
        self.assertTrue({"s-resumed", "s-desktop", "s-closed"} <= ids)

    def test_a_print_run_doesnt_hide_a_session_open_in_a_terminal(self):
        """`claude -p --resume` writes "sdk" entries to a session still open in a terminal: it stays."""
        build_fixtures()
        hl = lambda e, ep: dict(e, entrypoint=ep)
        for sid in ("s-term", "s-printing"):
            write_session(sid, [hl(user("hi", 30), "cli"), hl(user("run it", 5), "sdk-cli"), hl(asst([text("Done.")], 4), "sdk-cli")])
        peers = {"s-term": {"name": None, "pid": 1, "pids": [1]}, "s-printing": {"name": None, "pid": 2, "pids": [2]}}
        with mock.patch.object(cc, "peer_sessions", lambda: peers), \
             mock.patch.object(cc, "pid_is_headless", lambda p: p == 2):
            ids = set(by_id(cc.collect(True, 10 ** 6)))
        self.assertIn("s-term", ids)  # an interactive process runs it
        self.assertNotIn("s-printing", ids)  # only the print run itself has it open
        me = os.getpid()
        hooks = {"s-h": {"pid": me, "pid_start": cc.proc_start_time(me)}, "s-ended": {"pid": me, "status": "ended"}}
        with mock.patch.object(cc, "pid_is_headless", lambda p: False):
            self.assertTrue(cc.has_live_owner("s-h", hooks, {}, {}, set()))
            self.assertTrue(cc.has_live_owner("s-h", hooks, {}, {me: "s-h"}, set()))
            self.assertFalse(cc.has_live_owner("s-h", hooks, {}, {me: "s-b"}, set()))  # /resume moved it to s-b
            self.assertFalse(cc.has_live_owner("s-ended", hooks, {}, {}, set()))
            self.assertFalse(cc.has_live_owner("s-none", hooks, {}, {}, set()))
            self.assertTrue(cc.has_live_owner("s-none", hooks, {}, {}, {"s-none"}))  # an interactive --resume
        with mock.patch.object(cc, "pid_is_headless", lambda p: p == me):
            self.assertFalse(cc.has_live_owner("s-h", hooks, {}, {}, set()))  # an older release's hook from a -p run

    def test_duplicate_sessions_filter_headless_after_selecting_latest(self):
        for older_ep, newer_ep, has_prompt in (("cli", "sdk-cli", True),
                                               ("sdk-cli", "cli", True),
                                               ("cli", "sdk-cli", False)):
            for newest_first in (False, True):
                with self.subTest(older=older_ep, newer=newer_ep, prompt=has_prompt,
                                  newest_first=newest_first), tempfile.TemporaryDirectory() as tmp:
                    projects = Path(tmp) / "projects"
                    paths = []
                    for folder, ep, minutes in (("old", older_ep, 30), ("new", newer_ep, 10)):
                        path = projects / folder / "duplicate.jsonl"
                        path.parent.mkdir(parents=True)
                        entry = user("hello", minutes) if folder == "old" or has_prompt else asst([text("Done.")], minutes)
                        path.write_text(json.dumps(dict(entry, entrypoint=ep)) + "\n")
                        paths.append(path)
                    if newest_first:
                        paths.reverse()
                    with mock.patch.object(cc, "PROJECTS_DIR", projects), \
                         mock.patch.object(cc, "CACHE_FILE", Path(tmp) / "cache.json"), \
                         mock.patch.object(Path, "glob", lambda _, pattern: iter(paths) if pattern == "*/*.jsonl" else iter(())):
                        for _ in range(2):  # parse first, then reuse cached transcripts
                            sessions = cc.scan_transcripts()  # the latest copy wins; collect drops headless ones
                            selected = sessions["duplicate"]
                            self.assertEqual(selected["entrypoint"], newer_ep if has_prompt else older_ep)
                            self.assertEqual(selected["last_ts"], cc.parse_transcript(projects / ("new" if has_prompt else "old") / "duplicate.jsonl")["last_ts"])

    def test_registry_sessions_use_up_their_process(self):
        # registry session A is open in /w/app; B is newer and closed: one process, so only A
        sessions = {"A": {"id": "A", "cwd": "/w/app", "last_ts": 1}, "B": {"id": "B", "cwd": "/w/app", "last_ts": 2}}
        self.assertEqual(cc.guess_live_sessions(sessions, {}, [(1, "/w/app", ["claude"])], {"A"}), set())


class MacDesktopTest(unittest.TestCase):
    def test_terminal_and_iterm_scripts(self):
        t = cc.mac_terminal_command("/Users/me/my \"dir\"", ["claude", "--resume", "abc"], "terminal")
        self.assertEqual(t[0], "osascript")
        self.assertIn('do script "cd \'/Users/me/my \\"dir\\"\' && claude --resume abc"', t[2])
        i = cc.mac_terminal_command("/tmp", ["claude"], "iterm")
        self.assertIn('tell application "iTerm"', i)
        self.assertTrue(any("write text" in x for x in i))

    def test_resume_opens_the_terminal_you_use(self):
        """Both installed: the app most open sessions run in, else the one the dashboard started from."""
        procs = {  # pid: (ppid, command)
            10: (9, "claude"), 9: (8, "-zsh"), 8: (7, "login -pfl me"), 7: (1, "/System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal"),
            20: (19, "claude"), 19: (18, "-zsh"), 18: (1, "/Applications/iTerm.app/Contents/MacOS/iTerm2 --server login"),
            30: (1, "claude"),  # no terminal above it (an IDE, ssh, …)
            40: (8, "claude look at /Applications/iTerm.app/Contents/Info.plist"),  # in Terminal; its prompt names iTerm
            50: (51, "claude"), 51: (1, "/Users/me/Local Apps/iTerm.app/Contents/MacOS/iTerm2"),  # a space in the path
        }
        exact = {51: ["/Users/me/Local Apps/iTerm.app/Contents/MacOS/iTerm2"]}  # what the kernel says; ps splits it
        def app_for(peers, term_program=""):
            def peer(p):  # a pid, or a list: one session in several processes
                ps = p if isinstance(p, list) else [p]
                return {"pid": ps[0], "pids": ps}
            with mock.patch.object(cc, "peer_sessions", lambda: {f"s{i}": peer(p) for i, p in enumerate(peers)}), \
                    mock.patch.object(cc, "proc_ppid", lambda p: procs.get(p, (None,))[0]), \
                    mock.patch.object(cc, "proc_cmdline", lambda p: procs[p][1].split() if p in procs else []), \
                    mock.patch.object(cc, "_process_argv", lambda p: exact.get(p, [])), \
                    mock.patch.object(cc.Path, "exists", lambda self: "iTerm.app" in str(self)), \
                    mock.patch.dict(os.environ, {"TERM_PROGRAM": term_program}):
                cmd = cc.mac_terminal_command("/tmp", ["claude"])
            return "iterm" if 'tell application "iTerm"' in cmd else "terminal"
        self.assertEqual(app_for([10]), "terminal")             # iTerm installed, but you work in Terminal
        self.assertEqual(app_for([20, 10, 20]), "iterm")        # most sessions
        self.assertEqual(app_for([10, 20]), "terminal")         # a tie: Terminal
        self.assertEqual(app_for([40]), "terminal")             # only the program counts, not a path in a prompt
        self.assertEqual(app_for([50], "Apple_Terminal"), "iterm")  # iTerm in a folder with a space
        self.assertEqual(app_for([[30, 20]], "Apple_Terminal"), "iterm")  # open in an IDE and in iTerm
        self.assertEqual(app_for([[20, 50], 10]), "terminal")    # one vote per session (a tie), not per process
        self.assertEqual(app_for([30], "Apple_Terminal"), "terminal")  # no session says: where the dashboard started
        self.assertEqual(app_for([], "iTerm.app"), "iterm")
        self.assertEqual(app_for([]), "iterm")                  # nothing says: iTerm when it's installed, as before
        with mock.patch.object(cc.Path, "exists", lambda self: False):
            self.assertIn('tell application "Terminal" to activate', cc.mac_terminal_command("/tmp", ["claude"]))

    def test_focus_commands(self):
        gnome = {"GNOME_TERMINAL_SCREEN": "/org/gnome/Terminal/screen/ecdaf156_8be8_4402_9d37_193ec38ead77"}
        saved = cc.shutil.which
        cc.shutil.which = lambda b: "/usr/bin/" + b
        try:
            (label, [cmd]), = cc.focus_commands(gnome, None)
            self.assertEqual(label, "gnome-terminal")
            self.assertIn("ecdaf156-8be8-4402-9d37-193ec38ead77", cmd)
            self.assertEqual(cc.focus_commands({"GNOME_TERMINAL_SCREEN": "/org/gnome/Terminal/screen/x; rm -rf"}, None), [])
            self.assertEqual(cc.focus_commands({}, None), [])
            out = cc.focus_commands(dict(gnome, TMUX="/tmp/tmux-1000/default,123,0", TMUX_PANE="%7"), None)
        finally:
            cc.shutil.which = saved
        self.assertEqual([l for l, _ in out], ["tmux"])  # a pane's GNOME_TERMINAL_SCREEN is the tmux server's
        cc.shutil.which = lambda b: None if b == "tmux" else "/usr/bin/" + b
        try:  # no tmux to reach the pane: don't fall back to the tab the tmux server started in
            self.assertEqual(cc.focus_commands(dict(gnome, TMUX_PANE="%7"), None), [])
        finally:
            cc.shutil.which = saved
        self.assertEqual(out[0][1][1], ["tmux", "-S", "/tmp/tmux-1000/default", "select-window", "-t", "%7"])

    def test_tmux_pane_from_tty_without_proc_and_detached_sessions(self):
        fake = FAKE_GH.parent / "tmux"
        log = TMP / "tmux.log"
        saved = cc.USE_PROC, cc.IS_MAC, cc.proc_tty
        cc.USE_PROC, cc.IS_MAC, cc.proc_tty = False, False, lambda pid: "/dev/ttys004"
        try:
            for attached, expected in (("1", "tmux"), ("0", None)):
                fake.write_text(f"""#!/bin/sh
echo "$@" >> {log}
case "$1" in
  list-panes) printf '/dev/ttys003 %%5\n/dev/ttys004 %%6\n' ;;
  display-message) echo {attached} ;;
esac
""")
                fake.chmod(0o755)
                log.unlink(missing_ok=True)
                self.assertEqual(cc.focus_terminal(123), expected)
                self.assertIn("display-message -p -t %6", log.read_text())  # the pane that owns the tty
        finally:
            cc.USE_PROC, cc.IS_MAC, cc.proc_tty = saved
            fake.unlink(missing_ok=True)

    def test_applescript_escaping(self):
        self.assertEqual(cc.applescript_str('a "b" \\ c\nd'), '"a \\"b\\" \\\\ c d"')


class TerminalTest(unittest.TestCase):
    def test_command_is_quoted(self):
        os.environ["CCDECK_TERMINAL"] = "gh --"  # any binary on PATH works for building the command
        try:
            cmd = cc.terminal_command("/tmp/it's here", ["claude", "--resume", "abc"])
        finally:
            del os.environ["CCDECK_TERMINAL"]
        self.assertEqual(cmd[:4], ["gh", "--", "bash", "-lc"])
        self.assertIn("cd '/tmp/it'\"'\"'s here' && claude --resume abc", cmd[4])


class ChecksTest(unittest.TestCase):
    """CI state from every check on the head commit, less the runs a newer one replaced."""

    @staticmethod
    def ch(name, concl, started, wf="CI", status="COMPLETED"):
        return {"__typename": "CheckRun", "workflowName": wf, "name": name, "status": status, "conclusion": concl, "startedAt": started}

    def test_replaced_runs_dont_count(self):
        c = self.ch
        cancelled_run = [c("lint", "CANCELLED", "10:00:00"), c("test (${{ matrix.shard }})", "CANCELLED", "10:00:00"),
                         c("lint", "SUCCESS", "10:00:03")]
        self.assertEqual(cc.summarize_checks(cancelled_run), "passing")
        rerun = [c("test", "FAILURE", "10:00:00"), c("test", "SUCCESS", "10:05:00")]
        self.assertEqual(cc.summarize_checks(rerun), "passing")
        self.assertEqual(cc.summarize_checks(rerun[::-1][:1] + [c("test", "FAILURE", "10:09:00")]), "failing")  # re-run red
        self.assertEqual(cc.summarize_checks(rerun[:1] + [c("test", "", "10:05:00", status="IN_PROGRESS")]), "pending")

    def test_real_failures_still_count(self):
        c = self.ch
        self.assertEqual(cc.summarize_checks([c("deploy", "CANCELLED", "10:00:00")]), "failing")  # nothing newer replaced it
        self.assertEqual(cc.summarize_checks([c("lint", "SUCCESS", "10:00:00"), c("review", "CANCELLED", "10:00:00", wf="Bot"),
                                              c("lint", "SUCCESS", "10:09:00")]), "failing")  # another workflow's run is no newer run of Bot
        fail_fast = [c("unit", "FAILURE", "10:00:00"), c("e2e", "CANCELLED", "10:00:00"), c("report", "SKIPPED", "10:02:00")]
        self.assertEqual(cc.summarize_checks(fail_fast), "failing")
        self.assertEqual(cc.summarize_checks([{"__typename": "StatusContext", "context": "ci/legacy", "state": "ERROR"}]), "failing")


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()


class DemoDataTest(unittest.TestCase):
    """scripts/demo_data.py builds a HOME that ccdeck reads as the intended mix of states."""

    def test_demo_home(self):
        import subprocess
        home = TMP / "demo-home"
        shutil.rmtree(home, ignore_errors=True)
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "demo_data.py"), str(home), "--pid", str(os.getpid())],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        env = dict(os.environ, HOME=str(home), PATH=f"{home / 'bin'}:{os.environ['PATH']}", NO_COLOR="1")
        out = subprocess.run([sys.executable, str(ROOT / "ccdeck"), "-a"], capture_output=True, text=True, env=env, timeout=30).stdout
        for label in ("Permission needed", "Question for you", "Decision needed", "Error", "Working", "Parked"):
            self.assertIn(label, out)
        gh = subprocess.run([str(home / "bin" / "gh"), "pr", "view", "https://github.com/acme/dashboard/pull/212", "--json", "x"],
                            capture_output=True, text=True)
        self.assertEqual(json.loads(gh.stdout)["number"], 212)


class PreviewTest(unittest.TestCase):
    """`ccdeck preview` runs another ccdeck on the same sessions, with its own caches."""

    def test_resume_from_a_preview_gets_your_environment(self):
        """The preview's own config folder and CCDECK_NOTIFY=0 must not reach a `claude --resume` it starts."""
        saved = json.dumps({"CLAUDE_CONFIG_DIR": None, "CCDECK_PREVIEW": None, "CCDECK_NOTIFY": "1", "CCDECK_AUTOSTART": None})
        env = cc.user_env({"CLAUDE_CONFIG_DIR": "/p/claude", "CCDECK_PREVIEW": "#35", "CCDECK_NOTIFY": "0",
                           "CCDECK_AUTOSTART": "0", "CCDECK_PREVIEW_SAVED_ENV": saved, "PATH": "/bin"})
        self.assertEqual(env, {"CCDECK_NOTIFY": "1", "PATH": "/bin"})
        env = cc.user_env({"CLAUDE_CONFIG_DIR": "/p/claude", "CCDECK_PREVIEW_SAVED_ENV": json.dumps({"CLAUDE_CONFIG_DIR": "/me/.claude"})})
        self.assertEqual(env, {"CLAUDE_CONFIG_DIR": "/me/.claude"})
        self.assertEqual(cc.user_env({"CLAUDE_CONFIG_DIR": "/mine"}), {"CLAUDE_CONFIG_DIR": "/mine"})  # not a preview: as is

    def test_preview_refuses_a_port_another_program_has(self):
        import socket
        other = socket.socket()
        other.bind(("127.0.0.1", 0))
        other.listen()
        self.addCleanup(other.close)
        args = mock.Mock(target="34", repo="o/r", trust=False, port=other.getsockname()[1], no_open=True)
        started = []
        with mock.patch.object(cc, "resolve_preview", lambda *a: (TMP, "#34 x")), \
                mock.patch.object(cc, "stop_preview", lambda quiet=False: True), \
                mock.patch.object(cc.subprocess, "Popen", lambda *a, **k: started.append(a)), \
                mock.patch("builtins.print"):
            with self.assertRaises(SystemExit) as e:
                cc.cmd_preview(args)
        self.assertIn("in use", str(e.exception.code))
        self.assertEqual(started, [])

    def test_config_dir_links_resolve_with_a_relative_config_dir(self):
        """CLAUDE_CONFIG_DIR=.claude: the links must lead to the real folders, not to paths inside the preview."""
        cwd = os.getcwd()
        os.chdir(TMP)
        self.addCleanup(os.chdir, cwd)
        rel = Path("rel-claude") / "projects"
        with mock.patch.object(cc, "PROJECTS_DIR", rel):
            cfg = cc.preview_config_dir()
        self.assertEqual((cfg / "projects").resolve(), (TMP / rel).resolve())

    def test_stop_counts_an_unreaped_preview_as_stopped(self):
        """A preview whose parent never reaps it stays a zombie: its start time is still there, but it has
        stopped, and stop must say so rather than time out (and block every later start)."""
        import subprocess
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])  # never waited for: a zombie
        self.addCleanup(lambda: (proc.kill(), proc.wait()))
        cc._ps_cache["t"] = 0
        cc.PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
        cc.save_json(cc.PREVIEW_DIR / "preview.json", {"pid": proc.pid, "pid_start": cc.proc_start_time(proc.pid), "label": "x"})
        t = time.monotonic()
        with mock.patch("builtins.print"):
            self.assertTrue(cc.stop_preview())
        self.assertLess(time.monotonic() - t, 5)
        self.assertTrue(cc.proc_zombie(proc.pid))  # still unreaped: it was the state that counted

    def test_stop_never_signals_a_pid_it_cant_identify(self):
        cc.PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
        cc.save_json(cc.PREVIEW_DIR / "preview.json", {"pid": os.getpid(), "pid_start": None, "label": "x"})
        with mock.patch.object(cc.os, "kill") as kill, mock.patch("builtins.print"):
            self.assertFalse(cc.stop_preview())
        kill.assert_not_called()

    def test_preview_commands_run_one_at_a_time(self):
        active, overlap = [], []
        def start(args):
            active.append(1)
            if len(active) > 1:
                overlap.append(1)
            time.sleep(0.2)
            active.pop()
        with mock.patch.object(cc, "start_preview", start):
            threads = [threading.Thread(target=cc.cmd_preview, args=(mock.Mock(target="34"),)) for _ in range(3)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(overlap, [])

    def test_config_dir_drops_a_stale_copy(self):
        """No config.json of yours: the preview starts without one, not with the last preview's changes."""
        cc.CONFIG_FILE.unlink(missing_ok=True)
        stale = cc.PREVIEW_DIR / "claude" / "ccdeck" / "config.json"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text('{"review_requests": "off"}')
        cc.preview_config_dir()
        self.assertFalse(stale.exists())

    def test_stop_waits_until_the_preview_is_gone(self):
        import subprocess
        # detached like a real preview (its parent exits), so the system reaps it rather than leaving a zombie
        pid = int(subprocess.run(["sh", "-c", "sleep 30 >/dev/null 2>&1 & echo $!"], capture_output=True, text=True).stdout)
        self.addCleanup(lambda: os.kill(pid, 9) if cc.pid_alive(pid, None) else None)
        cc.PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
        cc._ps_cache["t"] = 0
        cc.save_json(cc.PREVIEW_DIR / "preview.json", {"pid": pid, "pid_start": cc.proc_start_time(pid), "label": "x"})
        t = time.monotonic()
        with mock.patch("builtins.print"):
            self.assertTrue(cc.stop_preview())
        cc._ps_cache["t"] = 0
        # gone by the time it returns (a zombie has exited too: only its parent hasn't collected it)
        self.assertTrue(not cc.pid_alive(pid, None) or cc.proc_zombie(pid))
        self.assertLess(time.monotonic() - t, 5)  # SIGTERM was enough
        self.assertFalse((cc.PREVIEW_DIR / "preview.json").exists())

    def test_version_target_finds_main_or_the_open_pr(self):
        def gh(path):
            if path.startswith("repos/o/r/commits/"):
                raise RuntimeError("no such tag")
            if path.startswith("repos/o/r/pulls?"):
                return [{"number": 7, "head": {"sha": "aaa", "repo": {"full_name": "o/r"}}},
                        {"number": 8, "head": {"sha": "bbb", "repo": {"full_name": "fork/r"}}}]
            import base64
            ver = {"main": "0.8.1", "aaa": "0.8.5", "bbb": "0.8.6"}[path.rsplit("ref=", 1)[1]]
            return {"content": base64.b64encode(json.dumps({"version": ver}).encode()).decode()}
        with mock.patch.object(cc, "github_json", gh):
            self.assertEqual(cc.find_version("0.8.1", "o/r"), "main")
            self.assertEqual(cc.find_version("0.8.5", "o/r"), "#7")
            self.assertIsNone(cc.find_version("0.8.6", "o/r"))  # only in a fork's PR: not looked at
            self.assertIsNone(cc.find_version("9.9.9", "o/r"))

    def test_config_dir_shares_sessions_and_copies_marks_and_settings(self):
        cc.save_json(cc.CONFIG_FILE, {"review_requests": "mine"})
        self.addCleanup(lambda: cc.CONFIG_FILE.unlink(missing_ok=True))
        cfg = cc.preview_config_dir()
        self.assertEqual((cfg / "projects").resolve(), cc.PROJECTS_DIR.resolve())
        self.assertEqual((cfg / "ccdeck" / "state").resolve(), cc.STATE_DIR.resolve())
        self.assertEqual((cfg / "sessions").resolve(), (cc.CLAUDE_DIR / "sessions").resolve())  # which sessions are open
        copied = cfg / "ccdeck" / "config.json"
        self.assertFalse(copied.is_symlink())
        self.assertEqual(cc.load_json(copied, {}), {"review_requests": "mine"})
        cc.save_json(copied, {"review_requests": "off"})  # changed in the preview
        self.assertEqual(cc.load_json(cc.CONFIG_FILE, {}), {"review_requests": "mine"})

    def test_unpack_refuses_escaping_paths(self):
        import io
        import tarfile

        def tarball(names):
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:gz") as tar:
                for n in names:
                    info = tarfile.TarInfo(n)
                    info.size = 2
                    tar.addfile(info, io.BytesIO(b"ok"))
            return buf.getvalue()

        dest = TMP / "unpack-ok"
        cc.unpack_tarball(tarball(["repo-abc/ccdeck", "repo-abc/web/index.html"]), dest)
        self.assertEqual((dest / "web" / "index.html").read_text(), "ok")
        with self.assertRaises(RuntimeError):
            cc.unpack_tarball(tarball(["repo-abc/../../evil"]), TMP / "unpack-bad")
        self.assertFalse((TMP / "evil").exists())

    def test_fork_pr_needs_trust(self):
        saved = cc.github_json
        cc.github_json = lambda path: {"number": 9, "title": "x", "head": {"sha": "f" * 40, "repo": {"full_name": "someone/fork"}},
                                       "base": {"repo": {"full_name": "slonik923/ccdeck"}}}
        try:
            with self.assertRaises(SystemExit) as e:
                cc.resolve_preview("9", "slonik923/ccdeck", trust=False)
            self.assertIn("--trust", str(e.exception))
        finally:
            cc.github_json = saved

    def test_preview_of_local_checkout(self):
        import socket
        import subprocess
        build_fixtures()
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        env = dict(os.environ, CCDECK_AUTOSTART="0")
        r = subprocess.run([sys.executable, str(ROOT / "ccdeck"), "preview", str(ROOT), "--port", str(port), "--no-open"],
                           capture_output=True, text=True, env=env, timeout=60)
        log = lambda: (cc.PREVIEW_DIR / "serve.log").read_text() if (cc.PREVIEW_DIR / "serve.log").exists() else ""  # noqa: E731
        try:
            self.assertEqual(r.returncode, 0, r.stderr + r.stdout + log())
            self.assertIn(f"http://localhost:{port}", r.stdout)
            cfg = cc.PREVIEW_DIR / "claude"
            for _ in range(100):  # "(still starting)" is allowed; wait for it here
                if (cfg / "ccdeck" / "token").exists():
                    try:
                        urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1).close()
                        break
                    except OSError:
                        pass
                time.sleep(0.3)
            token = (cfg / "ccdeck" / "token").read_text().strip()
            req = urllib.request.Request(f"http://127.0.0.1:{port}/api/sessions", headers={"X-CC-Token": token, "Host": f"localhost:{port}"})
            snap = json.load(urllib.request.urlopen(req, timeout=5))
            self.assertTrue(snap["preview"].startswith("local "))
            self.assertIn("s-decision", {s["id"] for s in snap["sessions"]})  # your real transcripts
            self.assertNotEqual(token, cc.TOKEN_FILE.read_text().strip() if cc.TOKEN_FILE.exists() else "")
            self.assertFalse((cc.DATA_DIR / "daemon.json").exists() and
                             json.loads((cc.DATA_DIR / "daemon.json").read_text()).get("port") == port)
        finally:
            subprocess.run([sys.executable, str(ROOT / "ccdeck"), "preview", "stop"], capture_output=True, env=env, timeout=30)
        self.assertFalse((cc.PREVIEW_DIR / "preview.json").exists())

    def test_pr_preview_downloads_once(self):
        import subprocess
        tar = subprocess.run(["git", "-C", str(ROOT), "archive", "--format=tar.gz", "--prefix=ccdeck-abc/", "HEAD"],
                             capture_output=True).stdout
        if not tar:
            self.skipTest("not a git checkout")
        saved = (cc.github_json, cc.download_source)
        calls = []
        cc.github_json = lambda path: {"number": 16, "title": "Group sessions", "head": {"sha": "a" * 40, "repo": {"full_name": "slonik923/ccdeck"}},
                                       "base": {"repo": {"full_name": "slonik923/ccdeck"}}}
        cc.download_source = lambda repo, sha, dest: (calls.append(sha), cc.unpack_tarball(tar, dest))
        try:
            src, label = cc.resolve_preview("16", "slonik923/cc-dashboard", trust=False)  # old name: same repo
            self.assertTrue((src / "ccdeck").is_file())
            self.assertEqual(label, "#16 Group sessions (aaaaaaa)")
            cc.resolve_preview("#16", "slonik923/ccdeck", trust=False)
            self.assertEqual(len(calls), 1)  # cached by commit
        finally:
            cc.github_json, cc.download_source = saved
