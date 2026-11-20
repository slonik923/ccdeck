# ccdeck

One Python file (`ccdeck`, standard library only, except the optional PyGObject import for clickable Linux notifications; see `.github/styleguide.md`) plus `web/`. Tests: `python3 -m unittest discover -s tests` (also run them with `CCDECK_NO_PROC=1`, the macOS code path). Bump `VERSION` in `ccdeck` and `.claude-plugin/plugin.json` together in every release.

## Version-bumping PRs are stacked

Only one open PR at a time may bump `VERSION` against `main`. When another open PR already bumps it, stack the new one on top with `gh stack` (the `github/gh-stack` extension). Don't open it against `main`.

- **Versions:** each layer takes the next patch version after the layer below (0.8.2 → 0.8.3 → 0.8.4), so the version lines never conflict and each PR's diff shows only its own change.
- **Adding a layer:** `gh stack init <bottom> … <top>` adopts existing branches, bottom first. First bring each one up to date as a local branch: `git fetch origin`, then `git switch <branch>` and `git merge --ff-only origin/<branch>` (`--ff-only` refuses if the two have diverged: sort that out before going on). gh-stack 0.1.0 can create a new branch for a name that's only on the remote, and `submit` pushes the local tip with `--force-with-lease` against the remote one it just fetched, so a local branch that's behind would rewind the PR's branch and drop commits pushed to it. Submit right after, so nobody pushes in between. `gh stack add <branch>` adds a new top layer. `gh stack submit --auto` pushes, retargets each PR's base and links the stack on GitHub.
- **Moving open PRs into a stack:** rebase each branch onto the one below, then `git push --force-with-lease`. In a conflict, fix only the conflicting hunks; never `git checkout --theirs <file>`, which replaces the whole file and drops the lower layer's changes. Afterwards, check that each layer's diff against its new base matches its old diff against `main`, apart from the version it bumps from.
- **Editing a lower layer:** run `gh stack sync` (or `gh stack rebase` then `gh stack push`) so the layers above pick it up.
- **Merging:** merge from the bottom; GitHub retargets and rebases the layers above. Don't close a layer in the middle, because that blocks every PR above it.
- **PRs that don't bump `VERSION`** (docs, CI) go to `main` as usual.

## PR screenshots

PRs that change what the dashboard shows get before/after screenshots, made with the `pr-screenshots` skill (dev-core plugin). The project-specific parts:

- **Demo data:** `scripts/demo_data.py <dir> --pid <pid>` writes a made-up HOME: sessions in every state, hook state, marks, and a fake `gh` in `<dir>/bin` for PR states and review requests. Nothing in it is real, so images are safe to publish. Add a session there when a change needs a case it doesn't cover. Never use real names, repos or tickets.
- **Run:**
  ```bash
  <skill>/scripts/before-after.sh --spec scripts/shots/<topic>.json --base origin/main [--head origin/<branch>] \
    --prepare 'python3 scripts/demo_data.py {data} --pid $SHOTS_KEEPER_PID' \
    --serve 'HOME={data} PATH={data}/bin:$PATH CCDECK_AUTOSTART=0 CCDECK_NOTIFY=0 python3 ccdeck serve --port {port}'
  ```
  The fake `gh` answers within a second, so the defaults are enough. No login is needed.
- **Specs** live in `scripts/shots/`. `states-pr.json` is an example.
- **Selectors:**
  - cards and rows have `data-sid` (the demo ids are `d-deploy`, `d-auth`, `d-queue`, `d-flaky`, `d-csv`, `d-rate`, `d-pdf`, `d-node`, `d-dark`, `d-cache`, `d-badges`, `d-bun`, `d-tz` and `d-stack`);
  - the Needs-you queue buttons have `data-nav`;
  - pills have `data-g` (group) and `data-k` (state);
  - the drawer is `.drawer`, and its sections are `.dsec`.
- **Upload:** `SHOTS_REPO=slonik923/ccdeck`, the repo's current name, because the origin remote may still use the old `cc-dashboard` name.
