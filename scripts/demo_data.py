#!/usr/bin/env python3
"""Write a demo HOME full of made-up Claude Code sessions, for screenshots and manual checks.

    python3 scripts/demo_data.py /tmp/ccdeck-demo [--pid PID]

Then run any version of ccdeck against it:

    HOME=/tmp/ccdeck-demo PATH=/tmp/ccdeck-demo/bin:$PATH python3 ccdeck serve --port 7800

Nothing here is real: names, repos, tickets and PRs are invented, so screenshots are safe to
publish. `--pid` is a process that stays alive while you take screenshots (the sessions with hook
state count as running while it lives). A fake `gh` in <home>/bin answers PR and review queries.
"""
import argparse
import json
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = "https://github.com/acme/{}/pull/{}"
WORK = "/home/demo/code/"


def ts(minutes_ago):
    t = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return t.isoformat().replace("+00:00", "Z")


def text(t):
    return {"type": "text", "text": t}


def tool(tid, name, inp=None):
    return {"type": "tool_use", "id": tid, "name": name, "input": inp or {}}


class Session:
    def __init__(self, home, sid, title, repo, branch, prompt, m):
        self.sid, self.cwd, self.branch, self.n = sid, WORK + repo, branch, 0
        self.path = home / ".claude" / "projects" / ("-home-demo-code-" + repo) / f"{sid}.jsonl"
        self.entries = []
        self.user(prompt, m)
        self.entries.append({"type": "ai-title", "aiTitle": title})

    def _base(self, kind, m):
        return {"type": kind, "cwd": self.cwd, "gitBranch": self.branch, "timestamp": ts(m), "sessionId": self.sid}

    def user(self, content, m):
        e = self._base("user", m)
        e["message"] = {"role": "user", "content": content}
        self.entries.append(e)
        return self

    def say(self, blocks, m, ctx=150000, out=8000, **extra):
        self.n += 1
        e = self._base("assistant", m)
        e["message"] = {"id": f"{self.sid}-m{self.n}", "role": "assistant", "model": "claude-opus-5-5", "content": blocks,
                        "usage": {"input_tokens": 3, "cache_creation_input_tokens": ctx // 10,
                                  "cache_read_input_tokens": ctx - ctx // 10, "output_tokens": out}}
        e.update(extra)
        self.entries.append(e)
        return self

    def result(self, tid, content, m):
        return self.user([{"type": "tool_result", "tool_use_id": tid, "content": content}], m)

    def created_pr(self, repo, n, m):
        tid = f"pr{n}"
        self.say([tool(tid, "mcp__github__create_pull_request", {"title": "PR"})], m)
        return self.result(tid, json.dumps({"url": REPO.format(repo, n)}), m)

    def write(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w") as f:
            for e in self.entries:
                f.write(json.dumps(e) + "\n")


FAKE_GH = r'''#!/usr/bin/env python3
import json, sys
PRS = __PRS__
if sys.argv[1:3] == ["api", "graphql"]:  # the PR view's query: your open PRs
    print(json.dumps({"data": {"viewer": {"login": "you"}, "mine": {"nodes": __PLAN__}}})); sys.exit(0)
if sys.argv[1:3] == ["search", "prs"]:
    print(json.dumps(__REVIEWS__)); sys.exit(0)
if sys.argv[1:3] == ["pr", "view"]:
    url = sys.argv[3]
    d = PRS.get(url)
    if not d:
        print("not found", file=sys.stderr); sys.exit(1)
    print(json.dumps(d)); sys.exit(0)
if sys.argv[1:3] == ["api", "user"]:  # who is signed in: review requests are paired with that account's PRs
    print("you"); sys.exit(0)
if sys.argv[1:3] == ["auth", "status"]:
    sys.exit(0)
sys.exit(1)
'''


def pr(n, title, state="OPEN", draft=False, review="", checks="SUCCESS", mergeable="MERGEABLE", head="", base="main", failing=()):
    """One PR as `gh pr view` returns it. failing: names of failed checks (the rest pass)."""
    roll = [] if checks is None else [{"status": "IN_PROGRESS" if checks == "PENDING" else "COMPLETED",
                                       "conclusion": "" if checks == "PENDING" else checks}]
    if failing:
        roll = [{"name": c, "workflowName": "CI", "status": "COMPLETED", "conclusion": "FAILURE", "startedAt": ts(30)} for c in failing] + \
               [{"name": "build", "workflowName": "CI", "status": "COMPLETED", "conclusion": "SUCCESS", "startedAt": ts(30)}]
    return {"number": n, "title": title, "state": state, "isDraft": draft, "reviewDecision": review,
            "mergeable": mergeable, "statusCheckRollup": roll, "headRefName": head, "baseRefName": base}


def gql(repo, n, title, head, base="main", draft=False, merge="CLEAN", review=None, checks="SUCCESS", said=(), mins=60,
        queue=0, auto=False, asked=0):
    """One PR as the PR view's GraphQL query returns it. said: who wrote last on each open review thread.
    queue: its place in the merge queue (0: not queued). auto: auto-merge on. asked: reviewers asked."""
    return {"url": REPO.format(repo, n), "number": n, "title": title, "isDraft": draft, "updatedAt": ts(mins),
            "isInMergeQueue": bool(queue), "mergeQueueEntry": {"position": queue} if queue else None,
            "autoMergeRequest": {"enabledAt": ts(mins)} if auto else None, "reviewRequests": {"totalCount": asked},
            "headRefName": head, "baseRefName": base, "mergeStateStatus": merge, "reviewDecision": review,
            "repository": {"nameWithOwner": "acme/" + repo, "defaultBranchRef": {"name": "main"}}, "author": {"login": "you"},
            "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": checks} if checks else None}}]},
            "reviewThreads": {"pageInfo": {"hasNextPage": False},
                              "nodes": [{"isResolved": False, "comments": {"nodes": [{"author": {"login": a}}]}} for a in said]}}


