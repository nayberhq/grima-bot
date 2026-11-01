# grima-bot shared event forums

grima-bot forks Baviaan/lotro (base commit `36e97bd`) with persistent creation
panels, event forums, temporary voice channels, a shared cross-server roster,
announcement delivery, and `mirrorkey:` channel watchers. It is a local
implementation, not a deployed service. The upstream GPL-3.0 license remains.

See [PLANNING.md](PLANNING.md) for description/announcement templates, Vanguard
export/import and synchronized planned-roster editing.

## Installation

Create a separate Discord application named **grima-bot** and use its bot token.
The application name is configured in Discord; renaming this repository does not
rename an existing Discord application.

For GitHub, create your own fork named `grima-bot`. Keep Baviaan/lotro as the
fetch-only `upstream`; add your fork as `origin`. Push only to your fork. The local
working repository has upstream pushes disabled and no `origin` remote. These
local Git settings are not embedded in source archives.

1. Use one bot deployment and one SQLite database for every participating server.
   Invite that bot to the source and destination servers. Separate installations
   do not share events or recognize each other's sharing codes.
2. Install `requirements.txt`, configure `source/config.json` using the supplied
   example, and run the existing `source/main.py` from the `source` directory.
3. Enable **Message Content Intent** in the Discord Developer Portal for this
   application. The code also enables Guild Messages and Voice States. Voice
   States lets cleanup detect whether a voice channel is occupied; the bot does
   not join voice, listen, or record audio.
4. Ensure the bot can view channels, read message history, send messages, embed
   links, send messages in threads, manage threads, and manage channels in the
   configured destinations. Grant pin permissions in creation-panel channels.
   Existing Saruman class-role buttons still use Manage Roles. The optional
   native Discord calendar feature uses Manage Events.
5. Sync application commands using upstream's owner-only `!sync` command (or the
   existing command registration flow). No commands are registered against a live
   server as part of the supplied tests.
6. Run `/event-setup forum:<forum> voice_category:<category>` in each participating
   server. The forum must not require tags. The defaults are a 180-minute duration
   and locking 60 minutes after the end. Configuration applies to new events;
   existing events retain their duration and lock delay.

Back up `raid_db` before upgrading. New tables are additive; existing ordinary
text-channel raids keep their old behavior. After setup, newly created raids use
the configured forum, including events created through existing slash commands.

## Creation panels

Run `/event-panel preset:cluster` or `/event-panel preset:misc` in your creation
channel. The bot posts and pins an embed with persistent buttons. They work after
a restart. Configure `/leader` to allow the intended event creators to use them.

The cluster panel includes Folly, Wailing Hold, Palace, Garden, and Hunter Dailies.
The miscellaneous panel includes 12- and 6-player events. Clicking a button opens
a modal with title, start time, tier, duration and description. Blank duration
uses the server default. Start times use Saruman's existing personal/server
timezone configuration.

Templates live in `source/event_panels.json`. Restart and refresh the affected
panels when changing their structure. These are pinned persistent panels, not
continually deleted and reposted messages. A dedicated creation channel, as in
the reference screenshot, keeps the panels together. Poll creation is not part
of this implementation.

## Event lifecycle

- Each event has one canonical raid record and one forum projection per server.
- Thread titles are `<weekday> <title>`, with the tier appended when set. The
  weekday uses each destination server's configured timezone.
- Signup buttons across all copies operate on the same player IDs and roster.
  Nicknames reflect the server where the player last updated their signup.
- At the start, a voice channel appears in each destination's configured category.
  It inherits that category's permissions; it is not automatically restricted to
  signed-up players. Its link is included in the local signup embed.
- At the end, signup controls stop accepting changes. Empty voice channels are
  deleted; occupied ones remain until empty. At end plus the configured delay,
  threads are locked and archived, retaining their discussion and final roster.
- The scheduler checks every 30 seconds and uses persisted IDs and timestamps.
  Restarting after the event does not create an already-expired voice channel.
- `/event-duration minutes:240` inside any linked post changes the common
  duration while the event is still active. Settings edits synchronize titles,
  time and roster. Rescheduling an active event retires an early voice channel
  when empty and recreates it at the new start.
