# Keeping grima-bot current with Saruman

grima-bot is a Git fork with Saruman's original history and our changes on top.
It runs as one Discord bot. The upstream repository is
`https://github.com/Baviaan/lotro.git`; our deployment, configuration and write
remote are separate. A fork can receive upstream updates without sending our
changes back to the original project.

## Why a fork

The independent features live in `source/events_cog.py`, `event_store.py`, and
`event_panels.json`, with their own regression tests. Small integration changes
remain in Saruman's startup, calendar, raid callbacks and privacy text. They are
necessary because Saruman uses Discord message IDs as raid IDs, assumes local
guild membership during assignment, and deletes events two hours after starting.

A wrapper would need to monkey-patch these internal methods or apply patches at
build time. That would still depend on upstream internals while hiding merge
conflicts. Keeping explicit commits makes those differences reviewable.

## Create the hosted fork

No hosted repository has been created or pushed by this implementation.
On GitHub, fork `Baviaan/lotro` into your account or organization and name the
repository **grima-bot**. Do not open a pull request against Baviaan/lotro for
these independent changes.

The full-history `grima-bot.bundle` is the preferred handoff; an ordinary ZIP
does not contain Git history. Restore it with:

```sh
git clone -b main grima-bot.bundle grima-bot
cd grima-bot
git remote remove origin
git remote add upstream https://github.com/Baviaan/lotro.git
git remote set-url --push upstream DISABLED
git remote add origin https://github.com/YOUR-ACCOUNT/grima-bot.git
git push -u origin main
```

Replace `YOUR-ACCOUNT` with the actual fork owner. The initial clone's `origin`
points to the bundle, so remove it before adding the hosted fork. The final push
creates `main` in **your fork** and preserves its existing `master`; set `main`
as your fork's default branch in GitHub settings. No force-push is needed.

For the existing local checkout, simply add your fork as `origin`; `upstream`
already exists with pushes disabled. Always check `git remote -v` before the
first push. The upstream submodule in `data/` remains read-only LOTRO content;
use `git submodule update --init --recursive` if you need the optional loot data.

## Receive upstream updates

Commit your work first and install `requirements.txt` in the Python environment
used for validation. Then run:

```sh
python scripts/upstream_update.py
```

The script:

1. Verifies the upstream URL and disables pushing to it.
2. Fetches Saruman's `master` branch.
3. Creates an isolated `upstream-sync/<timestamp>` branch and sibling worktree.
4. Merges upstream into that candidate branch, preserving both histories.
5. Runs the regression suite and application syntax checks.
6. Prints commands to review and accept the candidate with a fast-forward merge.

It never pushes, deploys, resets your branch, or accepts an update automatically.
If a merge conflicts or a check fails, it leaves the candidate checkout in place
for repair and leaves your original branch untouched. Resolve conflicts there,
commit the resolution, and rerun the tests before merging the candidate.
`--no-fetch` uses the locally available `upstream/master` ref for offline work.

After review, push to `origin main` explicitly. Recheck a real event across two
test Discord servers before deployment; mocked tests cannot validate Discord's
actual permissions, gateway delivery, or UI rendering.

## Integration points to check during upgrades

| Upstream area | Grima integration |
| --- | --- |
| `bot.py` | Message/voice intents and loading the new cog |
| `raid_cog.py` | Canonical IDs, forum creation, shared signup updates, permissions and cancellation |
| `calendar_cog.py` | Mirrored event links and shared duration |
| `config_cog.py` | Privacy notice for shared/retained rosters |
| `setup.py`, `source/__init__.py` | Independent project name and fork version |

Keep new capabilities in Grima-owned files where possible. Avoid broad formatting
or unrelated refactoring of upstream files; those changes make later merges
harder. Preserve upstream authorship and the GPL-3.0 license.