def build(home, pid):
    home = Path(home)
    S = lambda *a: Session(home, *a)  # noqa: E731
    sessions = [
        S("d-deploy", "Ship Stripe webhook handler to staging", "billing-api", "feat/stripe-webhooks", "deploy it to staging", 3)
        .say([text("The webhook handler is finished and all 212 unit tests pass. To check signature handling against real "
                   "Stripe events, I want to deploy to staging and replay the last 50 events."),
              tool("b1", "Bash", {"command": "npm run deploy -- --env=staging"})], 2, ctx=391000, out=48200),
        S("d-auth", "Migrate auth to Lucia v4", "web-app", "chore/lucia-v4", "migrate auth", 6)
        .say([tool("q1", "AskUserQuestion", {"questions": [{"question": "Should sessions created before the migration be "
                                                                         "invalidated, or should I write a shim that upgrades them?"}]})], 5),
        S("d-queue", "Pick a queue for transactional email", "notifications", "dana/eng-57-email-queue",
          "Pick a queue for https://linear.app/acme/issue/ENG-57/transactional-email-queue, spec in "
          "https://www.notion.so/acme/Email-queue-spec-" + "c" * 32, 12)
        .say([tool("l1", "mcp__linear__get_issue", {"id": "ENG-31"})], 12)
        .result("l1", json.dumps({"identifier": "ENG-31", "title": "Old SMTP retries", "url": "https://linear.app/acme/issue/ENG-31"}), 12)
        .say([text("## Two options for the email queue\n\n| | BullMQ | pg-boss |\n|---|---|---|\n| Infra | Redis (**already running**) | Postgres only |\n"
                   "| Peak throughput | ~5k jobs/s | ~800 jobs/s |\n\n1. **BullMQ**: faster, one more moving part.\n2. **pg-boss**: no new infra.\n\n"
                   "Which one should I implement?")], 11, ctx=134000),
        S("d-flaky", "Fix flaky e2e checkout test", "storefront", "fix/checkout-flake", "fix the flake", 15)
        .say([text("API Error: 529 Overloaded. The session stopped after 3 retries while running Playwright.")], 14, isApiErrorMessage=True),
        S("d-csv", "Add CSV export to reports", "dashboard", "feat/csv-export", "csv export", 20)
        .created_pr("dashboard", 212, 19)
        .say([text("Opened PR #212. Typecheck fails because ReportRow.total is optional, and the e2e download step times out. "
                   "I'll look at both next unless you want something else.")], 18, ctx=301000),
        S("d-rate", "Rate-limit the public API", "billing-api", "feat/rate-limit", "rate limit ENG-38", 35)
        .created_pr("billing-api", 88, 34).say([text("Pushed the rate limiter; review asked for a per-key override.")], 34),
        S("d-pdf", "Refactor invoice PDF renderer", "billing-api", "refactor/pdf-renderer", "refactor the renderer", 1)
        # a refactor touches many files: a long "Files written" list, for the drawer's "Show all"
        .say([tool(f"w{i}", "Write", {"file_path": WORK + f"billing-api/src/pdf/{f}.ts"}) for i, f in enumerate(
            ("fonts", "table", "header", "footer", "totals", "currency", "page-break", "qr-code", "watermark", "theme", "index"))], 1)
        .say([tool("e1", "Edit", {"file_path": WORK + "billing-api/src/pdf/layout.ts"})], 0, ctx=156000),
        S("d-node", "Bump Node to 22 LTS", "web-app", "chore/node-22", "bump node", 4)
        .created_pr("web-app", 301, 3).say([text("Pushed fixes; CI is running.")], 3, ctx=97000),
        S("d-dark", "Dark mode for the settings page", "web-app", "feat/settings-dark", "dark mode", 8)
        .created_pr("web-app", 299, 7).say([text("Opened a draft PR: theme toggle, persisted, 14 components updated. All checks pass.")], 7),
        S("d-cache", "Cache exchange rates", "billing-api", "feat/fx-cache", "cache fx rates", 50)
        .created_pr("billing-api", 90, 49).say([text("PR #90 is up and green; requested review from the payments team.")], 49),
        S("d-badges", "Update README badges", "tooling", "main", "badges", 61).say([text("Updated 4 badges and the install snippet.")], 60),
        S("d-bun", "Explore Bun for build scripts", "tooling", "spike/bun-scripts", "try bun", 2881)
        .say([text("Bun cut cold start from 1.9 s to 0.4 s; two scripts still need Node APIs.")], 2880),
        # a stack of three PRs, the middle one failing: one chip in the list, a stack view in the details;
        # plus a PR in another repo, which the details show apart from the stack
        S("d-stack", "Split ledger export into a stack", "billing-api", "feat/ledger-3-ui", "split the ledger export", 26)
        .created_pr("billing-api", 95, 25).created_pr("billing-api", 96, 24).created_pr("billing-api", 97, 23)
        .created_pr("web-app", 302, 22)
        .say([text("Stacked the ledger export as three PRs: query, CSV writer, download button.")], 22, ctx=188000),
        S("d-tz", "Fix timezone bug in invoices", "billing-api", "fix/invoice-tz", "fix tz", 1441)
        .created_pr("billing-api", 77, 1440).say([text("Merged.")], 1440),
    ]
    for s in sessions:
        s.write()
    prs = {
        REPO.format("dashboard", 212): pr(212, "CSV export for reports", checks="FAILURE", failing=("typecheck", "e2e-download")),
        REPO.format("billing-api", 88): pr(88, "Rate-limit public API", review="CHANGES_REQUESTED"),
        REPO.format("web-app", 301): pr(301, "Node 22 LTS", checks="PENDING"),
        REPO.format("web-app", 299): pr(299, "Settings dark mode", draft=True),
        REPO.format("billing-api", 90): pr(90, "Cache exchange rates", review="REVIEW_REQUIRED"),
        REPO.format("billing-api", 77): pr(77, "Invoice timezone fix", state="MERGED", review="APPROVED"),
        REPO.format("billing-api", 95): pr(95, "Ledger export: query entries by period", review="APPROVED", head="feat/ledger-1-query"),
        REPO.format("billing-api", 96): pr(96, "Ledger export: stream entries as CSV", review="REVIEW_REQUIRED", head="feat/ledger-2-csv",
                                           base="feat/ledger-1-query", failing=("test-unit", "lint")),
        REPO.format("billing-api", 97): pr(97, "Ledger export: download button", draft=True, head="feat/ledger-3-ui", base="feat/ledger-2-csv"),
        REPO.format("web-app", 302): pr(302, "Ledger export link in the billing menu", head="feat/ledger-3-ui"),  # same branch name, other repo
    }
    reviews = [{"number": 412, "title": "Move webhooks to the event bus", "url": REPO.format("platform", 412), "isDraft": False,
                "repository": {"name": "platform", "nameWithOwner": "acme/platform"}, "author": {"login": "sam"},
                "updatedAt": ts(90)}]
    gh = home / "bin" / "gh"
    gh.parent.mkdir(parents=True, exist_ok=True)
    plan = [  # one or more PRs per step of the PR view
        gql("dashboard", 212, "CSV export for reports", "feat/csv-export", checks="FAILURE", said=("reviewbot",), mins=19),
        gql("billing-api", 88, "Rate-limit public API", "feat/rate-limit", review="CHANGES_REQUESTED", said=("dana", "reviewbot"), mins=34),
        gql("web-app", 301, "Node 22 LTS", "chore/node-22", checks="PENDING", mins=3),
        gql("web-app", 299, "Settings dark mode", "feat/settings-dark", draft=True, mins=7),
        gql("billing-api", 90, "Cache exchange rates", "feat/fx-cache", merge="BLOCKED", review="REVIEW_REQUIRED", mins=49, asked=2),
        gql("billing-api", 93, "Refresh cached rates hourly", "feat/fx-refresh", base="feat/fx-cache", mins=45),
        gql("billing-api", 95, "Ledger export: query entries by period", "feat/ledger-1-query", review="APPROVED", mins=25),
        gql("billing-api", 96, "Ledger export: stream entries as CSV", "feat/ledger-2-csv", base="feat/ledger-1-query", checks="FAILURE", mins=24),
        gql("billing-api", 97, "Ledger export: download button", "feat/ledger-3-ui", base="feat/ledger-2-csv", draft=True, mins=23),
        gql("tooling", 55, "Pin GitHub Actions to commit SHAs", "ci/pin-actions", said=("you", "you"), mins=300),
        gql("storefront", 140, "Lazy-load product images", "perf/lazy-images", review="APPROVED", mins=150),
        gql("storefront", 142, "Preload hero image", "perf/hero-preload", review="APPROVED", queue=2, mins=20),
        gql("web-app", 305, "Upgrade ESLint to v9", "chore/eslint-9", merge="BLOCKED", review="APPROVED", checks="PENDING", auto=True, mins=12),
        gql("web-app", 256, "Remove old feature flags", "chore/flags", merge="DIRTY", mins=30 * 1440),
    ]
    gh.write_text(FAKE_GH.replace("__PRS__", repr(prs)).replace("__REVIEWS__", repr(reviews)).replace("__PLAN__", repr(plan)))
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)

    data = home / ".claude" / "ccdeck"
    (data / "state").mkdir(parents=True, exist_ok=True)
    start = proc_start(pid)
    live = {"d-deploy": ("permission", "Claude needs your permission to use Bash"), "d-auth": ("question", None),
            "d-queue": ("stopped", None), "d-pdf": ("working", None), "d-dark": ("stopped", None),
            "d-node": ("stopped", None), "d-cache": ("stopped", None)}
    for sid, (status, msg) in live.items():
        st = {"session_id": sid, "event": "demo", "ts": __import__("time").time() + 5, "status": status,
              "pid": pid, "pid_start": start}
        if msg:
            st["message"] = msg
        (data / "state" / f"{sid}.json").write_text(json.dumps(st))
    # the bell's notifications: one per session, each in the state that raised it (else the dashboard drops it)
    (data / "inbox").mkdir(exist_ok=True)
    now = __import__("time").time()
    for i, (sid, state, title, body, ago) in enumerate((
            ("d-deploy", "permission", "Permission needed · Ship Stripe webhook handler to staging",
             "Claude needs your permission to use Bash", 2),
            ("d-auth", "question", "Question for you · Migrate auth to Lucia v4",
             "Should sessions created before the migration be invalidated, or should I write a shim that upgrades them?", 5))):
        nid = f"{int((now - ago * 60) * 1000)}-demo{i}"
        (data / "inbox" / f"{nid}.json").write_text(json.dumps(
            {"id": nid, "sid": sid, "state": state, "title": title, "body": body, "ts": now - ago * 60}))
    # what sessions said about their own work (`ccdeck report`): goals, next steps and to-dos for you
    (data / "reports").mkdir(exist_ok=True)
    for sid, rep in {
        "d-deploy": {"goal": "Launch Stripe billing", "priority": "high", "waiting": "you", "item": "P1",
                     "next": "Deploy to staging, then replay the last 50 Stripe events",
                     "todos": [{"text": "Rotate the Stripe test key in the staging vault", "kind": "hands-on", "size": "small"},
                               {"text": "Approve the staging deploy window", "kind": "decide", "size": "small", "done_at": now - 40 * 60}]},
        "d-rate": {"goal": "Launch Stripe billing", "priority": "high", "waiting": "session", "item": "P2",
                   "next": "Add the per-key override the review asked for", "after": [REPO.format("billing-api", 90)]},
        "d-cache": {"goal": "Launch Stripe billing", "waiting": "review", "item": "P3", "next": "Waiting for the payments team's review",
                    "todos": [{"text": "Decide the exchange-rate cache TTL (1 h or 24 h)", "kind": "decide", "size": "small"}]},
        "d-dark": {"goal": "Settings refresh", "waiting": "you", "next": "Mark the PR ready once the preview looks right",
                   "todos": [{"text": "Check the dark theme on the preview", "kind": "validate", "size": "small",
                              "url": "https://preview.example.com/settings"},
                             {"text": "Review the new settings copy", "kind": "review", "size": "small", "done_at": now - 3 * 3600}]},
        "d-node": {"goal": "Settings refresh", "priority": "low", "waiting": "ci", "next": "Merge when CI passes"},
        "d-csv": {"goal": "Reports v2", "waiting": "session", "next": "Make ReportRow.total required, then fix the e2e timeout"},
    }.items():
        (data / "reports" / f"{sid}.json").write_text(json.dumps(dict(rep, updated=now - 60)))
    # a goal's plan (`ccdeck plan`): items in priority order; their states come from the sessions and PRs above
    issue = lambda n, slug: f"https://linear.app/acme/issue/BIL-{n}/{slug}"
    item = lambda iid, title, url="", **kw: dict({"id": iid, "title": title, "url": url, "cwd": "/home/demo/billing-api"}, **kw)
    (data / "goals").mkdir(exist_ok=True)
    plan = {"goal": "Launch Stripe billing", "created": now - 5 * 86400, "next_id": 9, "items": [
        item("P1", "Stripe webhook handler", issue(101, "stripe-webhook-handler"), size="medium", sessions=["d-deploy"]),
        item("P2", "Rate-limit the public API", issue(102, "rate-limit-public-api"), size="small", sessions=["d-rate"]),
        item("P3", "Cache exchange rates", issue(103, "cache-exchange-rates"), size="small", sessions=["d-cache"]),
        item("P7", "Invoice timezone fix", issue(107, "invoice-timezone"), size="small", prs=[REPO.format("billing-api", 77)]),
        item("P4", "Refund flow", issue(104, "refund-flow"), size="medium", owner="Sam"),
        item("P5", "Invoice emails", issue(105, "invoice-emails"), size="medium", after=["P2"]),
        item("P6", "Tax rates per region", issue(106, "tax-rates"), size="large", status="blocked", blocked_on="Waiting for finance's rate table"),
        item("P8", "Usage-based pricing", issue(108, "usage-pricing"), size="large"),
    ]}
    name = __import__("hashlib").sha1(" ".join(plan["goal"].lower().split()).encode()).hexdigest()[:12]
    (data / "goals" / f"{name}.json").write_text(json.dumps(plan))
    (data / "marks.json").write_text(json.dumps({
        "d-bun": {"parked": True, "note": "after Q4 freeze"},
        "d-node": {"note": "waiting on QA"},
    }))
    return home


def proc_start(pid):
    """The start time ccdeck records for a pid, so it sees that process as alive."""
    import importlib.machinery
    import importlib.util
    path = str(Path(__file__).resolve().parent.parent / "ccdeck")
    loader = importlib.machinery.SourceFileLoader("ccdeck_demo", path)
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("ccdeck_demo", loader))
    loader.exec_module(mod)
    return mod.proc_start_time(pid)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("home")
    ap.add_argument("--pid", type=int, default=os.getppid(), help="a process that stays alive (default: the parent shell)")
    a = ap.parse_args()
    print(build(a.home, a.pid))
