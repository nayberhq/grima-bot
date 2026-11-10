import dateparser
import discord
import logging
import pytz
import re

from datetime import datetime, timedelta, timezone
from discord import app_commands
from discord.ext import commands

from database import select_one, select_order, upsert
from utils import chunks
from event_store import EventStore

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


@app_commands.guild_only()
class CalendarCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.conn = bot.conn
        EventStore(self.conn)
        if 'calendar_event_id' not in {r[1] for r in self.conn.execute('PRAGMA table_info(Settings)')}:
            self.conn.execute('ALTER TABLE Settings ADD COLUMN calendar_event_id INTEGER')
            self.conn.commit()
        self.conn.execute('CREATE TABLE IF NOT EXISTS LiveEvents (guild_id INTEGER, raid_id INTEGER, event_id INTEGER, '
                          'PRIMARY KEY (guild_id, raid_id))')
        self.conn.commit()
        self.time_cog = bot.get_cog('TimeCog')
        self.upcoming_events = None
        self.cached_events_at = None

    def is_raid_leader(self, user, guild):
        if user.guild_permissions.administrator:
            return True
        raid_leader_id = select_one(self.conn, 'Settings', ['raid_leader'], ['guild_id'], [guild.id])
        if raid_leader_id:
            raid_leader = guild.get_role(raid_leader_id)
            if raid_leader in user.roles:
                return True
        return False

    async def post_calendar(self, guild_id, channel):
        embeds = self.calendar_embeds(guild_id)
        msg = await channel.send(embeds=embeds)
        ids = "{0}/{1}".format(channel.id, msg.id)
        upsert(self.conn, 'Settings', ['calendar'], [ids], ['guild_id'], [guild_id])
        self.conn.commit()
        await self.ensure_calendar_event(channel.guild)

    async def update_calendar(self, guild_id):
        conn = self.bot.conn
        res = select_one(conn, 'Settings', ['calendar'], ['guild_id'], [guild_id])
        if not res:
            return
        result = res.split("/")
        chn_id = int(result[0])
        msg_id = int(result[1])
        chn = self.bot.get_channel(chn_id)
        try:
            msg = chn.get_partial_message(msg_id)
        except AttributeError:
            logger.warning("Calendar channel not found for guild {0}.".format(guild_id))
            res = upsert(conn, 'Settings', ['calendar'], [None], ['guild_id'], [guild_id])
            if res:
                conn.commit()
            await self._drop_calendar_event(guild_id)
            return

        embeds = self.calendar_embeds(guild_id)
        try:
            await msg.edit(embeds=embeds)
            guild = self.bot.get_guild(guild_id) if hasattr(self.bot, 'get_guild') else None
            if guild:
                await self.ensure_calendar_event(guild)
        except discord.Forbidden:
            logger.warning("Calendar access restricted for guild {0}.".format(guild_id))
            return
        except discord.NotFound:
            logger.warning("Calendar post not found for guild {0}.".format(guild_id))
            upsert(conn, 'Settings', ['calendar'], [None], ['guild_id'], [guild_id])
            conn.commit()
            await self._drop_calendar_event(guild_id)
            return
        except discord.HTTPException as e:
            logger.warning("Failed to update calendar for guild {0}.".format(guild_id))
            logger.warning(e)
            return

    async def _drop_calendar_event(self, guild_id):
        guild = self.bot.get_guild(guild_id)
        if guild:
            await self.remove_calendar_event(guild)

    def _calendar_raids(self, guild_id):
        conn = self.bot.conn
        return conn.execute('''SELECT r.channel_id,r.raid_id,r.name,r.tier,r.time,r.guild_id FROM Raids r
            LEFT JOIN SharedEvents e USING (raid_id) WHERE r.guild_id=?
            AND (e.raid_id IS NULL OR (e.cancelled=0 AND r.time+e.duration>CAST(strftime('%s','now') AS INTEGER)))
            AND NOT EXISTS (SELECT 1 FROM EventPolls tp WHERE tp.raid_id=r.raid_id AND tp.kind='time' AND tp.resolved=0)
            UNION ALL SELECT c.thread_id,c.message_id,r.name,r.tier,r.time,r.guild_id
            FROM EventCopies c JOIN Raids r USING (raid_id) JOIN SharedEvents e USING (raid_id)
            WHERE c.guild_id=? AND r.guild_id!=? AND e.cancelled=0
            AND r.time+e.duration>CAST(strftime('%s','now') AS INTEGER)
            AND NOT EXISTS (SELECT 1 FROM EventPolls tp WHERE tp.raid_id=r.raid_id AND tp.kind='time' AND tp.resolved=0)
            ORDER BY 5''', (guild_id, guild_id, guild_id)).fetchall()

    def calendar_server_links_embed(self, guild_id, raids):
        shown = raids[:20]
        host_ids = []
        seen = set()
        if any(r[5] == guild_id for r in shown):
            host_ids.append(guild_id)
            seen.add(guild_id)
        for raid in shown:
            source_guild_id = raid[5]
            if source_guild_id not in seen:
                host_ids.append(source_guild_id)
                seen.add(source_guild_id)
        if not host_ids:
            return None
        store = EventStore(self.conn)
        lines = []
        for source_guild_id in host_ids:
            invite_row = store.one('SELECT url FROM SourceInvites WHERE guild_id=?', (source_guild_id,))
            url = invite_row['url'] if invite_row else _('Invite unavailable')
            source_guild = self.bot.get_guild(source_guild_id) if hasattr(self.bot, 'get_guild') else None
            fallback_name = getattr(source_guild, 'name', None) if source_guild else None
            name = fallback_name or f'Server {source_guild_id}'
            badge = store.guild_badge(source_guild_id, fallback_name=fallback_name)
            prefix = f"{badge} " if badge else ""
            lines.append(f"{prefix}**{name}**: {url}")
        return discord.Embed(
            title=_('Server links'),
            description='\n'.join(lines),
            colour=discord.Colour(0x3498db),
        )

    def _server_tz(self, guild_id):
        name = None
        time_cog = self.bot.get_cog('TimeCog') if hasattr(self.bot, 'get_cog') else None
        if time_cog:
            name = time_cog.get_server_timezone(guild_id)
        name = name or getattr(self.bot, 'server_tz', None) or 'UTC'
        try:
            return pytz.timezone(name)
        except pytz.UnknownTimeZoneError:
            return pytz.utc

    def _run_line(self, guild_id, raid, store):
        source_guild_id = raid[5]
        source_guild = self.bot.get_guild(source_guild_id) if hasattr(self.bot, 'get_guild') else None
        fallback_name = getattr(source_guild, 'name', None) if source_guild else None
        badge = store.guild_badge(source_guild_id, fallback_name=fallback_name)
        prefix = f"{badge} " if badge else ""
        label = f"{raid[2]} {raid[3]}" if raid[3] else raid[2]
        link = f"https://discord.com/channels/{guild_id}/{raid[0]}/{raid[1]}"
        return f"<t:{int(raid[4])}:f> · {prefix}[{label}](<{link}>)"

    def calendar_runs_embed(self, guild_id, raids):
        store = EventStore(self.conn)
        title = _("Scheduled runs:")
        desc = _("Click the link to sign up!")
        embed = discord.Embed(title=title, description=desc, colour=discord.Colour(0x3498db))
        tz = self._server_tz(guild_id)
        days = []
        for raid in raids[:20]:
            day = datetime.fromtimestamp(int(raid[4]), tz)
            header = f"{day.strftime('%A, %b')} {day.day} ({day.strftime('%Z')})"
            if not days or days[-1][0] != header:
                days.append((header, []))
            days[-1][1].append(self._run_line(guild_id, raid, store))
        for header, lines in days:
            name, value = header, ''
            for line in lines:
                if value and len(value) + 1 + len(line) > 1024:
                    embed.add_field(name=name, value=value, inline=False)
                    name, value = f"{header} (cont.)", ''
                value = f"{value}\n{line}" if value else line
            embed.add_field(name=name, value=value, inline=False)
        embed.set_footer(text=_("Last updated"))
        embed.timestamp = datetime.now()
        return embed

    def calendar_embeds(self, guild_id):
        raids = self._calendar_raids(guild_id)
        embeds = []
        links = self.calendar_server_links_embed(guild_id, raids)
        if links is not None:
            embeds.append(links)
        embeds.append(self.calendar_runs_embed(guild_id, raids))
        return embeds

    def calendar_embed(self, guild_id):
        return self.calendar_runs_embed(guild_id, self._calendar_raids(guild_id))

    CALENDAR_EVENT_NAME = "Raid calendar"
    CALENDAR_EVENT_LEAD = timedelta(days=1)
    CALENDAR_EVENT_RENEW = timedelta(hours=12)
    CALENDAR_EVENT_LENGTH = timedelta(hours=1)

    CALENDAR_EVENT_UPCOMING = 5
    EVENT_DESCRIPTION_LIMIT = 1000

    def calendar_event_description(self, guild_id, channel_id, msg_id):
        """Next runs that have not started, plus the full calendar link, within Discord's limit."""
        now = datetime.now(timezone.utc).timestamp()
        calendar = f"https://discord.com/channels/{guild_id}/{channel_id}/{msg_id}"
        footer = f"Full calendar: {calendar}"
        upcoming = [r for r in self._calendar_raids(guild_id) if int(r[4]) > now][:self.CALENDAR_EVENT_UPCOMING]
        lines = []
        for raid in upcoming:
            start = int(raid[4])
            label = f"{raid[2]} {raid[3]}" if raid[3] else raid[2]
            lines.append(f"<t:{start}:f> (<t:{start}:R>) · {label}\n"
                         f"https://discord.com/channels/{guild_id}/{raid[0]}/{raid[1]}")
        header = "Next runs:" if lines else "No upcoming runs yet."
        while lines and len("\n\n".join([header, *lines, footer])) > self.EVENT_DESCRIPTION_LIMIT:
            lines.pop()
        return "\n\n".join([header, *lines, footer])[:self.EVENT_DESCRIPTION_LIMIT]

    async def ensure_calendar_event(self, guild):
        """Keep one event scheduled for tomorrow pointing at the calendar, or remove it."""
        row = self.conn.execute('SELECT guild_events, calendar, calendar_event_id FROM Settings WHERE guild_id=?',
                                (guild.id,)).fetchone()
        if not row or not row[0] or not row[1] or '/' not in str(row[1]):
            await self.remove_calendar_event(guild)
            return
        channel_id, msg_id = str(row[1]).split('/')
        location = f"https://discord.com/channels/{guild.id}/{channel_id}/{msg_id}"
        description = self.calendar_event_description(guild.id, channel_id, msg_id)
        now = datetime.now(timezone.utc)
        start_time = now + self.CALENDAR_EVENT_LEAD
        end_time = start_time + self.CALENDAR_EVENT_LENGTH
        event = None
        if row[2]:
            try:
                event = await guild.fetch_scheduled_event(row[2], with_counts=False)
            except discord.NotFound:
                event = None
            except discord.HTTPException:
                logger.warning("Could not fetch calendar event for guild %s", guild.id, exc_info=True)
                return
        try:
            if event is not None and event.status != discord.EventStatus.scheduled:
                if event.status == discord.EventStatus.active:
                    await event.delete()
                event = None
            if event is None:
                event = await guild.create_scheduled_event(
                    name=self.CALENDAR_EVENT_NAME, start_time=start_time, end_time=end_time,
                    entity_type=discord.EntityType.external, privacy_level=discord.PrivacyLevel.guild_only,
                    location=location, description=description)
                upsert(self.conn, 'Settings', ['calendar_event_id'], [event.id], ['guild_id'], [guild.id])
                self.conn.commit()
                return
            changes = {}
            if event.location != location:
                changes['location'] = location
            if event.description != description:
                changes['description'] = description
            if event.start_time - now < self.CALENDAR_EVENT_RENEW:
                changes['start_time'] = start_time
                changes['end_time'] = end_time
            if changes:
                await event.edit(**changes)
        except discord.Forbidden:
            logger.warning("Missing manage events permission for guild {0}".format(guild.id))
        except discord.HTTPException:
            logger.warning("Could not update calendar event for guild %s", guild.id, exc_info=True)

    async def remove_calendar_event(self, guild):
        event_id = select_one(self.conn, 'Settings', ['calendar_event_id'], ['guild_id'], [guild.id])
        if not event_id:
            return
        try:
            event = await guild.fetch_scheduled_event(event_id, with_counts=False)
            await event.delete()
        except discord.NotFound:
            pass
        except discord.HTTPException:
            logger.warning("Could not delete calendar event for guild %s", guild.id, exc_info=True)
            return
        upsert(self.conn, 'Settings', ['calendar_event_id'], [None], ['guild_id'], [guild.id])
        self.conn.commit()

    async def ensure_all_calendar_events(self):
        for guild in self.bot.guilds:
            await self.ensure_calendar_event(guild)
            await self.sync_live_events(guild)

    def _live_runs(self, guild_id, now):
        return self.conn.execute('''SELECT r.raid_id, r.channel_id, r.raid_id, r.name, r.tier, r.boss,
                r.time + COALESCE(e.duration, 7200) FROM Raids r LEFT JOIN SharedEvents e USING (raid_id)
            WHERE r.guild_id=? AND COALESCE(e.cancelled, 0)=0 AND r.time<=? AND r.time+COALESCE(e.duration, 7200)>?
            UNION ALL SELECT r.raid_id, c.thread_id, c.message_id, r.name, r.tier, r.boss, r.time+e.duration
            FROM EventCopies c JOIN Raids r USING (raid_id) JOIN SharedEvents e USING (raid_id)
            WHERE c.guild_id=? AND r.guild_id!=? AND e.cancelled=0 AND r.time<=? AND r.time+e.duration>?''',
            (guild_id, now, now, guild_id, guild_id, now, now)).fetchall()

    async def sync_live_events(self, guild):
        """Show a live Discord event only while a run on this server's calendar is happening."""
        now = datetime.now(timezone.utc)
        enabled = select_one(self.conn, 'Settings', ['guild_events'], ['guild_id'], [guild.id])
        live = {row[0]: row for row in self._live_runs(guild.id, int(now.timestamp()))} if enabled else {}
        tracked = dict(self.conn.execute('SELECT raid_id, event_id FROM LiveEvents WHERE guild_id=?', (guild.id,)).fetchall())
        for raid_id, event_id in tracked.items():
            if raid_id in live:
                continue
            try:
                event = await guild.fetch_scheduled_event(event_id, with_counts=False)
                await event.delete()
            except discord.NotFound:
                pass
            except discord.HTTPException:
                logger.warning("Could not remove live event %s", event_id, exc_info=True)
                continue
            self.conn.execute('DELETE FROM LiveEvents WHERE guild_id=? AND raid_id=?', (guild.id, raid_id))
        for raid_id, (_raid, channel_id, msg_id, name, tier, boss, end_ts) in live.items():
            if raid_id in tracked:
                continue
            try:
                event = await guild.create_scheduled_event(
                    name=" ".join(filter(None, [name, tier])), start_time=now + timedelta(minutes=1),
                    end_time=datetime.fromtimestamp(end_ts, tz=timezone.utc),
                    entity_type=discord.EntityType.external, privacy_level=discord.PrivacyLevel.guild_only,
                    location=f"https://discord.com/channels/{guild.id}/{channel_id}/{msg_id}",
                    description=boss or None)
                await event.start()
            except discord.Forbidden:
                logger.warning("Missing manage events permission for guild {0}".format(guild.id))
                break
            except discord.HTTPException:
                logger.warning("Could not create live event for raid %s", raid_id, exc_info=True)
                continue
            self.conn.execute('INSERT OR REPLACE INTO LiveEvents VALUES (?,?,?)', (guild.id, raid_id, event.id))
        self.conn.commit()

    async def remove_legacy_raid_events(self):
        """Delete per-raid scheduled events left by older versions."""
        rows = self.conn.execute('SELECT raid_id, guild_id, event_id FROM Raids WHERE event_id IS NOT NULL').fetchall()
        for raid_id, guild_id, event_id in rows:
            guild = self.bot.get_guild(guild_id)
            if guild:
                try:
                    event = await guild.fetch_scheduled_event(event_id, with_counts=False)
                    await event.delete()
                except discord.NotFound:
                    pass
                except discord.HTTPException:
                    logger.warning("Could not delete legacy event %s", event_id, exc_info=True)
                    continue
            self.conn.execute('UPDATE Raids SET event_id=NULL WHERE raid_id=?', (raid_id,))
        self.conn.commit()

    async def get_events(self):
        current_time = datetime.now().timestamp()
        if self.cached_events_at and self.cached_events_at + 86400 > current_time:
            return self.upcoming_events

        event_url = "https://www.lotro.com/news/lotro-public-event-schedule-en"
        async with self.bot.http_session.get(event_url) as resp:
            if not resp.ok:
                logger.warning("Could not connect to lotro.com")
                return self.upcoming_events
            text = await resp.text()

        stripped = re.sub('<[^<]+?>', '', text)
        stripped = stripped.replace('\xa0', '')
        stripped = stripped.replace('&gt;', '>')
        # Let us hope this questionable pattern is stable enough
        pattern = r'ALL TIMES EASTERN/SERVER TIME(.*)Ends \(Eastern Time\)(.*)Share On:'

        prog = re.compile(pattern, flags=re.DOTALL)
        result = prog.search(stripped)
        events_data = result.group(2).strip().splitlines() + ['']
        events = [chunk for chunk in chunks(events_data, 5)]
        parsed_events = [(event[0], self.parse_event_time(event[1]), self.parse_event_time(event[2])) for event in events]

        cutoff_unlock = current_time - 30 * 86400
        cutoff_past = current_time - 86400
        cutoff_future = current_time + 60 * 86400

        upcoming_events = []
        for event in parsed_events:
            start_time = current_time
            if event[1]:
                start_time = event[1]
            if event[2]:
                if cutoff_past < event[2] and start_time < cutoff_future:
                    upcoming_events.append(event)
            else:
                if cutoff_unlock < start_time < cutoff_future:
                    upcoming_events.append(event)
            if start_time > cutoff_future:
                break

        self.cached_events_at = current_time
        self.upcoming_events = upcoming_events
        return upcoming_events

    async def events_embed(self, guild_id):
        events = await self.get_events()
        events = events[:25]

        title = _("Upcoming events:")
        embed = discord.Embed(title=title, colour=discord.Colour(0x3498db))
        for e in events:
            time_str = ""
            if e[1] and e[2]:
                time_str = f"<t:{e[1]}> -- <t:{e[2]}>"
            else:
                if e[1]:
                    time_str = f"From <t:{e[1]}>"
                if e[2]:
                    time_str = f"At <t:{e[2]}>"
            embed.add_field(name=e[0], value=time_str, inline=False)
        embed.set_footer(text=_("Last updated"))
        embed.timestamp = datetime.fromtimestamp(self.cached_events_at)
        return embed

    def parse_event_time(self, time_string):
        if not time_string:
            return None
        time_string = time_string.casefold()
        time_string = time_string.replace(" eastern", "")
        time_string = time_string.replace("approximately ", "")
        time = dateparser.parse(time_string)
        try:
            time = pytz.timezone("America/New_York").localize(time).timestamp()
        except:
            logger.info(f"Calendar failed to parse: {time_string}")
            return None
        return int(time)

    @app_commands.command(name=_("events"), description=_("Shows upcoming official LotRO events in your local time."))
    @app_commands.guild_only()
    async def events_respond(self, interaction: discord.Interaction):
        await interaction.response.send_message(_("Waiting for lotro.com to respond..."))
        events = await self.events_embed(interaction.guild_id)
        await interaction.edit_original_response(content='', embed=events)


async def setup(bot):
    await bot.add_cog(CalendarCog(bot))
    logger.info("Loaded Calendar Cog.")
