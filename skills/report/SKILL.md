---
name: report
description: Tell ccdeck what this session is working toward, its next step, what it waits on, and what only the user can do. Use after you open or update a PR, when you end a turn waiting on the user, when you hand the user a task (a manual step, something to validate, decide or review), and when the goal or priority changes.
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/ccdeck" report *)
---

ccdeck shows the user one plan across all sessions, grouped by goal. It knows PR statuses from GitHub. It knows the rest only from what sessions report. Each call merges into this session's report.

Run it as `python3 "${CLAUDE_PLUGIN_ROOT}/ccdeck" report …` (quoted: the path can contain spaces); the examples below shorten that to `ccdeck report`.

1. **Goal.** Run `ccdeck report --goals` first and reuse a goal that fits, word for word ("Ship 0.7.4", not "0.7.4 release"). Make a new goal only for separate work. If the command prints "Did you mean", decide whether it's the same goal and fix it.
2. **State:**
   ```bash
   ccdeck report --goal "Ship 0.7.4" --priority high --next "Waiting for CodeRabbit on #44" --waiting review
   ```
   - `--next`: one line, the next concrete step.
   - `--waiting`: `you`, `ci`, `review`, `session` (you're still working), or a URL you're blocked on.
   - `--priority`: `high`, `normal` or `low`, as the user ranked it. Default `normal`. Don't raise it on your own.
   - `--after URL`: a PR or issue that must land first. Repeat it for each one; giving it replaces the list.
3. **To-dos for the user**: only what the user has to do themselves. Don't add your own work.
   ```bash
   ccdeck report --todo "Run cleanup.http on prod after the 0.7.4 release" --kind hands-on --size small --url https://…
   ```
   `--kind` is `review`, `validate`, `decide` or `hands-on`; `--size` is `small`, `medium` or `large`. Remove a to-do that's no longer needed with `--done N` (the number in the printed list). The user marks finished ones done in the dashboard; they're listed as "done by the user", and don't count toward the 10 open to-dos a session can have.

4. **Plans.** A goal can hold a plan: its work items in priority order (tickets, issues, or a title alone). The plan belongs to the goal, not to you, so it stays when this session ends.
   - When the user gives you a list of work for a goal, put it in the plan, not in to-dos:
     ```bash
     ccdeck plan --goal "Ship 0.7.4" add "Fix rounding in exports" --url https://linear.app/… --size small [--after P1] [--owner Sam]
     ```
     Then reorder with `ccdeck plan move P5 --to 1` as the user ranks them. Don't reorder on your own.
   - When you work an item, say so first: `ccdeck report --goal "Ship 0.7.4" --item P3`. ccdeck then shows the item's state from this session and its PRs. One session works one item; for the next item, the user starts a new session (the dashboard's Start copies the command).
   - ccdeck links a PR to an item when the PR's title or branch has the item's ticket key, or when this session works the item. When neither holds, link it: `ccdeck plan set P3 --pr URL`.
   - An item with a PR is done when the PR merges; don't mark it. Mark `ccdeck plan done P3` only for work without a PR. Use `plan set P3 --blocked "why"` (and `--unblock`) when it can't move, `plan drop P3` when it's no longer wanted, `plan set P4 --owner Sam` when someone else took it.
   - `ccdeck plan` (with this session's goal) prints the items with their states.

Report briefly and only when something changed. Don't mention the report to the user unless they ask.
