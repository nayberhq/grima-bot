# Templates and Vanguard roster planning

These commands extend the current gateway-based Grima prototype. They do not yet
convert Grima to serverless or add the discussed push-channel configuration.
Templates and plans live in the same SQLite database as the canonical event.
Roster updates go directly to linked threads; they do not pass through the
mirror-code discovery network. Run one Grima process against the database.

## Templates

Admins use `/event-template kind:description text:...` or
`/event-template kind:announcement text:...`. Omit text to view; `reset:true`
removes a template. Templates belong to the source server, including when
rendered in another server's mirror. Description changes apply to existing
unlocked events on their next refresh, as well as future events.

Supported tags:

| Tag | Value |
| --- | --- |
| `{title}` | Event title |
| `{tier}` | Event tier |
| `{description}` | Original event aim |
| `{start}`, `{end}` | Discord timestamps displayed in the reader's timezone |
| `{organizer}` | Organizer mention, without pinging |
| `{signup_count}` | Current available signup count |

Use doubled braces for literal braces. No expressions, attributes, conversions
or format specifications are allowed. Template source is limited to 1,000
characters. Announcement output is limited to 1,800 characters; descriptions
must fit Discord's embed budget. An oversized description falls back to the
ordinary description and logs the problem.

Example description: `{title} {tier} — {description}\nStarts {start}. Organizer: {organizer}`
(The Discord input needs a real newline for line breaks.)

Example announcement: `{title} starts {start}. We have {signup_count} signups!`

`/event-announce` with no message renders the saved announcement template once
with current event values, then sends the same snapshot to every linked thread.
Supplying `message` sends literal text instead. All mentions remain disabled.

## Planner round trip

1. Inside an event thread, use `/planner-export`. Only the organizer or a
   source-server raid leader can export, import, or edit the plan.
2. Import the attached session JSON into Vanguard Raid Planner. Configure the
   desired composition and generate teams. Then export **Session JSON**.
3. Use `/planner-import session:<file> expected_revision:0` for the first plan.
   It replaces the complete planned roster. Later replacements require the
   current revision from `/roster-show` to prevent overwriting concurrent edits.
4. Use `/roster-edit expected_revision:N team:1 slot:2 player_id:<Discord ID>
   class_name:Guardian specialization:Blue` to replace a slot. The next unused
   slot appends; omitting player_id removes a slot and shifts later numbers.
   `/roster-swap` swaps two slots within or between teams. Use `/roster-show`
   to inspect the updated revision and numbered slots after each change.

Each team is published as one or more six-player roster messages in every
unlocked linked thread. Newly linked threads also receive the current plan.
Edits update those messages, and failed requests retry every 30 seconds.
Departed/unavailable signups are visibly flagged; they are not silently removed
from the organizer's plan. This is a planned roster separate from Saruman's
legacy Selected line up controls; use `/roster-edit` and `/roster-swap` for it.
Signups and their selected classes are not changed by importing a plan.

## Interchange details and limits

Tested against `nayberhq/vanguard-raid-planner` commit
`5b2e20f727a95466a9555b1eccabb76fea222584`, using its actual `validateSession`
and `compactSessionSnapshot` functions. Export uses session v6; import accepts
v6 and compact v7. Discord IDs remain strings (including IDs above JavaScript's
safe-integer range). Import matches current signups by ID, never by name;
unknown players, unselected classes, duplicate members in a team and invalid
class/spec/role combinations are rejected before any write. It rejects dirty
or ungenerated sessions. Files are limited to 2 MB, plans to 16 teams, and each
team to the event size.

Export includes available signups only, saved specializations for the event's
tier, one run at relative hour 0, and no composition requirements. When no spec
is recorded, all supported specs for that class are offered. Choose composition
requirements in Vanguard. The supported event size is 3–24, duration up to six
hours, and free-people classes only. The export does not include server-wide
player data, unavailable signups or other events.

Import preserves team labels, member order, assignments and planner time.
Displayed six-player groups follow member order; they are **not** Vanguard's
computed fellowship allocation, which compact session export omits. Advanced
constraints, character identities, notes and recommendations are not imported.
Import does not change Discord event times or create additional events for
extra planner runs. A planner time is displayed as provided, not treated as a
new scheduled event time. Vanguard can discard Grima's export event identifier;
when absent, the target is the thread where the command is run and all player
IDs are still checked against that event's current signups.

As with announcement delivery, a process crash between Discord accepting a new
roster message and saving its ID can leave a duplicate. Manually deleted roster
messages are recreated when their content next changes; unchanged deleted
messages are not proactively detected. Closed threads are not reopened to
update plans. Preserve the database across restarts. Live Discord deployment
and permission checks are still required; tests use mocked Discord endpoints.

## Signup button → Vanguard Pages

The persistent **Vanguard** button on free-people signup messages uses the same
organizer/source raid-leader authorization as `/planner-export`. It sends a
private, fresh signup snapshot link to
https://nayberhq.github.io/vanguard-raid-planner/ . Small snapshots get a link
button; longer ones get a clickable text link because Discord limits button
URLs to 512 characters. Oversized snapshots fall back to the existing session
attachment and a plain site link.

The Pages bridge opens a dialog with **Copy JSON**, **Download JSON**, and
**Load signups**. Discord itself cannot write to the clipboard or automatically
open the browser after a bot callback, so the user follows the private link,
then chooses the action on the site. Load signups imports directly without
copy/paste and replaces the current plan only after that explicit click.
Clipboard denial has a download fallback.

The compressed snapshot is carried in the URL fragment, which the Pages HTTP
request does not send to the host. The bridge clears it from the address bar
immediately. The link still contains signup names and IDs: keep it private.
It is a point-in-time export, not a live connection and not a secret-free URL.
This bridge needs no bot API or persistent web server.

**Deployment prerequisite:** `vanguard-pages-bridge.patch` contains the matching
change for `nayberhq/vanguard-raid-planner` based on commit
`5b2e20f727a95466a9555b1eccabb76fea222584`. Apply with `git am` in that repository,
run its tests, and deploy its Pages site before enabling this Grima version.
The bridge and bot changes have been committed locally only; neither has been
pushed or deployed. Without the bridge deployment, the site opens but does not
read the snapshot. A live Discord/browser clipboard check remains pending.