- The existing settings dialog's `delete` action cancels a shared event: all
  copies close, with occupied voice channels cleaned up once empty.

The organizer and raid leaders/administrators in the **source server** manage the
shared roster, settings and announcements. Destination administrators may sync
events and configure their own watchers; they do not gain control of the source
event simply by mirroring it.

## Manual sharing and announcements

In an event forum post, run `/event-share`. It returns a private response with a
sharing code and a keyword such as:

```
mirrorkey:<generated-32-character-code>
```

A destination raid leader runs `/event-sync code:<code>` to create their local
forum copy. Running this again returns the existing post. The shared event's
duration and lock delay apply to all its copies; each destination chooses its own
forum, timezone and voice category.

Sharing codes grant access to the roster and announcements to servers using this
deployment. Post them only where you intend to distribute the event.
`/event-share` rotates the code; `/event-share revoke:true` disables new mirrors.
Neither operation disconnects already-linked servers.

Run `/event-announce message:<text>` inside a linked post to deliver an
announcement to every currently linked thread. Ordinary conversation is **not**
mirrored. Delivery is queued in SQLite; a failure in one server does not prevent
delivery to others. All mentions are disabled for broadcast messages. The
destination list is captured when the command is issued, so newly connected
servers do not receive old announcements.

## Automatic mirrorkey watchers

In each receiving server, an administrator runs:

```
/event-watch source_channel_id:<channel-ID>
```

The source can be a text channel or an individual thread, including one in another
server. Both the configuring administrator and the bot need access and Read
Message History there. The bot must also be installed in the source server.

Whenever a **new message's text** contains `mirrorkey:<code>`, the watcher invokes
the same shared-event creation function as `/event-sync`. This works with human,
bot and webhook messages. Keys are case-sensitive and must be generated by this
deployment's `/event-share`. Multiple distinct markers in one message work;
repeated markers and repeated messages do not create additional copies.

Watching starts at the newest message when configured; it does not import the
channel's old events. A persistent cursor catches messages sent during downtime,
up to 100 messages per watcher per pass. A transient Discord failure holds the
cursor for retry. Invalid, revoked and expired keys are skipped. Text edits to
existing messages and markers inside embeds/attachments are not monitored.

To stop watching:

```
/event-watch source_channel_id:<channel-ID> enabled:false
```

Existing mirrors remain connected. Newly created mirrors contain no mirrorkey
marker, preventing a self-generated repost loop.

## Verification and remaining deployment checks

Run from the repository root:

```
python -m unittest discover -s tests -v
python -m py_compile source/events_cog.py source/event_store.py source/raid_cog.py source/calendar_cog.py source/bot.py
```

The regression suite uses real SQLite and discord.py command/view classes with
mocked Discord I/O. It covers shared signup/cancellation, concurrency and duplicate
sync, cross-server permissions, durable watcher cursors, retries, timezone names,
voice lifetime, delayed locking, ended-event rejection, token rotation, and
calendar projections. No live Discord end-to-end test has been run.

Before enabling in a community server, exercise a test event across two test
servers: create from a panel, publish its marker, sign up from both servers,
announce, restart, reschedule, cancel, and confirm an occupied voice channel is
not deleted. Check real role/channel permission overwrites and portal intents.

Known boundaries:

- Run a **single bot process** per database. In-process locks do not coordinate
  multiple workers. Discord and SQLite cannot commit atomically: a process crash
  between remote thread creation and saving its ID can leave an orphan forum
  post, and a crash between announcement send and acknowledgement can produce a
  duplicate announcement on retry. Voice creation has a recovery marker.
- A destination with missing access is retried; deleted forum projections are
  not automatically recreated. Repair permissions or recreate the event.
- Announcement retries never reopen a locked thread; an undelivered outbox entry
  can remain after closing. Operational logs identify failures without logging
  sharing codes.
- Forum discussions and the shared event's database records are retained after
  closing. This differs from upstream's two-hour deletion behavior; update your
  community's retention notice. The old built-in `/privacy` message is amended
  in this branch, and README policy below points to this retention model.
- The in-channel `/calendar` includes mirrored events. Native Discord scheduled
  events remain an optional source-server feature; mirrors use forum posts and
  timed voice creation, not a second native calendar event.
