"""Tests for ccdeck. Run: python3 -m unittest discover -s tests -v

Each test class gets a throwaway HOME with synthetic Claude Code transcripts and a
fake `gh` on PATH, so nothing touches your real ~/.claude.
"""
import importlib.machinery
import importlib.util
import json
import os
import shlex
import shutil
import stat
import sys
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

    def test_claude_detection(self):
        self.assertTrue(cc.is_claude_cmd(["/Users/me/.local/share/claude/versions/2.1.321", "--resume"]))
        self.assertTrue(cc.is_claude_cmd(["claude"]))
        self.assertFalse(cc.is_claude_cmd(["/bin/zsh"]))


class ResumedIdsTest(unittest.TestCase):
    def test_only_the_resume_argument_counts(self):
        procs = [(1, "/w", ["claude", "--resume", "a"]), (2, "/w", ["claude", "-r", "b"]),
                 (3, "/w", ["claude", "--resume=c"]), (4, "/w", ["claude", "-p", "look at session d and --resume"]),
                 (5, "/w", ["claude", "summarize", "e"]), (6, "/w", ["claude", "--resume"])]
        self.assertEqual(cc.resumed_ids(procs), {"a", "b", "c"})


class GuessLiveTest(unittest.TestCase):
    def test_n_processes_mark_n_newest_sessions_open(self):
        mk = lambda sid, ts: {"id": sid, "cwd": "/w/app", "last_ts": ts}
        sessions = {k: mk(k, ts) for k, ts in [("old", 1), ("mid", 2), ("new", 3), ("resumed", 0)]}
        procs = [(1, "/w/app", ["claude"]), (2, "/w/app", ["claude"]), (3, "/w/app", ["claude", "--resume", "resumed"])]
        live = cc.guess_live_sessions(sessions, {}, procs, {"resumed"})
        self.assertEqual(live, {"new", "mid"})  # 3 processes, one of them is the resumed session
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
                            sessions = cc.scan_transcripts()
                            if has_prompt and newer_ep == "sdk-cli":
                                self.assertNotIn("duplicate", sessions)
                            else:
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
        }
        def app_for(peers, term_program=""):
            with mock.patch.object(cc, "peer_sessions", lambda: {f"s{i}": {"pid": p} for i, p in enumerate(peers)}), \
                    mock.patch.object(cc, "proc_ppid", lambda p: procs.get(p, (None,))[0]), \
                    mock.patch.object(cc, "proc_cmdline", lambda p: procs[p][1].split() if p in procs else []), \
                    mock.patch.object(cc.Path, "exists", lambda self: "iTerm.app" in str(self)), \
                    mock.patch.dict(os.environ, {"TERM_PROGRAM": term_program}):
                cmd = cc.mac_terminal_command("/tmp", ["claude"])
            return "iterm" if 'tell application "iTerm"' in cmd else "terminal"
        self.assertEqual(app_for([10]), "terminal")             # iTerm installed, but you work in Terminal
        self.assertEqual(app_for([20, 10, 20]), "iterm")        # most sessions
        self.assertEqual(app_for([10, 20]), "terminal")         # a tie: Terminal
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
