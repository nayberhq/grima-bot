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
