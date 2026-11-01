# Cross-server audit fixes

## Implemented

- `/network-push channel` independently configures automatic discovery publication. Omit channel to disable. Existing active unpublished events are included. `/event-watch` configures pull separately.
- Discovery messages contain mirror codes only, plus event name/time. Signup, roster and announcement changes do not flow through the discovery channel.
- Fixable discovery failures persist in SQLite and retry independently, without blocking subsequent discoveries. `/network-status retry:true` shows pending setup/delivery errors. Disabling a watch also clears its pending retries.
- `/event-status retry:true` reports mirror revisions, voice IDs, locks, announcement backlog and discovery publication result, and retries refreshes.
- `/event-repair code` checks the existing starter message and recreates a missing copy. The canonical raid ID, signups and plan survive.
- `/event-unlink guild_id` disconnects a destination and blocks rediscovery. Source organizers or that destination's administrators may disconnect it. `/event-reconnect guild_id` lets the source organizer allow it again and rotates the share code. Repair with that code reopens the copy.
- Only the owning/source server creates a temporary voice channel. Every mirror displays the owner-server invite and the owner VC link (or a pending notice before start). Grima creates a non-expiring source-forum invite when permitted; `/event-invite url` lets a source-server administrator supply a verified invite instead. Invites do not bypass membership screening or channel permissions. The former `/event-meeting` override is removed.
- Mirror-only servers may omit voice_category from `/event-setup`. Hosting their own events still requires one. Legacy mirror VCs are retired only when empty.
- Creation panels repost after human activity (five-second batching), removing the old panel. Bot messages do not trigger bumps. Failed Discord requests are logged; another human message retries the bump. These are now bottom-of-channel panels rather than permanently pinned anchors.
- Shared green signup opens an inline class/spec picker. A class button without saved tier specs also opens it. Submit saves the spec and signup together. Shared signup no longer needs local class-role creation.
- Shared events use the planned roster only; legacy lineup controls direct organizers to `/roster-create`, `/roster-edit`, `/roster-swap` and `/planner-import`. Legacy nonshared events retain their controls. Existing legacy assignments are not migrated automatically.
- Planned rosters mark departed players and withdrawn class selections. A withdrawn class does not silently remove the organizer's assignment.
- Vanguard compact export now carries explicit fellowship member IDs. Grima validates complete, unique membership and preserves fellowship sizes and order. Adding/removing a member clears that team's fellowship layout; replacing or swapping slots retains it.
- The Vanguard button offers an Import plan code dialog. Vanguard's Return plan to Grima button generates a compressed code to paste back. Current roster revision is captured when the dialog opens; concurrent changes are rejected. Decompression is bounded to 2 MB. Plans exceeding Discord's 4,000-character modal limit use file import.

## Verification

38 Python tests pass, including canonical signup, mirror deduplication, setup-failure retry, publication deduplication, blocked destinations, deleted-copy repair, inline signup, class withdrawal, fellowship validation, plan-code limits, roster revisions and lifecycle boundaries. Vanguard's JavaScript test suite and bridge syntax checks pass. Discord endpoints in Python tests are mocks; browser clipboard and live multi-server operation have not been verified.

## Still outstanding — not a completed end-to-end release

- Deploy the included Vanguard Pages patch and the Grima bot, then rehearse in two real Discord servers. No remote pushes or deployments were performed.
- Serverless conversion remains unimplemented. These changes still run in the existing gateway process with a persistent SQLite database. Ordinary channel watchers, panel bumps and occupied-VC cleanup use gateway state. An HTTP interaction service plus durable scheduled workers/polling and shared storage requires a separate hosting implementation.
- Status reports revision/backlog state rather than a timestamped delivery history. Known crash windows between Discord accepting a message and SQLite recording it remain; a crash can duplicate publication or a panel/roster message.
- Existing selected-lineup assignments need explicit migration/import into the planned roster when upgrading a live installation. No existing live database was available here.

Apply `vanguard-pages-bridge.patch` with `git am` in the planner repository based on `5b2e20f727a95466a9555b1eccabb76fea222584`; the patch contains the entire local bridge series. It is intended for `nayberhq/vanguard-raid-planner`, never the original Saruman repository.

## Manual publication of announcement-channel events

Push sends the discovery message to the configured channel but never invokes
Discord Publish/crosspost. An authorized Discord user chooses which messages
to publish to follower servers. When Grima watches the source announcement
channel directly, unpublished mirror-code messages enter a durable pending
queue and sync only after Discord marks them published. Publication checks
run on the lifecycle cycle, including after restarts. Deleted messages and
expired/revoked event codes are retired; fixable sync errors enter the normal
retry queue. `/network-status` shows the number awaiting publication.

Ordinary text-channel watchers still import new mirror codes automatically;
use an announcement channel when manual Publish is the desired approval step.
These rules apply to new discoveries, not mirrors already connected before
this update. The current suite has 40 passing Python tests, including manual
publication gating, restart recovery, and verification that push never calls
Discord's publish method. Live Discord validation remains pending.

## Second-audit hardening

- `/event-share` and reconnect reuse the current code. Explicit revocation is still supported. Existing discovery messages are not silently invalidated by requesting the code again.
- Unlink commits the block and locked mirror state transactionally before best-effort Discord cleanup. Missing messages or denied cleanup permissions cannot prevent disconnection.
- Planner sessions preserve `grimaEventId`. File/code imports now require a matching identity; older exports without it must be regenerated from a fresh Grima export using the updated planner.
- Imports and manual assignment edits validate the selected specialization against saved tier choices (unspecified specs retain the existing all-supported-specs policy).
- Unchanged roster messages are checked for existence and recreated if deleted. Roster delivery status records last successful synchronization and the latest Discord error, exposed in `/event-status`.
- Source invites are checked hourly. Missing/invalid invites are removed and event projections marked stale; ordinary lifecycle processing attempts regeneration. Transient Discord errors retain the current invite.

42 Python tests pass; planner JavaScript syntax and bridge tests pass. Full live browser/Discord verification has not been performed. Remaining work is not complete: organizer dropdown UI, automatic legacy-lineup migration, crash-window reconciliation, deployment, serverless conversion, and a live two-server permissions/recovery rehearsal.

## Organizer UI, legacy migration, and discovery-send recovery

The signup lineup button and `/roster-manage` now open private team, slot,
player, and offered-class/spec dropdowns. Player and spec lists paginate.
Save/remove reuse the authoritative roster edit validation; authority and
revision are checked on every interaction. Swap remains available as a command.

Startup migrates unambiguous shared-event legacy Assignment rows to EventPlans
without deleting the source rows or overwriting an existing plan. Unknown
class/spec assignments, duplicate players, and oversized lineups are flagged
in `/event-status` for manual review rather than guessed. New-editor creation
is blocked on those flagged records. Explicit replacement through import or
roster-create remains an organizer action.

Discovery sends now persist an intent before sending. After an interrupted
attempt, the sender searches subsequent bot-authored channel history for the
original content before retrying, or uses the already-recorded message ID.
Unreadable history does not authorize a resend. Recovery refuses to search
beyond 2,000 messages and reports the error for operator review. This reduces
crash duplicates; it is not a transactional exactly-once guarantee from Discord.
This recovery currently covers discovery publication only, not roster messages,
organizer announcements, panels, or thread creation.

47 Python tests pass. Still outstanding: recovery for those other Discord
writes; serverless hosting implementation and deployment; real browser/Discord
validation across two servers. No remote pushes or deployments performed.
