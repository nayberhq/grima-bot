"""Creation panels, forum/voice lifecycle and cross-guild projections."""
import asyncio
import json
import logging
import re
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import dateparser
import discord
import emoji as Emoji
import pytz
from discord import app_commands
from discord.ext import commands, tasks

from database import upsert
from event_store import EventStore, forum_title, mirror_keys, extract_origin_badge, InvalidShare
from time_cog import Time, time_zone_autocomplete

with open('common_timezones.txt', 'r') as _zones:
    COMMON_TIMEZONES = _zones.read().splitlines()[:25]

log = logging.getLogger(__name__)
PRESETS = json.loads(Path(__file__).with_name('event_panels.json').read_text())
VOICE_LEAD = 3600
POLL_LEAD = 86400
POLL_TIERS = ('T1', 'T2', 'T2C', 'T3', 'T4', 'T5')
CONVERT_CUTOFF = 4 * 3600
CONVERT_LENGTH = 2 * 3600
DESCRIPTION_SEP = '----------------------------------------------'
EARLY_PRESETS = ('angmar', 'moria_mirkwood', 'isengard_rohan', 'helms_mordor', 'morgul_gundabad')
LATER_PRESETS = ('umbar_morgoth', 'seasonal', 'cluster', 'misc')


def time_entry_placeholder(bot, guild_id, user_id, example, keep_current=False):
    time_cog = bot.get_cog('TimeCog') if hasattr(bot, 'get_cog') else None
    hint = getattr(time_cog, 'time_entry_placeholder', None)
    if hint:
        return hint(guild_id, user_id, example, keep_current=keep_current)
    server = 'UTC'
    personal = None
    if time_cog and getattr(time_cog, 'get_server_timezone', None):
        server = time_cog.get_server_timezone(guild_id) or 'UTC'
    if time_cog and getattr(time_cog, 'get_personal_timezone', None):
        personal = time_cog.get_personal_timezone(user_id)
    you = personal or 'not set'
    lead = 'blank keeps current' if keep_current else example
    return f'{lead} · server {server} · you {you}'[:100]


def iter_templates(presets):
    for preset in presets:
        for index, button in enumerate(PRESETS[preset]['buttons']):
            if button.get('poll') or button.get('time_poll') or button.get('timezone') or 'name' not in button:
                continue
            yield preset, index, button


def poll_select_options():
    def options(presets):
        return [
            discord.SelectOption(label=button['label'][:100], value=f'{preset}:{index}', description=button['name'][:100])
            for preset, index, button in iter_templates(presets)
        ]
    return options(EARLY_PRESETS), options(LATER_PRESETS)


class ModalSelect(discord.ui.Select):
    """A menu inside a popup. Discord rejects a disabled field there."""

    def to_component_dict(self):
        payload = super().to_component_dict()
        payload.pop('disabled', None)
        return payload


async def acknowledge_selection(interaction):
    """Keep the private message in place while the leader picks from several menus."""
    if not interaction.response.is_done():
        await interaction.response.defer()


def tier_select(*, modal=False):
    kind = ModalSelect if modal else discord.ui.Select
    select = kind(
        placeholder='Tiers (optional)',
        min_values=0,
        max_values=len(POLL_TIERS),
        required=False,
        options=[
            discord.SelectOption(label='T2c' if tier == 'T2C' else tier, value=tier)
            for tier in POLL_TIERS
        ],
    )
    if not modal:
        select.callback = acknowledge_selection
    return select


def choice_label(choice):
    tier = (choice.get('tier') or '').strip()
    shown = 'T2c' if tier == 'T2C' else tier
    return f"{choice['name']} {shown}".strip()


def choices_from_values(values, tiers=None):
    """Catalog order, then T1 through T5, so a tie keeps the choice listed first."""
    wanted = set(values)
    selected = {str(tier).strip().upper() for tier in (tiers or [])}
    ordered = [tier for tier in POLL_TIERS if tier in selected] or ['']
    found = 0
    choices = []
    for preset, index, button in iter_templates(EARLY_PRESETS + LATER_PRESETS):
        if f'{preset}:{index}' not in wanted:
            continue
        found += 1
        for tier in ordered:
            choices.append({
                'name': button['name'],
                'size': int(button['size']),
                'creep': int(bool(button.get('creep'))),
                'tier': tier,
            })
    if found != len(wanted):
        raise ValueError('Choose events from the lists.')
    if not 2 <= len(choices) <= 25:
        raise ValueError('Choose between 2 and 25 choices.')
    return choices


def _clock_hour(hour, marker, fallback):
    if not 0 <= hour <= 23:
        raise ValueError('Use an hour from 0 to 23.')
    marker = (marker or fallback or '').lower()
    if marker == 'am':
        if hour == 12:
            return 0
        if hour > 12:
            raise ValueError('Use an hour from 1 to 12 with am or pm.')
        return hour
    if marker == 'pm':
        if hour == 12:
            return 12
        if not 1 <= hour <= 11:
            raise ValueError('Use an hour from 1 to 12 with am or pm.')
        return hour + 12
    if 1 <= hour <= 11:
        return hour + 12
    return hour


_TIMEZONE_NAMES = {
    'pst': 'America/Los_Angeles', 'pdt': 'America/Los_Angeles',
    'mst': 'America/Denver', 'mdt': 'America/Denver',
    'cst': 'America/Chicago', 'cdt': 'America/Chicago',
    'est': 'America/New_York', 'edt': 'America/New_York',
    'utc': 'UTC', 'gmt': 'UTC',
}
_TIMEFRAME_ERROR = 'A timeframe looks like 5 to 10 pm, or 5 to 10 pm PST.'


def _named_timezone(token):
    key = token.strip()
    try:
        return pytz.timezone(_TIMEZONE_NAMES.get(key.lower(), key))
    except pytz.UnknownTimeZoneError:
        raise ValueError('Could not read that timezone.') from None


def _parse_clock(text, tz):
    """One whole hour. A bare 1–11 means evening, matching the old hour box."""
    plain = re.fullmatch(r'\s*(\d{1,2})\s*', text)
    if plain:
        return _clock_hour(int(plain.group(1)), None, None)
    settings = {
        'TIMEZONE': getattr(tz, 'zone', 'UTC'),
        'RETURN_AS_TIMEZONE_AWARE': True,
        'PREFER_DATES_FROM': 'future',
        'RELATIVE_BASE': datetime.now(tz).replace(tzinfo=None),
    }
    parsed = dateparser.parse(text, settings=settings)
    if parsed is None:
        marked = re.fullmatch(r'\s*(\d{1,2})(?::00)?\s*(am|pm)\s*', text, re.I)
        if not marked:
            raise ValueError(_TIMEFRAME_ERROR)
        return _clock_hour(int(marked.group(1)), marked.group(2), None)
    if parsed.minute or parsed.second:
        raise ValueError('Use whole hours, like 5 to 10 pm.')
    return parsed.hour


def parse_timeframe(text, server_tz):
    """Inclusive hours and the zone they belong to. A trailing zone overrides the server."""
    body = text.strip()
    zone = re.search(r'(?i)\s+(pst|pdt|mst|mdt|cst|cdt|est|edt|utc|gmt|[A-Za-z]+/[A-Za-z0-9_+-]+)\s*$', body)
    tz = server_tz
    if zone:
        tz = _named_timezone(zone.group(1))
        body = body[:zone.start()].strip()
    match = re.fullmatch(r'(.+?)\s*(?:to|-)\s*(.+)', body, re.I)
    if not match:
        raise ValueError(_TIMEFRAME_ERROR)
    left, right = match.group(1).strip(), match.group(2).strip()
    right_marker = re.search(r'(?i)\b(am|pm)\b', right)
    left_marker = re.search(r'(?i)\b(am|pm)\b', left)
    if right_marker and not left_marker:
        left = f'{left} {right_marker.group(1)}'
    elif left_marker and not right_marker:
        right = f'{right} {left_marker.group(1)}'
    start, end = _parse_clock(left, tz), _parse_clock(right, tz)
    if end <= start:
        raise ValueError('The later hour has to be later on the same day.')
    return list(range(start, end + 1)), tz


def parse_hour_range(text):
    """Inclusive hours. Plain 1–11 means evening, so 5-10 is 5:00 PM through 10:00 PM."""
    return parse_timeframe(text, pytz.UTC)[0]


def parse_poll_day(text, tz):
    settings = {
        'PREFER_DATES_FROM': 'future',
        'TIMEZONE': getattr(tz, 'zone', str(tz)),
        'RETURN_AS_TIMEZONE_AWARE': True,
        'RELATIVE_BASE': datetime.now(tz).replace(tzinfo=None),
    }
    parsed = dateparser.parse(text, settings=settings)
    if parsed is None:
        raise ValueError('Could not read that day.')
    if parsed.tzinfo is None:
        parsed = tz.localize(parsed)
    return parsed.astimezone(tz).date()


def find_template(text):
    """One named event, or None when the text is the content poll."""
    query = text.strip().casefold()
    if not query:
        raise ValueError('Name one event, or poll.')
    if query == 'poll':
        return None
    matches = []
    for preset, index, button in iter_templates(EARLY_PRESETS + LATER_PRESETS):
        if query in (button['label'].casefold(), button['name'].casefold()):
            matches.append({'name': button['name'], 'size': int(button['size']), 'creep': int(bool(button.get('creep')))})
    if len(matches) != 1:
        raise ValueError('Name one event, or poll.')
    return matches[0]


def hour_choices(day, hours, tz):
    choices = []
    for hour in hours:
        local = tz.localize(datetime(day.year, day.month, day.day, hour))
        choices.append({
            'name': local.strftime('%I:%M %p').lstrip('0'),
            'size': 0,
            'creep': 0,
            'starts': int(local.timestamp()),
        })
    return choices


class EventsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.store = EventStore(bot.conn)
        self.locks = defaultdict(asyncio.Lock)
        self.delivery_lock = asyncio.Lock()
        self.watch_lock = asyncio.Lock()
        from durable_send import DurableSender
        self.sender=DurableSender(bot.conn,bot)

    @property
    def raids(self):
        return self.bot.get_cog('RaidCog')

    async def cog_load(self):
        for row in self.store.rows('SELECT * FROM CreationPanels'):
            if row['preset'] in PRESETS:
                self.bot.add_view(CreationView(self, row['preset']), message_id=row['message_id'])
        self.bot.loop.create_task(self._refresh_custom_panels_when_ready())
        for poll in self.store.open_polls():
            view = PollVoteView(self, poll['raid_id'])
            for copy in self.store.copies(poll['raid_id']):
                self.bot.add_view(view, message_id=copy['message_id'])
        self.lifecycle.start()

    async def cog_unload(self):
        self.lifecycle.cancel()

    async def _refresh_custom_panels_when_ready(self):
        await self.bot.wait_until_ready()
        await self.refresh_custom_panels()

    async def open_timezone_picker(self, interaction):
        time_cog = self.bot.get_cog('TimeCog')
        personal = time_cog.get_personal_timezone(interaction.user.id) if time_cog and hasattr(time_cog, 'get_personal_timezone') else None
        server = time_cog.get_server_timezone(interaction.guild_id) if time_cog else 'UTC'
        you = personal or 'not set'
        view = TimezonePicker(self, interaction.user.id, personal)
        await interaction.response.send_message(
            f'Personal timezone: {you}. Server timezone: {server}.',
            view=view, ephemeral=True)

    async def refresh_custom_panels(self):
        """Existing Custom cards keep their place, and gain buttons added after they were posted."""
        for row in self.store.rows("SELECT * FROM CreationPanels WHERE preset='misc'"):
            try:
                channel = await self.channel(row['channel_id'])
                await channel.get_partial_message(row['message_id']).edit(view=CreationView(self, 'misc'))
            except discord.HTTPException:
                log.exception('Could not refresh Custom panel %s', row['message_id'])

    async def channel(self, channel_id):
        return self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)

    def title(self, event, guild_id):
        timezone = self.bot.get_cog('TimeCog').get_server_timezone(guild_id)
        title = ' '.join(filter(None, [event['name'], event['tier']]))
        return forum_title(title, event['time'], timezone)

    async def can_manage(self, interaction, raid_id):
        event = self.store.event(raid_id)
        if not event:
            return False
        if interaction.user.id == event['organizer_id']:
            return True
        # A destination's administrator cannot acquire authority over the source raid.
        guild = self.bot.get_guild(event['guild_id'])
        if not guild:
            return False
        try:
            member = await self.raids.member_for_role_check(guild, interaction.user)
        except (discord.NotFound, discord.Forbidden):
            return False
        return self.raids.calendar_cog.is_raid_leader(member, guild)

    async def command_event(self, interaction, manage=True):
        raid_id = self.store.resolve(interaction.channel_id)
        event = self.store.event(raid_id)
        if not event:
            await interaction.response.send_message('Use this command inside an event forum post.', ephemeral=True)
            return None
        if manage and not await self.can_manage(interaction, raid_id):
            await interaction.response.send_message('Only the organizer or a source-server raid leader can do that.', ephemeral=True)
            return None
        return event

    async def validate_destinations(self, settings, require_voice=True, forum_id=None):
        forum = await self.channel(forum_id or settings['forum_id'])
        if require_voice and not settings['voice_category_id']:
            raise ValueError('Hosting events requires a voice category. Set it with /setup.')
        category = await self.channel(settings['voice_category_id']) if require_voice else None
        if not isinstance(forum, discord.ForumChannel) or (require_voice and not isinstance(category, discord.CategoryChannel)):
            raise ValueError('Configure an existing forum and voice category with /setup.')
        if forum.guild.id != settings['guild_id'] or (category and category.guild.id != settings['guild_id']):
            raise ValueError('Forum and voice category must belong to this server.')
        perms = forum.permissions_for(forum.guild.me)
        if not all((perms.view_channel, perms.send_messages, perms.send_messages_in_threads,
                    perms.embed_links, perms.read_message_history, perms.manage_threads)):
            raise ValueError('The bot needs View Channel, Send Messages, Send Messages in Threads, Embed Links, Read Message History and Manage Threads in the forum.')
        if category and not category.permissions_for(category.guild.me).manage_channels:
            raise ValueError('The bot needs Manage Channels in the voice category.')
        if forum.flags.require_tag:
            raise ValueError('Disable required tags on the event forum before using it.')
        return forum

    async def create_origin(self, guild_id, name, tier, timestamp, size=12, force_default_forum=False):
        settings = self.store.settings(guild_id)
        if not settings:
            return None
        # A poll's size is unknown until it converts, so it stays in the default raid forum.
        if force_default_forum:
            target_forum_id = settings['forum_id']
        else:
            target_forum_id = self.store.select_forum_id(settings, size=size, tier=tier)
        forum = await self.validate_destinations(
            settings, require_voice=bool(settings.get('voice_category_id')), forum_id=target_forum_id)
        event = dict(name=name, tier=tier, time=timestamp)
        result = await forum.create_thread(name=self.title(event, guild_id),
            content='Preparing event…', allowed_mentions=discord.AllowedMentions.none(),
            auto_archive_duration=10080, reason='Create raid forum')
        return result.thread, result.message, settings

    @staticmethod
    def _append_template_section(lines, template_text):
        lines.extend(['', DESCRIPTION_SEP, '', template_text.strip(), '', DESCRIPTION_SEP])

    def format_event_description(self, event, template_text=None, invite_url=None, code=None):
        header = f"{event['name']} {event['tier']}".strip() if event.get('tier') else event['name']
        lines = [header]
        if event.get('boss'):
            lines.append(event['boss'])
        template = (template_text or '').strip()
        if template:
            self._append_template_section(lines, template)
        else:
            lines.extend(['', DESCRIPTION_SEP])
        guild = self.bot.get_guild(event['guild_id'])
        kinship = guild.name if guild and getattr(guild, 'name', None) else 'host'
        lines.append('')
        lines.append(f'Not in the {kinship} discord? join here!')
        lines.append(invite_url if invite_url else 'Invite unavailable. Ask the organizer to set server_invite_url in /setup in the owning server.')
        if code:
            lines.append('')
            lines.append(f'mirrorkey:{code}')
        return '\n'.join(lines)

    async def resolve_template_from_link(self, guild, message_link: str) -> str:
        message_link = message_link.strip()
        m = re.search(r'discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)', message_link)
        if m:
            link_guild_id, link_channel_id, link_message_id = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if link_guild_id != guild.id:
                raise ValueError('The template message link must be from a channel in this server.')
            try:
                channel = await self.channel(link_channel_id)
                msg = await channel.fetch_message(link_message_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                raise ValueError(
                    'Could not fetch that message. Ensure the bot has View Channel and Read Message History in that channel.'
                ) from exc
            parts = []
            if getattr(msg, 'content', None) and msg.content.strip():
                parts.append(msg.content.strip())
            for emb in getattr(msg, 'embeds', None) or ():
                if getattr(emb, 'description', None) and emb.description.strip():
                    parts.append(emb.description.strip())
            body = '\n\n'.join(parts).strip()
            if not body:
                raise ValueError('The linked message has no text content to copy.')
        else:
            body = message_link
        if len(body) > 2500:
            raise ValueError('The template text must be 2500 characters or fewer.')
        return body

    def guild_description_template_body(self, guild_id):
        row = self.store.one("SELECT body FROM EventTemplates WHERE guild_id=? AND kind='description'", (guild_id,))
        if row:
            return row['body']
        legacy = self.store.one("SELECT body FROM EventTemplates WHERE guild_id=? AND kind='footer'", (guild_id,))
        return legacy['body'] if legacy else None

    async def persist_description_template(self, guild_id, body: str | None):
        """Save or clear the server event description template and refresh active events."""
        if body:
            self.store.write("DELETE FROM EventTemplates WHERE guild_id=? AND kind='footer'", (guild_id,))
            self.store.write(
                'INSERT OR REPLACE INTO EventTemplates VALUES (?,?,?)',
                (guild_id, 'description', body),
            )
        else:
            self.store.write(
                "DELETE FROM EventTemplates WHERE guild_id=? AND kind IN ('description', 'footer')",
                (guild_id,),
            )
        self.store.write(
            'UPDATE SharedEvents SET revision=revision+1 WHERE raid_id IN (SELECT raid_id FROM Raids WHERE guild_id=?)',
            (guild_id,),
        )
        for r in self.store.rows('SELECT raid_id FROM Raids WHERE guild_id=?', (guild_id,)):
            if self.store.event(r['raid_id']):
                await self.refresh(r['raid_id'])

    async def refresh(self, raid_id):
        async with self.locks[raid_id]:
            await self.refresh_locked(raid_id)

    async def refresh_locked(self, raid_id, now=None):
        event = self.store.event(raid_id)
        if not event:
            return
        from raid_cog import RaidView, CreepView
        if self.store.poll_open(raid_id):
            await self._refresh_poll_locked(event)
            return
        embed = self.raids.build_raid_message(raid_id,
            self.raids.build_raid_players(raid_id), self.raids.build_raid_players(raid_id, available=False))
        if not embed:
            return
        current = time.time() if now is None else now
        ended = self.store.ended(event, current)
        code = event['share_token'] or (None if ended else self.store.share(raid_id))
        if code and not event['share_token']:
            event['share_token'] = code
        embed.add_field(name='Ends', value=f"<t:{event['time'] + event['duration']}:F>", inline=False)
        embed.set_footer(text='Cancelled' if event['cancelled'] else ('Event ended' if ended else 'Shared roster • Sign up from any linked server'))
        roster_cards = []
        planning = self.bot.get_cog('PlanningCog')
        if planning:
            plan = planning.plan(raid_id)
            if plan:
                roster_cards, _ = planning.split_cards(event, plan)
        # Later fields (voice and invite links) are added after the description is sized.
        cards_room = sum(len(card) for card in roster_cards) + (300 if roster_cards else 0)
        for copy in self.store.copies(raid_id):
            if copy['locked'] or copy['revision'] == event['revision']:
                continue
            try:
                thread = await self.channel(copy['thread_id'])
                await thread.edit(name=self.title(event, copy['guild_id']), archived=False)
                local_embed = embed.copy()
                template_body = self.guild_description_template_body(event['guild_id'])
                invite_row = self.store.one('SELECT url FROM SourceInvites WHERE guild_id=?', (event['guild_id'],))
                invite_url = invite_row['url'] if invite_row else None
                budget = min(4096, 6000 - cards_room - len(local_embed) + (len(local_embed.description or '')))
                if template_body:
                    if planning:
                        try:
                            template_body = planning.render(event, template_body, budget)
                        except ValueError:
                            log.warning(
                                'Could not render event description template for guild %s; using raw template',
                                event['guild_id'],
                                exc_info=True,
                            )
                local_embed.description = self.format_event_description(
                    event,
                    template_text=template_body,
                    invite_url=invite_url,
                    code=code,
                )[:budget]
                owner = next((c for c in self.store.copies(raid_id) if c['guild_id']==event['guild_id']), None)
                if owner and owner['voice_id']:
                    local_embed.add_field(name='Event voice channel', value=f"https://discord.com/channels/{event['guild_id']}/{owner['voice_id']}", inline=False)
                elif not ended and owner and owner.get('voice_category_id'):
                    local_embed.add_field(name='Event voice channel', value='Created in the owning server when the event starts.', inline=False)
                if copy['guild_id'] != event['guild_id']:
                    local_embed.add_field(name='Join the event server',value=invite_url if invite_url else 'Invite unavailable. Ask the organizer to set server_invite_url in /setup in the owning server.',inline=False)
                show_poll = int(event['time']) - CONVERT_CUTOFF >= current
                view = None if ended else (
                    CreepView(self.raids) if event['creep'] else RaidView(self.raids, raid_id=raid_id, show_poll=show_poll))
                await thread.get_partial_message(copy['message_id']).edit(
                    content=None, embeds=[local_embed, *roster_cards], view=view, allowed_mentions=discord.AllowedMentions.none())
                if copy['voice_id'] and copy['guild_id']==event['guild_id']:
                    try:
                        voice = await self.channel(copy['voice_id'])
                    except discord.NotFound:
                        self.store.write('UPDATE EventCopies SET voice_id=NULL WHERE thread_id=?', (copy['thread_id'],))
                    else:
                        await voice.edit(name=self.title(event, copy['guild_id']))
                self.store.write('UPDATE EventCopies SET revision=?, ended_rendered=? WHERE thread_id=?',
                                 (event['revision'], int(ended), copy['thread_id']))
            except discord.HTTPException:
                log.exception('Could not refresh event %s in server %s; will retry', raid_id, copy['guild_id'])

    async def _refresh_poll_locked(self, event):
        raid_id = event['raid_id']
        poll = self.store.poll_row(raid_id) or {}
        counts = {row['position']: row['votes'] for row in self.store.rows(
            'SELECT position, COUNT(*) AS votes FROM PollVotes WHERE raid_id=? GROUP BY position', (raid_id,))}
        time_poll = poll.get('kind') == 'time'
        convert_poll = poll.get('kind') == 'convert'
        if time_poll:
            closes = int(poll['closes_at'])
            lines = [
                f"{counts.get(choice['position'], 0)} · {choice['name']} (<t:{int(choice['starts'])}:t>)"
                for choice in self.store.poll_choices(raid_id)
            ]
            description = f"Vote for a start time. The winner is chosen <t:{closes}:R>."
            footer = 'Time poll'
        elif convert_poll:
            closes = int(poll['closes_at'])
            lines = [
                f"{counts.get(choice['position'], 0)} · {choice_label(choice)} ({choice['size']}-player)"
                for choice in self.store.poll_choices(raid_id)
            ]
            description = (
                f"Vote for what this event should become. Voting closes <t:{closes}:R>.\n"
                "Anyone already signed up stays signed up."
            )
            footer = 'Event poll'
        else:
            closes = int(event['time']) - POLL_LEAD
            lines = [
                f"{counts.get(choice['position'], 0)} · {choice_label(choice)} ({choice['size']}-player)"
                for choice in self.store.poll_choices(raid_id)
            ]
            description = (
                f"Vote for any you want to run. The winner becomes the signup <t:{closes}:R>.\n"
                f"Starts <t:{int(event['time'])}:F>"
            )
            footer = 'Poll'
        embed = discord.Embed(
            title=f"Poll: {event['name']}",
            description=description,
            colour=discord.Colour(0x3498db),
        )
        packed = '\n'.join(lines) or 'No choices.'
        embed.add_field(name='Votes', value=packed[:1024], inline=False)
        if len(packed) > 1024:
            embed.add_field(name='\u200b', value=packed[1024:2048], inline=False)
        embed.set_footer(text=footer)
        view = PollVoteView(self, raid_id)
        for copy in self.store.copies(raid_id):
            if copy['locked'] or copy['revision'] == event['revision']:
                continue
            try:
                thread = await self.channel(copy['thread_id'])
                await thread.edit(name=self.title(event, copy['guild_id']), archived=False)
                await thread.get_partial_message(copy['message_id']).edit(
                    content=None, embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none())
                self.store.write('UPDATE EventCopies SET revision=? WHERE thread_id=?', (event['revision'], copy['thread_id']))
            except discord.HTTPException:
                log.exception('Could not refresh poll %s in server %s; will retry', raid_id, copy['guild_id'])

    def _cancel_poll(self, raid_id, reason):
        self.store.write('UPDATE Raids SET boss=? WHERE raid_id=?', (reason, raid_id))
        self.store.write(
            'UPDATE SharedEvents SET cancelled=1, share_token=NULL, revision=revision+1 WHERE raid_id=?',
            (raid_id,))
        self.store.write('UPDATE EventPolls SET resolved=1 WHERE raid_id=?', (raid_id,))

    def _schedule_event(self, event, name, size, creep, starts=None, tier=None):
        raid_id = event['raid_id']
        if tier is None:
            tier = event['tier'] or ''
        roster = int(bool(tier and tier[1:2].isdigit() and int(tier[1]) > 2) and not creep)
        columns = ['name=?', 'size=?', 'roster=?', 'tier=?']
        values = [name, int(size), roster, tier]
        if starts is not None:
            columns.append('time=?')
            values.append(int(starts))
            self.store.write('UPDATE SharedEvents SET start_pinged=0 WHERE raid_id=?', (raid_id,))
        values.append(raid_id)
        self.store.write(f"UPDATE Raids SET {', '.join(columns)} WHERE raid_id=?", tuple(values))
        self.store.write(
            'UPDATE SharedEvents SET creep=?, revision=revision+1 WHERE raid_id=?',
            (int(creep), raid_id))
        if not creep and self.raids:
            self.raids.roster_init(raid_id, int(size))

    async def _refresh_calendar(self, event):
        if event:
            await self.refresh_calendars_for_raid(event['raid_id'])

    async def resolve_poll_locked(self, raid_id):
        """Turn the winning choice into this thread's signup, or cancel when nobody voted."""
        if not self.store.poll_open(raid_id):
            return
        event = self.store.event(raid_id)
        winner = self.store.winning_choice(raid_id)
        poll = self.store.poll_row(raid_id)
        if winner is None:
            if poll and poll['kind'] == 'convert':
                self.store.write('UPDATE EventPolls SET resolved=1 WHERE raid_id=?', (raid_id,))
                self.store.write('UPDATE SharedEvents SET revision=revision+1 WHERE raid_id=?', (raid_id,))
            else:
                self._cancel_poll(raid_id, 'This poll closed with no votes, so no event was scheduled.')
        else:
            voters = self.store.poll_voter_ids(raid_id)
            self._schedule_event(
                event, winner['name'], winner['size'], winner['creep'], tier=winner.get('tier') or '')
            self.store.write('UPDATE EventPolls SET resolved=1 WHERE raid_id=?', (raid_id,))
            await self._ping_poll_change(raid_id, voters)
        await self._refresh_calendar(event)

    async def resolve_time_poll_locked(self, raid_id):
        """Lock the winning hour. A named template becomes the event; a poll becomes a content poll."""
        if not self.store.poll_open(raid_id):
            return
        event = self.store.event(raid_id)
        poll = self.store.poll_row(raid_id)
        winner = self.store.winning_choice(raid_id)
        if winner is None or not winner.get('starts'):
            self._cancel_poll(raid_id, 'This time poll closed with no votes, so no event was scheduled.')
            await self._refresh_calendar(event)
            return
        voters = self.store.poll_voter_ids(raid_id)
        if self.store.held_choices(raid_id):
            self.store.write('UPDATE Raids SET time=? WHERE raid_id=?', (int(winner['starts']), raid_id))
            self.store.write('UPDATE SharedEvents SET start_pinged=0 WHERE raid_id=?', (raid_id,))
            self.store.adopt_held_choices(raid_id)
            self.store.write(
                "UPDATE EventPolls SET kind='content', announced=0, resolved=0, closes_at=NULL WHERE raid_id=?",
                (raid_id,))
            self.store.write('UPDATE SharedEvents SET revision=revision+1 WHERE raid_id=?', (raid_id,))
            updated = self.store.event(raid_id)
            await self.ping_users(
                raid_id, voters,
                f"You voted in the last poll. That poll has become this poll. "
                f"The time is <t:{int(updated['time'])}:F>. Please vote.")
        else:
            self._schedule_event(
                event, poll['template_name'], poll['template_size'], poll['template_creep'], starts=winner['starts'])
            self.store.write('UPDATE EventPolls SET resolved=1 WHERE raid_id=?', (raid_id,))
            await self._ping_poll_change(raid_id, voters)
        await self._refresh_calendar(event)

    def format_poll_announcement(self, event):
        guild = self.bot.get_guild(event['guild_id'])
        kinship = guild.name if guild and getattr(guild, 'name', None) else 'host'
        title = f"{event['name']} {event['tier']}".strip() if event.get('tier') else event['name']
        poll = self.store.poll_row(event['raid_id'])
        if poll and poll['kind'] == 'time':
            choices = self.store.poll_choices(event['raid_id'])
            first, last = int(choices[0]['starts']), int(choices[-1]['starts'])
            when = f"on <t:{first}:D>, <t:{first}:t>–<t:{last}:t>"
        else:
            when = f"at <t:{int(event['time'])}:F>"
        return '\n'.join([
            f'{kinship} posted a raid poll.',
            f'{title} {when}',
            f"Vote in <#{event['channel_id']}>",
        ])

    async def deliver_poll_announcements(self):
        """Tell the announcement channel a poll exists, without a mirror code."""
        for poll in self.store.rows('SELECT raid_id FROM EventPolls WHERE resolved=0 AND announced=0'):
            event = self.store.event(poll['raid_id'])
            if not event or self.store.ended(event, time.time()):
                continue
            row = self.store.one('SELECT push_id FROM NetworkSettings WHERE guild_id=?', (event['guild_id'],))
            if not row or not row['push_id']:
                continue
            try:
                channel = await self.channel(row['push_id'])
                kind = (self.store.poll_row(poll['raid_id']) or {}).get('kind') or 'content'
                await self.sender.send(
                    channel, f"poll:{poll['raid_id']}:{row['push_id']}:{kind}", self.format_poll_announcement(event))
                self.store.write('UPDATE EventPolls SET announced=1 WHERE raid_id=?', (poll['raid_id'],))
            except discord.HTTPException:
                log.exception('Poll announcement failed for %s; will retry', poll['raid_id'])

    async def create_poll(self, guild_id, author_id, title, tier, timestamp, choices):
        if not time.time() + POLL_LEAD < timestamp <= time.time() + 31536000:
            raise ValueError('Polls must start more than 24 hours from now, and within one year.')
        if not 2 <= len(choices) <= 25:
            raise ValueError('Choose between 2 and 25 events.')
        origin = await self.create_origin(guild_id, title, '', timestamp, size=12, force_default_forum=True)
        if origin is None:
            raise ValueError('Run /setup before creating a poll.')
        thread, post, settings = origin
        raid_id = post.id
        upsert(self.bot.conn, 'Raids',
               ['channel_id', 'guild_id', 'organizer_id', 'name', 'tier', 'boss', 'time', 'roster', 'tag', 'size'],
               [thread.id, guild_id, author_id, title[:256], tier, '', timestamp, 0, None, 12],
               ['raid_id'], [raid_id])
        self.bot.conn.commit()
        self.store.add_event(raid_id, settings)
        self.store.add_copy(raid_id, guild_id, thread.id, post.id, settings['voice_category_id'])
        self.store.add_poll(raid_id, choices)
        await self.refresh(raid_id)
        await self.deliver_poll_announcements()
        if self.raids and self.raids.calendar_cog:
            await self.raids.calendar_cog.update_calendar(guild_id)
        return raid_id

    def server_timezone(self, guild_id):
        time_cog = self.bot.get_cog('TimeCog') if hasattr(self.bot, 'get_cog') else None
        name = time_cog.get_server_timezone(guild_id) if time_cog else None
        name = name or getattr(self.bot, 'server_tz', None) or 'UTC'
        try:
            return pytz.timezone(name)
        except pytz.UnknownTimeZoneError:
            return pytz.utc

    async def create_time_poll(self, guild_id, author_id, title, hours, template=None, held=None):
        earliest = min(choice['starts'] for choice in hours)
        lead = POLL_LEAD * (2 if held else 1)
        if not time.time() + lead < earliest <= time.time() + 31536000:
            if held:
                raise ValueError('A time poll that becomes a content poll needs its earliest hour more than 48 hours away.')
            raise ValueError('The earliest hour must be more than 24 hours away, and within one year.')
        origin = await self.create_origin(guild_id, title, '', earliest, size=12, force_default_forum=True)
        if origin is None:
            raise ValueError('Run /setup before creating a poll.')
        thread, post, settings = origin
        raid_id = post.id
        upsert(self.bot.conn, 'Raids',
               ['channel_id', 'guild_id', 'organizer_id', 'name', 'tier', 'boss', 'time', 'roster', 'tag', 'size'],
               [thread.id, guild_id, author_id, title[:256], '', '', earliest, 0, None, 12],
               ['raid_id'], [raid_id])
        self.bot.conn.commit()
        self.store.add_event(raid_id, settings)
        self.store.add_copy(raid_id, guild_id, thread.id, post.id, settings['voice_category_id'])
        self.store.add_poll(
            raid_id, hours, kind='time', closes_at=int(time.time()) + POLL_LEAD,
            template=template, held=held)
        await self.refresh(raid_id)
        await self.deliver_poll_announcements()
        if self.raids and self.raids.calendar_cog:
            await self.raids.calendar_cog.update_calendar(guild_id)
        return raid_id

    async def convert_to_poll(self, raid_id, choices):
        """Turn an existing event into a two-hour poll. Current signups stay signed up."""
        event = self.store.event(raid_id)
        if not event or self.store.ended(event, time.time()):
            raise ValueError('This event has ended.')
        if self.store.poll_open(raid_id):
            raise ValueError('This event is already a poll.')
        if time.time() > int(event['time']) - CONVERT_CUTOFF:
            raise ValueError('It is too late to change this event.')
        if not 2 <= len(choices) <= 25:
            raise ValueError('Choose between 2 and 25 events.')
        if self.store.poll_row(raid_id):
            self.store.write('DELETE FROM PollVotes WHERE raid_id=?', (raid_id,))
            self.store.write('DELETE FROM PollChoices WHERE raid_id=?', (raid_id,))
            self.store.write('DELETE FROM HeldPollChoices WHERE raid_id=?', (raid_id,))
            self.store.write('DELETE FROM EventPolls WHERE raid_id=?', (raid_id,))
        closes = int(time.time()) + CONVERT_LENGTH
        self.store.add_poll(raid_id, choices, kind='convert', closes_at=closes)
        self.store.write('UPDATE EventPolls SET announced=1 WHERE raid_id=?', (raid_id,))
        self.store.touch(raid_id)
        await self.refresh(raid_id)
        await self.ping_signups(
            raid_id, f"This event may change. Vote on the post for what to run. Poll closes <t:{int(closes)}:R>.")
        return closes

    async def _ping_poll_change(self, raid_id, voters):
        event = self.store.event(raid_id)
        if not event:
            return
        title = f"{event['name']} {event.get('tier') or ''}".strip()
        await self.ping_users(
            raid_id, voters,
            f"You voted in the last poll. That poll has become this event, {title}, "
            f"<t:{int(event['time'])}:F>. Please sign up.")

    async def ping_signups(self, raid_id, notice):
        players = self.store.rows(
            'SELECT player_id FROM Players WHERE raid_id=? AND unavailable=0 AND player_id>0', (raid_id,))
        return await self.ping_users(raid_id, [row['player_id'] for row in players], notice)

    async def ping_users(self, raid_id, user_ids, notice):
        seen = []
        for user_id in user_ids:
            user_id = int(user_id)
            if user_id > 0 and user_id not in seen:
                seen.append(user_id)
        mentions = ' '.join(f"<@{user_id}>" for user_id in seen)
        if not mentions:
            return False
        text = f"{mentions}\n{notice}"
        for copy in self.store.copies(raid_id):
            if copy['locked']:
                continue
            try:
                thread = await self.channel(copy['thread_id'])
                await thread.send(text, allowed_mentions=discord.AllowedMentions(users=True))
            except discord.HTTPException:
                log.exception('Could not ping signups for %s in %s', raid_id, copy['guild_id'])
        return True

    async def cancel(self, raid_id):
        self.store.write('UPDATE SharedEvents SET cancelled=1, share_token=NULL, revision=revision+1 WHERE raid_id=?', (raid_id,))
        await self.reconcile(raid_id)
        await self.refresh_calendars_for_raid(raid_id)

    async def refresh_calendars_for_raid(self, raid_id):
        if not self.raids or not self.raids.calendar_cog:
            return
        event = self.store.event(raid_id)
        guild_ids = set()
        if event:
            guild_ids.add(event['guild_id'])
        for copy in self.store.copies(raid_id):
            guild_ids.add(copy['guild_id'])
        for guild_id in guild_ids:
            await self.raids.calendar_cog.update_calendar(guild_id)

    async def reconcile(self, raid_id, now=None):
        now = time.time() if now is None else now
        async with self.locks[raid_id]:
            event = self.store.event(raid_id)
            if not event:
                return
            poll = self.store.poll_row(raid_id) if self.store.poll_open(raid_id) else None
            if poll and poll['kind'] == 'time' and poll.get('closes_at') and now >= poll['closes_at']:
                await self.resolve_time_poll_locked(raid_id)
                event = self.store.event(raid_id)
            elif poll and poll['kind'] == 'convert' and poll.get('closes_at') and now >= poll['closes_at']:
                await self.resolve_poll_locked(raid_id)
                event = self.store.event(raid_id)
            elif poll and poll['kind'] == 'content' and now >= int(event['time']) - POLL_LEAD:
                await self.resolve_poll_locked(raid_id)
                event = self.store.event(raid_id)
            elif (not poll and not event['cancelled'] and not event.get('convert_closed')
                    and now > int(event['time']) - CONVERT_CUTOFF):
                self.store.write(
                    'UPDATE SharedEvents SET convert_closed=1, revision=revision+1 WHERE raid_id=?', (raid_id,))
                event = self.store.event(raid_id)
            ended = self.store.ended(event, now)
            drop_from_calendar = ended and any(
                not copy['ended_rendered'] for copy in self.store.copies(raid_id))
            if (not ended and not event['cancelled'] and not self.store.poll_open(raid_id)
                    and not event.get('start_pinged') and now >= int(event['time']) - VOICE_LEAD):
                title = f"{event['name']} {event.get('tier') or ''}".strip()
                if await self.ping_signups(raid_id, f"Starting <t:{int(event['time'])}:R>. {title}."):
                    self.store.write('UPDATE SharedEvents SET start_pinged=1 WHERE raid_id=?', (raid_id,))
            if not ended:
                if not self.store.one('SELECT 1 FROM SourceInvites WHERE guild_id=?',(event['guild_id'],)):
                    try:
                        settings=self.store.settings(event['guild_id'])
                        forum=await self.channel(settings['forum_id'])
                        invite=await forum.create_invite(max_age=0,max_uses=0,unique=False,reason='Join the owning server for shared events')
                        self.store.write('INSERT OR REPLACE INTO SourceInvites VALUES (?,?)',(event['guild_id'],str(invite.url)))
                        self.store.touch(raid_id)
                    except discord.HTTPException:
                        log.warning('Cannot create owner-server invite for event %s; set server_invite_url in /setup or grant Create Invite',raid_id)
            # Update the final embed before locking; the renderer removes controls.
            if ended:
                for copy in self.store.copies(raid_id):
                    if not copy['locked'] and not copy['ended_rendered']:
                        self.store.write('UPDATE EventCopies SET revision=0 WHERE thread_id=?', (copy['thread_id'],))
                await self.refresh_locked(raid_id, now)
            for copy in self.store.copies(raid_id):
                try:
                    guild = self.bot.get_guild(copy['guild_id'])
                    if not guild:
                        continue
                    if not ended and not copy['locked']:
                        thread = await self.channel(copy['thread_id'])
                        if thread.archived and not thread.locked:
                            await thread.edit(archived=False)
                    voice_open = event['time'] - VOICE_LEAD
                    if (not self.store.poll_open(raid_id) and copy['guild_id']==event['guild_id']
                            and copy.get('voice_category_id')
                            and not ended and not copy['locked'] and now >= voice_open and not copy['voice_id']):
                        category = await self.channel(copy['voice_category_id'])
                        # A deterministic marker recovers a successful Discord creation
                        # if the process died before saving its ID.
                        marker = f'event-{raid_id}'
                        voice = discord.utils.get(category.voice_channels, name=marker)
                        if voice is None:
                            voice = await guild.create_voice_channel(marker, category=category,
                                reason=f'Raid {raid_id} starts within the hour')
                        self.store.write('UPDATE EventCopies SET voice_id=?, revision=0 WHERE thread_id=?',
                                         (voice.id, copy['thread_id']))
                        copy['voice_id'] = voice.id
                        self.store.touch(raid_id)
                    if copy['voice_id'] and (copy['guild_id']!=event['guild_id'] or (not ended and now < voice_open)):
                        # A running event may be rescheduled; retire the early channel
                        # when empty, and recreate it at the new start.
                        try:
                            voice = await self.channel(copy['voice_id'])
                        except discord.NotFound:
                            voice = None
                        if voice is None or not voice.members:
                            if voice:
                                await voice.delete(reason='Voice belongs only to the owning event server, or event was rescheduled')
                            self.store.write('UPDATE EventCopies SET voice_id=NULL, revision=0 WHERE thread_id=?', (copy['thread_id'],))
                            self.store.touch(raid_id)
                            copy['voice_id']=None
                    if ended:
                        if copy['voice_id']:
                            try:
                                voice = await self.channel(copy['voice_id'])
                            except discord.NotFound:
                                voice = None
                            if voice is None or not voice.members:
                                if voice:
                                    await voice.delete(reason='Event ended and voice channel is empty')
                                self.store.write('UPDATE EventCopies SET voice_id=NULL, revision=0 WHERE thread_id=?',
                                                 (copy['thread_id'],))
                                self.store.touch(raid_id)
                    if self.store.lock_due(event, now) and not copy['locked']:
                        thread = await self.channel(copy['thread_id'])
                        await thread.edit(locked=True, archived=True, reason='Event finished')
                        self.store.write('UPDATE EventCopies SET locked=1 WHERE thread_id=?', (copy['thread_id'],))
                except discord.HTTPException:
                    log.exception('Event lifecycle failed for %s/%s; will retry', raid_id, copy['guild_id'])
            await self.refresh_locked(raid_id, now)
            if drop_from_calendar:
                await self.refresh_calendars_for_raid(raid_id)

    async def sync_event(self, guild_id, code, origin_message=None):
        """One entry point for manual sync and watcher delivery. Idempotent per guild."""
        event = self.store.by_token(code.strip())
        if event and origin_message is not None:
            kin_name, emoji_str = extract_origin_badge(origin_message)
            if kin_name or emoji_str:
                self.store.set_guild_identity(event['guild_id'], kin_name=kin_name, emoji=emoji_str)
                await self.raids.calendar_cog.update_calendar(event['guild_id'])
        settings = self.store.settings(guild_id)
        if event and self.store.one('SELECT 1 FROM EventBlocks WHERE raid_id=? AND guild_id=?', (event['raid_id'],guild_id)):
            raise InvalidShare('This server was disconnected by the organizer.')
        if not event or self.store.ended(event, time.time()):
            raise InvalidShare('Invalid sharing code or the event has ended.')
        if self.store.poll_open(event['raid_id']):
            raise InvalidShare('This poll is not shared until it becomes an event.')
        if not settings:
            raise ValueError('Run /setup in the destination server first.')
        raid_id = event['raid_id']
        async with self.locks[raid_id]:
            event = self.store.by_token(code.strip())
            if not event or self.store.ended(event, time.time()):
                raise InvalidShare('This sharing code is no longer active.')
            existing = self.store.one('SELECT * FROM EventCopies WHERE raid_id=? AND guild_id=?', (raid_id, guild_id))
            if existing:
                try:
                    thread = await self.channel(existing['thread_id'])
                    await thread.fetch_message(existing['message_id'])
                    await thread.edit(locked=False,archived=False)
                    self.store.write('UPDATE EventCopies SET revision=0,locked=0 WHERE raid_id=? AND guild_id=?', (raid_id,guild_id))
                    await self.refresh_locked(raid_id)
                    return existing['thread_id'], False
                except discord.NotFound:
                    # Preserve the canonical raid and any surviving voice channel.
                    pass
            target_forum_id = self.store.select_forum_id(settings, size=event.get('size') or 12, tier=event.get('tier'))
            forum = await self.validate_destinations(settings, require_voice=False, forum_id=target_forum_id)
            result = await forum.create_thread(name=self.title(event, guild_id), content='Preparing shared raid…',
                auto_archive_duration=10080, allowed_mentions=discord.AllowedMentions.none())
            if existing:
                self.store.write('UPDATE EventCopies SET thread_id=?,message_id=?,revision=0,locked=0 WHERE raid_id=? AND guild_id=?', (result.thread.id,result.message.id,raid_id,guild_id))
                if guild_id == event['guild_id']:
                    self.store.write('UPDATE Raids SET channel_id=? WHERE raid_id=?', (result.thread.id,raid_id))
            else:
                self.store.add_copy(raid_id, guild_id, result.thread.id, result.message.id, settings['voice_category_id'])
            await self.refresh_locked(raid_id)
        await self.reconcile(raid_id)
        await self.raids.calendar_cog.update_calendar(guild_id)
        return result.thread.id, True

    async def deliver_watched_code(self, guild_id, code, origin_message=None):
        """Watch-list delivery. Sync creates a raid post; calendar-only only lists the event."""
        if self.store.watch_sync_enabled(guild_id):
            result = await self.sync_event(guild_id, code)
            event = self.store.by_token(code.strip())
            if event:
                self.store.write('DELETE FROM WatchCalendar WHERE guild_id=? AND raid_id=?', (guild_id, event['raid_id']))
            return result
        event = self.store.by_token(code.strip())
        if event and origin_message is not None:
            kin_name, emoji_str, icon_url = extract_origin_badge(origin_message, self.bot)
            if kin_name or emoji_str:
                await self.ensure_origin_emoji(event['guild_id'], kin_name, emoji_str, icon_url)
        if event and self.store.one('SELECT 1 FROM EventBlocks WHERE raid_id=? AND guild_id=?', (event['raid_id'], guild_id)):
            raise InvalidShare('This server was disconnected by the organizer.')
        if not event or self.store.ended(event, time.time()):
            raise InvalidShare('Invalid sharing code or the event has ended.')
        self.store.write('INSERT OR IGNORE INTO WatchCalendar (guild_id, raid_id) VALUES (?,?)', (guild_id, event['raid_id']))
        if self.raids and self.raids.calendar_cog:
            await self.raids.calendar_cog.update_calendar(guild_id)
        return None, False

    async def materialize_watch_calendar(self, guild_id):
        """Create raid posts for events that were calendar-only while sync was off."""
        created = 0
        failed = []
        for row in self.store.rows('SELECT raid_id FROM WatchCalendar WHERE guild_id=?', (guild_id,)):
            event = self.store.event(row['raid_id'])
            token = event.get('share_token') if event else None
            if not token:
                failed.append(f"raid {row['raid_id']} has no sharing code")
                continue
            try:
                await self.sync_event(guild_id, token)
            except (InvalidShare, ValueError, discord.HTTPException) as exc:
                failed.append(str(exc))
                continue
            self.store.write('DELETE FROM WatchCalendar WHERE guild_id=? AND raid_id=?', (guild_id, row['raid_id']))
            created += 1
        return created, failed

    async def ensure_origin_emoji(self, guild_id, kin_name, emoji_str, icon_url):
        if emoji_str:
            m = re.search(r':(\d+)>$', emoji_str)
            if m and hasattr(self.bot, 'get_emoji'):
                found = self.bot.get_emoji(int(m.group(1)))
                if found:
                    emoji_str = str(found)
                elif hasattr(self.bot, 'fetch_application_emojis'):
                    slug = re.sub(r'[^a-z0-9]+', '_', (kin_name or 'kin').lower()).strip('_') or 'kin'
                    prefix = f"net_{slug[:21].rstrip('_')}_"
                    app_name = f"net_{slug[:21].rstrip('_')}_{m.group(1)[-6:]}"[:32]
                    try:
                        app_emojis = await self.bot.fetch_application_emojis()
                        existing = next((e for e in app_emojis if e.name == app_name or e.name.startswith(prefix)), None)
                        if not existing and icon_url and hasattr(self.bot, 'create_application_emoji') and hasattr(self.bot, 'http_session'):
                            async with self.bot.http_session.get(icon_url) as resp:
                                if resp.ok:
                                    existing = await self.bot.create_application_emoji(name=app_name, image=await resp.read())
                        if existing:
                            emoji_str = str(existing)
                    except Exception:
                        pass
        self.store.set_guild_identity(guild_id, kin_name=kin_name, emoji=emoji_str)
        await self.raids.calendar_cog.update_calendar(guild_id)

    async def scan_watches(self):
        # A durable cursor also catches messages sent while the process was offline.
        async with self.watch_lock:
            for watch in self.store.rows('SELECT * FROM EventWatches'):
                try:
                    channel = await self.channel(watch['source_channel_id'])
                    async for message in channel.history(limit=100, oldest_first=True,
                            after=discord.Object(id=watch['last_message_id'])):
                        # Announcement drafts are not discovery authorization until manually published.
                        if isinstance(channel,discord.TextChannel) and channel.is_news() and not message.flags.crossposted:
                            if mirror_keys(message):
                                self.store.write('INSERT OR IGNORE INTO PendingPublications VALUES (?,?,?)',(watch['guild_id'],watch['source_channel_id'],message.id))
                            self.store.write('UPDATE EventWatches SET last_message_id=? WHERE guild_id=? AND source_channel_id=?',(message.id,watch['guild_id'],watch['source_channel_id']))
                            continue
                        # Parse both literal message text and embedded text/fields.
                        kin_name, emoji_str, icon_url = extract_origin_badge(message, self.bot)
                        for code in mirror_keys(message):
                            if kin_name or emoji_str:
                                ev = self.store.by_token(code)
                                if ev:
                                    await self.ensure_origin_emoji(ev['guild_id'], kin_name, emoji_str, icon_url)
                            try:
                                await self.deliver_watched_code(watch['guild_id'], code, message)
                            except InvalidShare:
                                # Invalid/expired markers are permanent failures, not a backlog blocker.
                                log.info('Ignored invalid mirrorkey in message %s', message.id)
                            except (ValueError, discord.HTTPException) as exc:
                                self.store.write('INSERT OR REPLACE INTO WatchFailures VALUES (?,?,?,?,?)', (watch['guild_id'],watch['source_channel_id'],message.id,code,str(exc)))
                        self.store.write('''UPDATE EventWatches SET last_message_id=?
                            WHERE guild_id=? AND source_channel_id=?''',
                            (message.id, watch['guild_id'], watch['source_channel_id']))
                except discord.NotFound:
                    self.store.write('DELETE FROM EventWatches WHERE guild_id=? AND source_channel_id=?',
                                     (watch['guild_id'], watch['source_channel_id']))
                except discord.HTTPException:
                    # Do not advance past a transient failure; already-created copies deduplicate.
                    log.exception('Watcher failed for server %s channel %s; will retry',
                                  watch['guild_id'], watch['source_channel_id'])

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.guild and self.store.one('SELECT 1 FROM EventWatches WHERE source_channel_id=?', (message.channel.id,)):
            await self.scan_watches()

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent):
        if self.store.one('SELECT 1 FROM EventWatches WHERE source_channel_id=?', (payload.channel_id,)):
            try:
                channel = await self.channel(payload.channel_id)
                message = await channel.fetch_message(payload.message_id)
            except discord.HTTPException:
                return
            codes = mirror_keys(message)
            if not codes:
                return
            kin_name, emoji_str, icon_url = extract_origin_badge(message, self.bot)
            for watch in self.store.rows('SELECT * FROM EventWatches WHERE source_channel_id=?', (payload.channel_id,)):
                for code in codes:
                    if kin_name or emoji_str:
                        ev = self.store.by_token(code)
                        if ev:
                            await self.ensure_origin_emoji(ev['guild_id'], kin_name, emoji_str, icon_url)
                    try:
                        await self.deliver_watched_code(watch['guild_id'], code, message)
                    except InvalidShare:
                        pass
                    except (ValueError, discord.HTTPException) as exc:
                        self.store.write('INSERT OR REPLACE INTO WatchFailures VALUES (?,?,?,?,?)',
                                         (watch['guild_id'], watch['source_channel_id'], message.id, code, str(exc)))

    async def deliver_announcements(self):
        async with self.delivery_lock:
            await self._deliver_announcements()

    async def _deliver_announcements(self):
        for delivery in self.store.pending_announcements():
            try:
                thread = await self.channel(delivery['thread_id'])
                # Do not resurrect finished threads to retry a failed announcement.
                if thread.locked:
                    continue
                if thread.archived:
                    await thread.edit(archived=False)
                await self.sender.send(thread,f"announcement:{delivery['id']}:{delivery['thread_id']}",f"**Announcement from <@{delivery['author_id']}>**\n{delivery['body']}")
                self.store.write('UPDATE AnnouncementDeliveries SET sent=1 WHERE announcement_id=? AND thread_id=?',
                                 (delivery['id'], delivery['thread_id']))
            except discord.HTTPException:
                log.exception('Announcement %s failed in %s; will retry', delivery['id'], delivery['thread_id'])

    @tasks.loop(seconds=30)
    async def lifecycle(self):
        for row in self.store.active_events():
            try:
                await self.reconcile(row['raid_id'])
            except Exception:
                log.exception('Event reconciliation failed; continuing other events')
        await self.deliver_announcements()
        await self.deliver_poll_announcements()
        await self.scan_watches()
        network = self.bot.get_cog('NetworkCog')
        if network:
            await network.tick()

    @lifecycle.before_loop
    async def before_lifecycle(self):
        await self.bot.wait_until_ready()

    @staticmethod
    def _missing_perms(perms, required):
        return [label for attr, label in required if not getattr(perms, attr, False)]

    async def _full_channel(self, guild, channel):
        """Select menus return a partial channel. Load the real one before type and permission checks."""
        if isinstance(channel, (discord.abc.GuildChannel, discord.Thread)):
            return channel
        mention = getattr(channel, 'mention', None) or f"<#{getattr(channel, 'id', '?')}>"
        found = None
        resolver = getattr(channel, 'resolve', None)
        if resolver:
            try:
                found = resolver()
            except Exception:
                found = None
        channel_id = getattr(channel, 'id', None)
        if found is None and channel_id and hasattr(guild, 'get_channel_or_thread'):
            found = guild.get_channel_or_thread(channel_id)
        if found is None and channel_id and hasattr(guild, 'get_channel'):
            found = guild.get_channel(channel_id)
        if found is None and channel_id:
            try:
                fetch = getattr(channel, 'fetch', None)
                found = await fetch() if fetch else await self.bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                raise ValueError(f'**{mention}:** the bot cannot see that channel ({type(exc).__name__}: {exc}).') from exc
            except Exception as exc:
                raise ValueError(f'**{mention}:** {type(exc).__name__}: {exc}') from exc
        if found is None:
            raise ValueError(f'**{mention}:** that channel could not be loaded.')
        gid = getattr(getattr(found, 'guild', None), 'id', None) or getattr(found, 'guild_id', None)
        if gid is not None and gid != guild.id:
            raise ValueError(f'**{mention}:** that channel is not in this server.')
        return found

    async def probe_setup_channel(self, guild, channel, kind) -> tuple[list[str], list[str]]:
        """Check one settings channel. Returns (problems, notes). Problems block a checkmark."""
        try:
            channel = await self._full_channel(guild, channel)
        except ValueError as exc:
            return [str(exc)], []
        label = {
            'raid_forum': 'Raid forum',
            'forum': 'Forum',
            'announcement': 'Announcement push',
            'panel': 'Event panels',
            'calendar': 'Calendar',
            'rss': 'RSS',
            'voice': 'Voice category',
        }.get(kind, kind)
        mention = getattr(channel, 'mention', None) or getattr(channel, 'name', None) or 'that channel'
        errors = []
        notes = []
        try:
            me = guild.me
        except Exception as exc:
            return [f'**{label}:** could not read the bot member ({type(exc).__name__}: {exc}).'], []
        if me is None:
            return [f'**{label} ({mention}):** the bot member is not available in this server, so permissions could not be checked.'], []
        try:
            if kind in ('raid_forum', 'forum'):
                if not isinstance(channel, discord.ForumChannel) or getattr(getattr(channel, 'guild', None), 'id', None) != guild.id:
                    errors.append(f'**{label} ({mention}):** Must be a Forum Channel in this server.')
                else:
                    missing = self._missing_perms(channel.permissions_for(me), [
                        ('view_channel', 'View Channel'),
                        ('send_messages', 'Send Messages (Create Posts)'),
                        ('send_messages_in_threads', 'Send Messages in Posts'),
                        ('embed_links', 'Embed Links'),
                        ('read_message_history', 'Read Post History'),
                        ('manage_threads', 'Manage Posts'),
                    ])
                    if missing:
                        errors.append(f"**{label} ({mention}):** Bot is missing: {', '.join(missing)}.")
                    if getattr(getattr(channel, 'flags', None), 'require_tag', False):
                        errors.append(f"**{label} ({mention}):** Disable *'Require people to select tags when posting'* in channel settings.")
                    if kind == 'raid_forum' and not missing and not getattr(channel.permissions_for(me), 'create_instant_invite', False):
                        notes.append(f"**{label} ({mention}):** Bot lacks `Create Invite`. Mirrored servers will not see a voice link unless you set a server invite URL.")
            elif kind == 'voice':
                if not isinstance(channel, discord.CategoryChannel) or getattr(getattr(channel, 'guild', None), 'id', None) != guild.id:
                    errors.append(f'**{label} ({mention}):** Must be a Category in this server.')
                else:
                    missing = self._missing_perms(channel.permissions_for(me), [
                        ('view_channel', 'View Channel'),
                        ('manage_channels', 'Manage Channels'),
                    ])
                    if missing:
                        errors.append(f"**{label} ({mention}):** Bot is missing: {', '.join(missing)}.")
            else:
                perms = {
                    'announcement': [
                        ('view_channel', 'View Channel'),
                        ('send_messages', 'Send Messages'),
                        ('read_message_history', 'Read Message History'),
                    ],
                    'panel': [
                        ('view_channel', 'View Channel'),
                        ('send_messages', 'Send Messages'),
                        ('embed_links', 'Embed Links'),
                        ('read_message_history', 'Read Message History'),
                    ],
                    'calendar': [
                        ('view_channel', 'View Channel'),
                        ('send_messages', 'Send Messages'),
                        ('embed_links', 'Embed Links'),
                    ],
                    'rss': [
                        ('view_channel', 'View Channel'),
                        ('send_messages', 'Send Messages'),
                        ('embed_links', 'Embed Links'),
                    ],
                }.get(kind)
                if perms is None:
                    errors.append(f'**{label}:** no permission check is defined for this field.')
                elif not isinstance(channel, discord.TextChannel) or getattr(getattr(channel, 'guild', None), 'id', None) != guild.id:
                    errors.append(f'**{label} ({mention}):** Must be a Text or Announcement Channel in this server.')
                else:
                    missing = self._missing_perms(channel.permissions_for(me), perms)
                    if missing:
                        errors.append(f"**{label} ({mention}):** Bot is missing: {', '.join(missing)}.")
        except Exception as exc:
            errors.append(f'**{label} ({mention}):** {type(exc).__name__}: {exc}')
        return errors, notes

    async def probe_text_settings(self, guild, timezone, kin_icon, invite_url, duration_minutes, lock_delay_minutes):
        """Check the text fields from the settings modal. Returns (problems, field states)."""
        errors = []
        states = {}
        if timezone:
            try:
                pytz.timezone(timezone)
                states['server_timezone'] = 'ok'
            except pytz.UnknownTimeZoneError:
                states['server_timezone'] = 'bad'
                errors.append(f'**Server timezone (`{timezone}`):** not a valid IANA timezone (for example `America/New_York`).')
        if kin_icon:
            try:
                resolved = await self.resolve_kin_icon(guild, kin_icon)
            except Exception as exc:
                resolved = None
                states['kin_icon'] = 'bad'
                errors.append(f'**Kin icon:** {type(exc).__name__}: {exc}')
            if resolved:
                states['kin_icon'] = 'ok'
            elif 'kin_icon' not in states:
                states['kin_icon'] = 'bad'
                errors.append(f'**Kin icon (`{kin_icon}`):** not a valid Unicode or accessible custom emoji.')
        if invite_url:
            try:
                invite = await self.bot.fetch_invite(invite_url)
                if not getattr(invite, 'guild', None) or invite.guild.id != guild.id:
                    states['server_invite_url'] = 'bad'
                    errors.append('**Server invite:** The invite must belong to this server.')
                else:
                    states['server_invite_url'] = 'ok'
            except Exception as exc:
                states['server_invite_url'] = 'bad'
                errors.append(f'**Server invite:** {type(exc).__name__}: {exc}')
        if not 15 <= duration_minutes <= 1440:
            errors.append('**Duration:** must be 15–1440 minutes.')
        if not 0 <= lock_delay_minutes <= 10080:
            errors.append('**Lock delay:** must be 0–10080 minutes.')
        return errors, states

    def probe_manage_events(self, guild) -> list[str]:
        try:
            me = guild.me
            allowed = me is not None and getattr(me.guild_permissions, 'manage_events', False)
        except Exception as exc:
            return [f'**Calendar event:** {type(exc).__name__}: {exc}']
        if not allowed:
            return ['**Calendar event:** Bot is missing the server-wide `Manage Events` permission.']
        return []

    def watch_rows(self, guild_id):
        return self.store.rows(
            'SELECT source_channel_id, last_message_id FROM EventWatches WHERE guild_id=? ORDER BY source_channel_id',
            (guild_id,),
        )

    def watch_channel_problem(self, guild, channel) -> str | None:
        mention = getattr(channel, 'mention', None) or f"<#{getattr(channel, 'id', '?')}>"
        if not isinstance(channel, (discord.TextChannel, discord.Thread)) or getattr(getattr(channel, 'guild', None), 'id', None) != guild.id:
            return f'**Event Watch Channel ({mention}):** Must be a Text/Announcement Channel or Thread in this server.'
        me = getattr(guild, 'me', None)
        if me is None:
            return f'**Event Watch Channel ({mention}):** Bot member is unavailable in this server.'
        missing = self._missing_perms(channel.permissions_for(me), [
            ('view_channel', 'View Channel'),
            ('read_message_history', 'Read Message History'),
        ])
        if missing:
            return f"**Event Watch Channel ({mention}):** Bot is missing: {', '.join(missing)}."
        return None

    async def watch_cursor(self, channel) -> int:
        latest = [message async for message in channel.history(limit=1)]
        return latest[0].id if latest else discord.utils.time_snowflake(discord.utils.utcnow())

    async def store_watch(self, guild, channel) -> bool:
        """Remember a channel. Returns True when it was not already watched."""
        prev = self.store.one(
            'SELECT last_message_id FROM EventWatches WHERE guild_id=? AND source_channel_id=?',
            (guild.id, channel.id),
        )
        if prev:
            return False
        self.store.write(
            'INSERT OR REPLACE INTO EventWatches VALUES (?,?,?)',
            (guild.id, channel.id, await self.watch_cursor(channel)),
        )
        return True

    async def add_watch_channels(self, guild, channels):
        """Add every valid channel. Invalid ones are reported and skipped."""
        errors = []
        added = []
        kept = []
        seen = set()
        for channel in channels:
            if getattr(channel, 'id', None) in seen:
                continue
            seen.add(channel.id)
            problem = self.watch_channel_problem(guild, channel)
            if problem:
                errors.append(problem)
                continue
            if await self.store_watch(guild, channel):
                added.append(channel)
            else:
                kept.append(channel)
        return errors, added, kept

    def remove_watch_channels(self, guild_id, channel_ids):
        removed = []
        for channel_id in channel_ids:
            if self.store.one(
                'SELECT 1 FROM EventWatches WHERE guild_id=? AND source_channel_id=?',
                (guild_id, channel_id),
            ):
                self.store.write(
                    'DELETE FROM EventWatches WHERE guild_id=? AND source_channel_id=?',
                    (guild_id, channel_id),
                )
                removed.append(channel_id)
        return removed

    def clear_watch_channels(self, guild_id) -> int:
        rows = self.watch_rows(guild_id)
        if rows:
            self.store.write('DELETE FROM EventWatches WHERE guild_id=?', (guild_id,))
        return len(rows)

    async def resolve_kin_icon(self, guild, raw_icon: str) -> str | None:
        icon_str = raw_icon.strip()
        if Emoji.is_emoji(icon_str):
            return icon_str
        partial = discord.PartialEmoji.from_str(icon_str)
        if partial.is_custom_emoji():
            if hasattr(self.bot, 'get_emoji'):
                found = self.bot.get_emoji(partial.id)
                if found:
                    return str(found)
            if hasattr(self.bot, 'fetch_application_emojis'):
                try:
                    app_emojis = await self.bot.fetch_application_emojis()
                    match = next((e for e in app_emojis if e.id == partial.id or e.name.lower() == partial.name.lower()), None)
                    if not match and hasattr(self.bot, 'create_application_emoji') and hasattr(self.bot, 'http_session'):
                        ext = 'gif' if partial.animated else 'png'
                        url = f'https://cdn.discordapp.com/emojis/{partial.id}.{ext}?size=128'
                        async with self.bot.http_session.get(url) as resp:
                            if resp.ok:
                                safe_name = re.sub(r'[^A-Za-z0-9_]', '_', partial.name)[:32] or 'kin_icon'
                                match = await self.bot.create_application_emoji(name=safe_name, image=await resp.read())
                    if match:
                        return str(match)
                except Exception:
                    pass
            return str(partial)
        short = re.fullmatch(r':?([A-Za-z0-9_]{2,32}):?', icon_str)
        if short:
            name_q = short.group(1).lower()
            for e in getattr(guild, 'emojis', None) or ():
                if e.name.lower() == name_q:
                    return str(e)
            if hasattr(self.bot, 'emojis'):
                for e in self.bot.emojis or ():
                    if e.name.lower() == name_q:
                        return str(e)
            if hasattr(self.bot, 'fetch_application_emojis'):
                try:
                    app_emojis = await self.bot.fetch_application_emojis()
                    match = next((e for e in app_emojis if e.name.lower() == name_q), None)
                    if match:
                        return str(match)
                except Exception:
                    pass
        return None

    def _should_redeploy_panels(self, guild_id, panel_channel_id):
        existing = self.store.rows('SELECT * FROM CreationPanels WHERE guild_id=?', (guild_id,))
        if not existing:
            return True
        if existing[0]['channel_id'] != panel_channel_id:
            return True
        if len(existing) < len(PRESETS):
            return True
        return False

    async def resolve_setup_draft(self, guild, draft):
        problems = []

        async def channel(cid, label, types=(), required=False):
            if not cid:
                if required:
                    problems.append(f'**{label} (required):** not set.')
                return None
            try:
                ch = guild.get_channel(cid) if hasattr(guild, 'get_channel') else None
                if ch is None:
                    ch = await self.bot.fetch_channel(cid)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                problems.append(f'**{label}:** the bot cannot use that channel ({type(exc).__name__}: {exc}).')
                return None
            except Exception as exc:
                problems.append(f'**{label}:** {type(exc).__name__}: {exc}')
                return None
            if not ch or (getattr(ch, 'guild', None) and ch.guild.id != guild.id):
                problems.append(f'**{label}:** must belong to this server.')
                return None
            if types and getattr(ch, 'type', None) not in types:
                problems.append(f'**{label}:** must be a {types[0].name.replace("_", " ")} channel.')
                return None
            return ch

        async def optional_forum(cid, label):
            ch = await channel(cid, label, (discord.ChannelType.forum,))
            if ch and not isinstance(ch, discord.ForumChannel):
                problems.append(f'**{label}:** must be a forum channel.')
                return None
            return ch

        async def optional_role(rid, label):
            if not rid:
                return None
            try:
                role = guild.get_role(rid) if hasattr(guild, 'get_role') else None
                if role is None and hasattr(guild, 'fetch_role'):
                    role = await guild.fetch_role(rid)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                problems.append(f'**{label}:** the bot cannot use that role ({type(exc).__name__}: {exc}).')
                return None
            except Exception as exc:
                problems.append(f'**{label}:** {type(exc).__name__}: {exc}')
                return None
            if role is None:
                problems.append(f'**{label}:** that role no longer exists.')
            return role

        raid_forum = await channel(draft.raid_forum_id, 'Raid forum', (discord.ChannelType.forum,), required=True)
        if raid_forum is not None and not isinstance(raid_forum, discord.ForumChannel):
            problems.append('**Raid forum (required):** must be a forum channel.')
            raid_forum = None
        announcement = await channel(draft.announcement_id, 'Announcement push', (discord.ChannelType.text, discord.ChannelType.news), required=True)
        if announcement is not None and not isinstance(announcement, discord.TextChannel):
            problems.append('**Announcement push (required):** must be a text channel.')
            announcement = None
        panel = await channel(draft.panel_id, 'Event panels', (discord.ChannelType.text, discord.ChannelType.news), required=True)
        if panel is not None and not isinstance(panel, discord.TextChannel):
            problems.append('**Event panels (required):** must be a text channel.')
            panel = None
        voice = await channel(draft.voice_category_id, 'Voice category', (discord.ChannelType.category,))
        if voice is not None and not isinstance(voice, discord.CategoryChannel):
            problems.append('**Voice category:** must be a category.')
            voice = None

        calendar = await channel(draft.calendar_id, 'Calendar channel', (discord.ChannelType.text, discord.ChannelType.news))
        if calendar is not None and not isinstance(calendar, discord.TextChannel):
            problems.append('**Calendar channel:** must be a text channel.')
            calendar = None
        rss = await channel(draft.rss_id, 'RSS channel', (discord.ChannelType.text, discord.ChannelType.news))
        if rss is not None and not isinstance(rss, discord.TextChannel):
            problems.append('**RSS channel:** must be a text channel.')
            rss = None
        instance_forum = await optional_forum(draft.instance_forum_id, 'Instance forum')
        t1_forum = await optional_forum(draft.t1_forum_id, 'T1 forum')
        t2_forum = await optional_forum(draft.t2_forum_id, 'T2 forum')
        t3_forum = await optional_forum(draft.t3_forum_id, 'T3 forum')
        t4_forum = await optional_forum(draft.t4_forum_id, 'T4 forum')
        t5_forum = await optional_forum(draft.t5_forum_id, 'T5 forum')
        leader_role = await optional_role(draft.leader_role_id, 'Raid leader role')
        kin_role = await optional_role(draft.kin_role_id, 'Kinship role')
        if problems:
            raise ValueError('\n'.join(problems))

        return dict(
            raid_forum=raid_forum,
            announcement_channel=announcement,
            panel_channel=panel,
            voice_category=voice,
            instance_forum=instance_forum,
            t1_forum=t1_forum,
            t2_forum=t2_forum,
            t3_forum=t3_forum,
            t4_forum=t4_forum,
            t5_forum=t5_forum,
            leader_role=leader_role,
            kin_role=kin_role,
            kin_icon=draft.kin_icon.strip() or None,
            calendar_channel=calendar,
            discord_events=draft.discord_events,
            rss_channel=rss,
            server_timezone=draft.server_timezone.strip() or None,
            server_invite_url=draft.server_invite_url.strip() or None,
            duration_minutes=draft.duration_minutes,
            lock_delay_minutes=draft.lock_delay_minutes,
            description_template=draft.description_template,
            watch_sync=bool(draft.watch_sync),
        )

    async def report_create_failure(self, interaction, exc, kind='event'):
        if isinstance(exc, discord.NotFound):
            await interaction.followup.send(
                f'Could not create {kind}: that channel no longer exists, or the bot cannot see it.',
                view=TroubleshootView(self, interaction.user.id),
                ephemeral=True,
            )
            return
        await interaction.followup.send(f'Could not create {kind}: {exc}', ephemeral=True)

    async def _saved_channel(self, guild, channel_id):
        fetch = getattr(self.bot, 'fetch_channel', None)
        try:
            channel = await fetch(channel_id) if fetch else await self.channel(channel_id)
        except (discord.NotFound, discord.Forbidden):
            return None
        if not channel or (getattr(channel, 'guild', None) and channel.guild.id != guild.id):
            return None
        return channel

    async def _saved_role(self, guild, role_id):
        if hasattr(guild, 'get_role'):
            found = guild.get_role(role_id)
            if found:
                return found
        fetch = getattr(guild, 'fetch_role', None)
        if not fetch:
            return None
        try:
            return await fetch(role_id)
        except (discord.NotFound, discord.Forbidden):
            return None

    async def troubleshoot_saved_setup(self, guild) -> discord.Embed:
        """Run the permission smoke test against saved channel IDs. Does not write settings."""
        from setup_editor import load_setup_draft
        draft = load_setup_draft(self.store, self.bot.conn, guild.id)
        unresolved = {}

        async def resolve(channel_id, label, required=False):
            if not channel_id:
                if required:
                    unresolved[label] = f'**{label}:** not set. Choose it in `/setup` or `/settings`.'
                return None
            channel = await self._saved_channel(guild, channel_id)
            if channel is None:
                unresolved[label] = f'**{label}:** saved channel no longer exists, or the bot cannot see it.'
            return channel

        async def resolve_role(role_id, label):
            if not role_id:
                return None
            role = await self._saved_role(guild, role_id)
            if role is None:
                unresolved[label] = f'**{label}:** saved role no longer exists.'
            return role

        watch_channels = []
        for row in self.watch_rows(guild.id):
            channel = await resolve(row['source_channel_id'], f"Event Watch Channel <#{row['source_channel_id']}>")
            if channel is not None:
                watch_channels.append(channel)

        _, embed = await self.smoke_test_and_apply_setup(
            interaction=type('GuildOnly', (), {'guild': guild})(),
            raid_forum=await resolve(draft.raid_forum_id, 'Raid Forum', required=True),
            announcement_channel=await resolve(draft.announcement_id, 'Announcement Channel', required=True),
            panel_channel=await resolve(draft.panel_id, 'Event Panel Channel', required=True),
            voice_category=await resolve(draft.voice_category_id, 'Voice Category'),
            instance_forum=await resolve(draft.instance_forum_id, 'Instance Forum'),
            t1_forum=await resolve(draft.t1_forum_id, 'T1 Forum'),
            t2_forum=await resolve(draft.t2_forum_id, 'T2 Forum'),
            t3_forum=await resolve(draft.t3_forum_id, 'T3 Forum'),
            t4_forum=await resolve(draft.t4_forum_id, 'T4 Forum'),
            t5_forum=await resolve(draft.t5_forum_id, 'T5 Forum'),
            watch_channels=watch_channels,
            leader_role=await resolve_role(draft.leader_role_id, 'Raid Leader Role'),
            kin_role=await resolve_role(draft.kin_role_id, 'Kinship Role'),
            kin_icon=draft.kin_icon.strip() or None,
            calendar_channel=await resolve(draft.calendar_id, 'Calendar Channel'),
            discord_events=draft.discord_events,
            rss_channel=await resolve(draft.rss_id, 'RSS Channel'),
            server_timezone=draft.server_timezone.strip() or None,
            server_invite_url=draft.server_invite_url.strip() or None,
            duration_minutes=draft.duration_minutes,
            lock_delay_minutes=draft.lock_delay_minutes,
            apply=False,
            unresolved=unresolved,
        )
        return embed

    def setup_failure_embed(self, errors, checks=None, warnings=None) -> discord.Embed:
        """A smoke-test failure that always names the problems, and stays within Discord's size limit."""
        def clip(lines, limit):
            text = '\n'.join(f'• {line}' for line in lines if line)
            if len(text) <= limit:
                return text
            kept = text[:limit].rsplit('\n', 1)[0]
            return kept + '\n• …truncated. Fix the lines above and run it again.'

        problems = [line for line in (errors or []) if line] or [
            'The smoke test failed without a recorded reason. Run it again and, if this repeats, check the bot log.',
        ]
        embed = discord.Embed(
            title='Setup Smoke Test Failed — Missing Permissions or Invalid Settings',
            colour=discord.Colour(0xe74c3c),
            description='These are the problems:\n\n' + clip(problems, 3500),
        )
        if checks:
            embed.add_field(name='Passed Checks', value=clip(checks, 1000) or '—', inline=False)
        if warnings:
            embed.add_field(name='Notes', value=clip(warnings, 1000) or '—', inline=False)
        return embed

    async def smoke_test_and_apply_setup(
        self,
        interaction: discord.Interaction,
        raid_forum: discord.ForumChannel,
        announcement_channel: discord.TextChannel,
        panel_channel: discord.TextChannel,
        voice_category: discord.CategoryChannel | None = None,
        instance_forum: discord.ForumChannel | None = None,
        t1_forum: discord.ForumChannel | None = None,
        t2_forum: discord.ForumChannel | None = None,
        t3_forum: discord.ForumChannel | None = None,
        t4_forum: discord.ForumChannel | None = None,
        t5_forum: discord.ForumChannel | None = None,
        watch_channel: discord.TextChannel | discord.Thread | None = None,
        watch_channels: list | None = None,
        leader_role: discord.Role | None = None,
        kin_role: discord.Role | None = None,
        kin_icon: str | None = None,
        calendar_channel: discord.TextChannel | None = None,
        discord_events: bool = False,
        rss_channel: discord.TextChannel | None = None,
        server_timezone: str | None = None,
        server_invite_url: str | None = None,
        duration_minutes: int = 180,
        lock_delay_minutes: int = 60,
        description_template: str | None = None,
        watch_sync: bool = True,
        *,
        apply: bool = True,
        unresolved: dict | None = None,
    ) -> tuple[bool, discord.Embed]:
        guild = interaction.guild
        unresolved = unresolved or {}
        errors = []
        warnings = []
        checks = []
        try:
            me = guild.me
        except Exception as exc:
            me = None
            errors.append(f'**Bot member:** could not be read ({type(exc).__name__}: {exc}).')

        def take_unresolved(label):
            note = unresolved.get(label)
            if not note:
                return False
            errors.append(note)
            return True

        def check_forum(forum_obj, label, required=False):
            if forum_obj is None:
                if required:
                    errors.append(f'**{label} (required):** not set.')
                return False
            try:
                if not isinstance(forum_obj, discord.ForumChannel) or forum_obj.guild.id != guild.id:
                    errors.append(f"**{label}:** Must be a Forum Channel in this server.")
                    return False
                if me is None:
                    errors.append(f"**{label} ({forum_obj.mention}):** could not check permissions because the bot member is not available in this server.")
                    return False
                f_perms = forum_obj.permissions_for(me)
                missing = self._missing_perms(f_perms, [
                    ('view_channel', 'View Channel'),
                    ('send_messages', 'Send Messages (Create Posts)'),
                    ('send_messages_in_threads', 'Send Messages in Posts'),
                    ('embed_links', 'Embed Links'),
                    ('read_message_history', 'Read Post History'),
                    ('manage_threads', 'Manage Posts'),
                ])
                if missing:
                    errors.append(f"**{label} ({forum_obj.mention}):** Bot is missing: {', '.join(missing)}.")
                if forum_obj.flags.require_tag:
                    errors.append(f"**{label} ({forum_obj.mention}):** Disable *'Require people to select tags when posting'* in channel settings.")
                if not missing and not forum_obj.flags.require_tag:
                    checks.append(f"**{label}:** {forum_obj.mention} (permissions & tag settings verified)")
                    return True
                return False
            except Exception as exc:
                errors.append(f"**{label}:** {type(exc).__name__}: {exc}")
                return False

        # 1. Default raid forum, then any optional instance or tier forums.
        # Routing is implied: matching tier forum, else instance forum for ≤6-player events, else raid forum.
        if not take_unresolved('Raid Forum'):
            if check_forum(raid_forum, 'Raid Forum', required=True):
                f_perms = raid_forum.permissions_for(me)
                if voice_category and not getattr(f_perms, 'create_instant_invite', False) and not server_invite_url:
                    warnings.append(f"**Raid Forum ({raid_forum.mention}):** Bot lacks `Create Invite` and no `server_invite_url` was provided; mirrored servers will not see a voice invite link until one is granted.")
        checks.append('**Forum routing:** tier forum if set, else instance forum for ≤6-player events, else the raid forum.')

        if not take_unresolved('Instance Forum') and instance_forum is not None:
            check_forum(instance_forum, 'Instance Forum (≤6-man when that tier has no forum)')

        for label, f_obj in (
            ('T1 Forum', t1_forum),
            ('T2 Forum', t2_forum),
            ('T3 Forum', t3_forum),
            ('T4 Forum', t4_forum),
            ('T5 Forum', t5_forum),
        ):
            if take_unresolved(label):
                continue
            if f_obj is not None:
                check_forum(f_obj, label)

        def check_text(channel, label, perms, success):
            if take_unresolved(label):
                return
            if channel is None:
                errors.append(f'**{label} (required):** not set.')
                return
            try:
                if not isinstance(channel, discord.TextChannel) or channel.guild.id != guild.id:
                    errors.append(f'**{label}:** Must be a Text or Announcement Channel in this server.')
                    return
                if me is None:
                    errors.append(f'**{label} ({channel.mention}):** could not check permissions because the bot member is not available in this server.')
                    return
                missing = self._missing_perms(channel.permissions_for(me), perms)
                if missing:
                    errors.append(f"**{label} ({channel.mention}):** Bot is missing: {', '.join(missing)}.")
                else:
                    checks.append(success(channel))
            except Exception as exc:
                errors.append(f"**{label}:** {type(exc).__name__}: {exc}")

        # 2. Mandatory: Announcement (Outgoing Discovery Push) Channel
        check_text(
            announcement_channel, 'Announcement Channel',
            [('view_channel', 'View Channel'), ('send_messages', 'Send Messages'), ('read_message_history', 'Read Message History')],
            lambda channel: f"**Announcement Push Channel:** {channel.mention} (permissions verified)",
        )

        # 3. Mandatory: Event Panel Channel
        check_text(
            panel_channel, 'Event Panel Channel',
            [('view_channel', 'View Channel'), ('send_messages', 'Send Messages'), ('embed_links', 'Embed Links'), ('read_message_history', 'Read Message History')],
            lambda channel: f"**Event Panel Channel:** {channel.mention} ({len(PRESETS)} cluster panels in release order)",
        )

        # 4. Mandatory: Voice Category
        if take_unresolved('Voice Category'):
            pass
        elif voice_category is None:
            checks.append('**Voice Category:** not set. Events will not create or link a voice channel.')
        else:
            try:
                if not isinstance(voice_category, discord.CategoryChannel) or voice_category.guild.id != guild.id:
                    errors.append('**Voice Category:** Must be a Category in this server.')
                elif me is None:
                    errors.append(f'**Voice Category ({voice_category.name}):** could not check permissions because the bot member is not available in this server.')
                else:
                    missing = self._missing_perms(voice_category.permissions_for(me), [
                        ('view_channel', 'View Channel'),
                        ('manage_channels', 'Manage Channels'),
                    ])
                    if missing:
                        errors.append(f"**Voice Category ({voice_category.name}):** Bot is missing: {', '.join(missing)}.")
                    else:
                        checks.append(f"**Voice Category:** {voice_category.name} (permissions verified)")
            except Exception as exc:
                errors.append(f"**Voice Category:** {type(exc).__name__}: {exc}")

        # 5. Optional: Watch (Incoming Discovery Pull) Channels.
        # Passing none leaves the saved list alone. Passing channels adds them.
        for label in [key for key in unresolved if key.startswith('Event Watch Channel')]:
            take_unresolved(label)
        requested = []
        if watch_channel is not None:
            requested.append(watch_channel)
        if watch_channels:
            requested.extend(watch_channels)
        validated = []
        seen_watches = set()
        for channel in requested:
            if channel is None or getattr(channel, 'id', None) in seen_watches:
                continue
            seen_watches.add(channel.id)
            try:
                problem = self.watch_channel_problem(guild, channel)
            except Exception as exc:
                problem = f"**Event Watch Channel:** {type(exc).__name__}: {exc}"
            if problem:
                errors.append(problem)
            else:
                validated.append(channel)
                checks.append(f"**Event Watch (Pull) Channel:** {channel.mention} (permissions verified)")

        # 6. Optional: Raid Leader Role & Kinship Role / Icon
        if not take_unresolved('Raid Leader Role') and leader_role is not None:
            checks.append(f"**Raid Leader Role:** {leader_role.mention}")
        elif 'Raid Leader Role' not in unresolved:
            checks.append('**Raid Leader Role:** not set; anyone can create events and use leader controls.')
        resolved_kin_icon = None
        if kin_icon is not None and kin_icon.strip():
            resolved_kin_icon = await self.resolve_kin_icon(guild, kin_icon)
            if not resolved_kin_icon:
                errors.append(f"**Kin Icon (`{kin_icon.strip()}`):** Not a valid Unicode or accessible custom emoji.")
            elif not kin_role:
                checks.append(f"**Kin Icon:** {resolved_kin_icon}")
        if not take_unresolved('Kinship Role') and kin_role is not None:
            icon_suffix = f" ({resolved_kin_icon})" if resolved_kin_icon else ""
            checks.append(f"**Kinship Role:** {kin_role.mention}{icon_suffix}")

        # 7. Optional: Calendar Channel & Native Discord Events
        if take_unresolved('Calendar Channel'):
            pass
        elif calendar_channel is not None:
            try:
                if not isinstance(calendar_channel, discord.TextChannel) or calendar_channel.guild.id != guild.id:
                    errors.append('**Calendar Channel:** Must be a Text Channel in this server.')
                elif me is None:
                    errors.append(f'**Calendar Channel ({calendar_channel.mention}):** could not check permissions because the bot member is not available in this server.')
                else:
                    missing = self._missing_perms(calendar_channel.permissions_for(me), [
                        ('view_channel', 'View Channel'),
                        ('send_messages', 'Send Messages'),
                        ('embed_links', 'Embed Links'),
                    ])
                    if missing:
                        errors.append(f"**Calendar Channel ({calendar_channel.mention}):** Bot is missing: {', '.join(missing)}.")
                    else:
                        checks.append(f"**Calendar Channel:** {calendar_channel.mention} (permissions verified)")
            except Exception as exc:
                errors.append(f"**Calendar Channel:** {type(exc).__name__}: {exc}")
        if discord_events:
            try:
                allowed = me is not None and getattr(me.guild_permissions, 'manage_events', False)
            except Exception as exc:
                allowed = False
                errors.append(f'**Calendar Event:** {type(exc).__name__}: {exc}')
            else:
                if not allowed:
                    errors.append('**Calendar Event (`discord_events=True`):** Bot is missing the server-wide `Manage Events` permission.')
                else:
                    checks.append('**Calendar Event:** Enabled (`Manage Events` verified)'
                                  + ('' if calendar_channel is not None else '; set a calendar channel for it to appear'))

        # 8. Optional: RSS Channel
        if take_unresolved('RSS Channel'):
            pass
        elif rss_channel is not None:
            try:
                if not isinstance(rss_channel, discord.TextChannel) or rss_channel.guild.id != guild.id:
                    errors.append('**RSS Channel:** Must be a Text Channel in this server.')
                elif me is None:
                    errors.append(f'**RSS Channel ({rss_channel.mention}):** could not check permissions because the bot member is not available in this server.')
                else:
                    missing = self._missing_perms(rss_channel.permissions_for(me), [
                        ('view_channel', 'View Channel'),
                        ('send_messages', 'Send Messages'),
                        ('embed_links', 'Embed Links'),
                    ])
                    if missing:
                        errors.append(f"**RSS Channel ({rss_channel.mention}):** Bot is missing: {', '.join(missing)}.")
                    else:
                        checks.append(f"**RSS Channel:** {rss_channel.mention} (permissions verified)")
            except Exception as exc:
                errors.append(f"**RSS Channel:** {type(exc).__name__}: {exc}")

        # 9. Optional: Server Timezone
        resolved_tz = None
        if server_timezone is not None and server_timezone.strip():
            try:
                resolved_tz = str(pytz.timezone(server_timezone.strip()))
                checks.append(f"**Server Timezone:** `{resolved_tz}`")
            except pytz.UnknownTimeZoneError:
                errors.append(f"**Server Timezone (`{server_timezone}`):** Invalid timezone name. Choose a valid IANA timezone (e.g. `America/New_York`).")

        # 10. Optional: Server Invite URL
        verified_invite_url = None
        if server_invite_url is not None and server_invite_url.strip():
            try:
                invite = await self.bot.fetch_invite(server_invite_url.strip())
                if not invite.guild or invite.guild.id != guild.id:
                    errors.append('**Server Invite URL:** The invite must belong to this server.')
                else:
                    verified_invite_url = invite.url
                    checks.append(f"**Server Voice Invite:** {verified_invite_url}")
            except (discord.NotFound, discord.HTTPException):
                errors.append('**Server Invite URL:** Could not verify that invite link. Use a valid, non-expiring Discord invite to this server.')

        if errors:
            return False, self.setup_failure_embed(errors, checks, warnings)

        if not apply:
            embed = discord.Embed(
                title='Permission smoke test passed',
                colour=discord.Colour(0x2ecc71),
                description='\n'.join(f"• {c}" for c in checks) or 'Saved channels passed the permission checks.',
            )
            if warnings:
                embed.add_field(name='Notes', value='\n'.join(f"• {w}" for w in warnings)[:1024], inline=False)
            return True, embed

        # All smoke tests passed — persist settings and deploy panels/calendar.
        self.store.configure(
            guild.id,
            raid_forum.id,
            voice_category.id if voice_category else 0,
            duration_minutes * 60,
            lock_delay_minutes * 60,
            instance_forum_id=instance_forum.id if instance_forum else 0,
            t1_forum_id=t1_forum.id if t1_forum else 0,
            t2_forum_id=t2_forum.id if t2_forum else 0,
            t3_forum_id=t3_forum.id if t3_forum else 0,
            t4_forum_id=t4_forum.id if t4_forum else 0,
            t5_forum_id=t5_forum.id if t5_forum else 0,
            watch_sync=watch_sync,
        )
        self.store.write('INSERT OR REPLACE INTO NetworkSettings VALUES (?,?)', (guild.id, announcement_channel.id))
        if watch_sync:
            checks.append('**Watch sync:** on. Watched mirrorkey codes create raid posts.')
            created, failed = await self.materialize_watch_calendar(guild.id)
            if created:
                checks.append(f'**Watch sync:** created {created} raid post(s) that were listed on the calendar only.')
            for item in failed:
                warnings.append(f'**Watch sync:** could not create a raid post: {item}')
        else:
            checks.append('**Watch sync:** off. Watched events stay on the calendar and do not create raid posts.')

        for channel in validated:
            if await self.store_watch(guild, channel):
                checks.append(f"**Event Watch (Pull) Channel:** {channel.mention} (watching new messages)")
            else:
                checks.append(f"**Event Watch (Pull) Channel:** {channel.mention} (unchanged cursor)")

        upsert(self.bot.conn, 'Settings', ['raid_leader'], [leader_role.id if leader_role else None], ['guild_id'], [guild.id])
        if kin_role is not None:
            upsert(self.bot.conn, 'Settings', ['priority'], [kin_role.id], ['guild_id'], [guild.id])
        if resolved_kin_icon is not None:
            old_icon = self.store.one('SELECT icon FROM Settings WHERE guild_id=?', (guild.id,))
            upsert(self.bot.conn, 'Settings', ['icon'], [resolved_kin_icon], ['guild_id'], [guild.id])
            if old_icon and old_icon.get('icon') and old_icon['icon'] != resolved_kin_icon:
                for p in self.store.rows('SELECT raid_id, player_id, byname FROM Players WHERE byname LIKE ?', (f"{old_icon['icon']}%",)):
                    new_byname = resolved_kin_icon + p['byname'][len(old_icon['icon']):]
                    self.bot.conn.execute('UPDATE Players SET byname=? WHERE raid_id=? AND player_id=?', (new_byname, p['raid_id'], p['player_id']))
        elif kin_role is not None:
            existing_icon = self.store.one('SELECT icon FROM Settings WHERE guild_id=?', (guild.id,))
            if not existing_icon or not existing_icon.get('icon'):
                ident = self.store.one('SELECT emoji FROM GuildIdentity WHERE guild_id=?', (guild.id,))
                if ident and ident.get('emoji'):
                    upsert(self.bot.conn, 'Settings', ['icon'], [ident['emoji']], ['guild_id'], [guild.id])
        if rss_channel is not None:
            upsert(self.bot.conn, 'Settings', ['rss'], [rss_channel.id], ['guild_id'], [guild.id])
        else:
            upsert(self.bot.conn, 'Settings', ['rss'], [None], ['guild_id'], [guild.id])
        if resolved_tz is not None:
            upsert(self.bot.conn, 'Settings', ['server'], [resolved_tz], ['guild_id'], [guild.id])
        upsert(self.bot.conn, 'Settings', ['guild_events'], [discord_events], ['guild_id'], [guild.id])
        if verified_invite_url is not None:
            self.store.write('INSERT OR REPLACE INTO SourceInvites VALUES (?,?)', (guild.id, verified_invite_url))
            self.store.write('UPDATE SharedEvents SET revision=revision+1 WHERE raid_id IN (SELECT raid_id FROM Raids WHERE guild_id=?)', (guild.id,))
        if description_template is not None:
            await self.persist_description_template(guild.id, description_template or None)
            if description_template:
                checks.append('**Event description template:** updated from message link')
            else:
                checks.append('**Event description template:** cleared')
        self.bot.conn.commit()

        network = self.bot.get_cog('NetworkCog')
        if network and self._should_redeploy_panels(guild.id, panel_channel.id):
            await network.deploy_all_panels(guild.id, panel_channel.id)
        elif network:
            checks.append(f"**Event Panel Channel:** {panel_channel.mention} (panels unchanged)")

        prev_cal = self.store.one('SELECT calendar FROM Settings WHERE guild_id=?', (guild.id,))
        old_cal_channel = None
        if prev_cal and prev_cal.get('calendar') and '/' in str(prev_cal['calendar']):
            old_cal_channel = int(str(prev_cal['calendar']).split('/')[0])
        if calendar_channel is None:
            if old_cal_channel:
                upsert(self.bot.conn, 'Settings', ['calendar'], [None], ['guild_id'], [guild.id])
        elif self.raids and self.raids.calendar_cog:
            if old_cal_channel != calendar_channel.id:
                await self.raids.calendar_cog.post_calendar(guild.id, calendar_channel)
            else:
                await self.raids.calendar_cog.update_calendar(guild.id)
                checks.append(f"**Calendar Channel:** {calendar_channel.mention} (unchanged, refreshed)")
        if self.raids and self.raids.calendar_cog:
            await self.raids.calendar_cog.ensure_calendar_event(guild)

        checks.append(f"**Event Timings:** {duration_minutes}m default duration, {lock_delay_minutes}m post-event lock delay")
        embed = discord.Embed(
            title='Server Setup Complete — All Permission Smoke Tests Passed',
            colour=discord.Colour(0x2ecc71),
            description='\n'.join(f"• {c}" for c in checks),
        )
        if warnings:
            embed.add_field(name='Notes', value='\n'.join(f"• {w}" for w in warnings)[:1024], inline=False)
        return True, embed

    @app_commands.command(name='setup', description='Configure the raid forum, optional instance and tier forums, panels, and roles.')
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.autocomplete(server_timezone=time_zone_autocomplete)
    @app_commands.describe(
        raid_forum='Default forum. Used when no matching tier forum is set, and for raids when no instance forum applies.',
        announcement_channel='Mandatory: Outgoing channel where mirrorkey discovery codes are posted.',
        panel_channel='Mandatory: Channel where sticky cluster raid creation panels are posted.',
        voice_category='Optional: Category for a temporary voice channel at start time. Leave empty and events will not create or link a voice channel.',
        instance_forum='Optional: ≤6-player events whose tier has no forum of its own.',
        t1_forum='Optional: Tier 1 events (instances and raids) go here instead of the instance or default forum.',
        t2_forum='Optional: Tier 2 / T2c events.',
        t3_forum='Optional: Tier 3 events.',
        t4_forum='Optional: Tier 4 events.',
        t5_forum='Optional: Tier 5 events.',
        watch_channel='Optional: Add one channel to the mirrorkey watch list. Does not remove channels already watched. Use /watch to add or remove any number.',
        leader_role='Optional. When set, only this role can create events and use leader controls. Leave unset and anyone can.',
        kin_role='Optional: Role used to distinguish kinship members on raid signups.',
        kin_icon='Optional: Emoji badge displayed next to kinship members on raid signups.',
        calendar_channel='Optional: Channel where the live auto-updating raid calendar embed is posted.',
        discord_events='Optional: Keep one Discord event that links to the calendar (needs calendar_channel).',
        rss_channel='Optional: Channel where official LOTRO forum RSS announcements are posted.',
        server_timezone='Optional: Default server timezone (e.g. America/New_York).',
        server_invite_url='Optional: Non-expiring invite URL to this server for cross-server voice links.',
        duration_minutes='Optional: Default raid duration in minutes (15–1440, default 180).',
        lock_delay_minutes='Optional: Minutes after event end before locking forum threads (0–10080, default 60).',
        description_message_link='Optional: Discord message link copied into the reusable event description template (see `/settings`).',
    )
    async def setup_command(
        self,
        interaction: discord.Interaction,
        raid_forum: discord.ForumChannel,
        announcement_channel: discord.TextChannel,
        panel_channel: discord.TextChannel,
        voice_category: discord.CategoryChannel | None = None,
        instance_forum: discord.ForumChannel | None = None,
        t1_forum: discord.ForumChannel | None = None,
        t2_forum: discord.ForumChannel | None = None,
        t3_forum: discord.ForumChannel | None = None,
        t4_forum: discord.ForumChannel | None = None,
        t5_forum: discord.ForumChannel | None = None,
        watch_channel: discord.TextChannel | discord.Thread | None = None,
        leader_role: discord.Role | None = None,
        kin_role: discord.Role | None = None,
        kin_icon: str | None = None,
        calendar_channel: discord.TextChannel | None = None,
        discord_events: bool = False,
        rss_channel: discord.TextChannel | None = None,
        server_timezone: str | None = None,
        server_invite_url: str | None = None,
        duration_minutes: app_commands.Range[int, 15, 1440] = 180,
        lock_delay_minutes: app_commands.Range[int, 0, 10080] = 60,
        description_message_link: str | None = None,
    ):
        await interaction.response.defer(ephemeral=True)
        description_template = None
        if description_message_link is not None and description_message_link.strip():
            description_template = await self.resolve_template_from_link(interaction.guild, description_message_link)
        _, embed = await self.smoke_test_and_apply_setup(
            interaction=interaction,
            raid_forum=raid_forum,
            announcement_channel=announcement_channel,
            panel_channel=panel_channel,
            voice_category=voice_category,
            instance_forum=instance_forum,
            t1_forum=t1_forum,
            t2_forum=t2_forum,
            t3_forum=t3_forum,
            t4_forum=t4_forum,
            t5_forum=t5_forum,
            watch_channel=watch_channel,
            leader_role=leader_role,
            kin_role=kin_role,
            kin_icon=kin_icon,
            calendar_channel=calendar_channel,
            discord_events=discord_events,
            rss_channel=rss_channel,
            server_timezone=server_timezone,
            server_invite_url=server_invite_url,
            duration_minutes=duration_minutes,
            lock_delay_minutes=lock_delay_minutes,
            description_template=description_template,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def reset_guild_setup(self, guild):
        """Clear one server's saved setup. Existing events, rosters, and forum posts stay."""
        guild_id = guild.id
        notes = []

        async def delete_message(channel_id, message_id, label):
            channel = None
            getter = getattr(guild, 'get_channel', None)
            if getter:
                channel = getter(channel_id)
            if channel is None and hasattr(self.bot, 'get_channel'):
                channel = self.bot.get_channel(channel_id)
            if channel is None:
                notes.append(f'{label} {message_id} is still in Discord; its channel is not loaded.')
                return
            try:
                await channel.get_partial_message(message_id).delete()
            except discord.NotFound:
                pass
            except (discord.Forbidden, discord.HTTPException, AttributeError) as exc:
                notes.append(f'{label} {message_id} is still in Discord ({type(exc).__name__}: {exc}).')

        for panel in self.store.rows('SELECT message_id, channel_id FROM CreationPanels WHERE guild_id=?', (guild_id,)):
            await delete_message(panel['channel_id'], panel['message_id'], 'Panel message')
        calendar = self.store.one('SELECT calendar FROM Settings WHERE guild_id=?', (guild_id,))
        if calendar and calendar.get('calendar') and '/' in str(calendar['calendar']):
            channel_id, message_id = str(calendar['calendar']).split('/', 1)
            await delete_message(int(channel_id), int(message_id), 'Calendar message')
        calendar_cog = self.raids.calendar_cog if self.raids and getattr(self.raids, 'calendar_cog', None) else None
        drop = getattr(calendar_cog, 'remove_calendar_event', None)
        if drop:
            await drop(guild)

        for table in (
            'EventSettings', 'NetworkSettings', 'EventWatches', 'WatchCalendar',
            'WatchFailures', 'PendingPublications', 'EventTemplates', 'CreationPanels',
            'SourceInvites', 'GuildIdentity', 'PanelJobs',
        ):
            if self.store.one("SELECT 1 AS ok FROM sqlite_master WHERE type='table' AND name=?", (table,)):
                self.store.write(f'DELETE FROM {table} WHERE guild_id=?', (guild_id,))
        self.bot.conn.execute(
            '''UPDATE Settings SET server=NULL, raid_leader=NULL, priority=NULL, icon=NULL,
               calendar=NULL, guild_events=NULL, rss=NULL, calendar_event_id=NULL WHERE guild_id=?''',
            (guild_id,),
        )
        self.bot.conn.commit()
        return notes

    @app_commands.command(name='setup-reset', description='Clear this server’s saved setup so /setup can be run again.')
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(confirm='Must be True. Existing events, rosters, and forum posts are left in place.')
    async def setup_reset_command(self, interaction: discord.Interaction, confirm: bool):
        if not confirm:
            await interaction.response.send_message(
                'Nothing was cleared. Run `/setup-reset` again with **confirm** set to True.',
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        notes = await self.reset_guild_setup(interaction.guild)
        lines = [
            'Saved setup for this server was cleared. Existing events were left in place.',
            'Run `/setup` or `/settings` to configure it again. Cluster panels are posted again on the next setup.',
        ]
        if notes:
            lines.extend(['', *notes])
        await interaction.followup.send('\n'.join(lines)[:1900], ephemeral=True)

    @app_commands.command(name='settings', description='Open the paged server settings editor (prefilled from saved configuration).')
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.default_permissions(manage_guild=True)
    async def settings_command(self, interaction: discord.Interaction):
        from setup_editor import SetupEditorView, load_setup_draft
        draft = load_setup_draft(self.store, self.bot.conn, interaction.guild_id)
        view = SetupEditorView(self, interaction.user.id, draft, interaction.guild_id)
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)

    @app_commands.command(name='watch', description='Add or remove channels scanned for incoming mirrorkey codes.')
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.default_permissions(manage_guild=True)
    async def watch_command(self, interaction: discord.Interaction):
        from setup_editor import WatchListView
        view = WatchListView(self, interaction.guild, interaction.user.id)
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)

    @app_commands.command(name='event-share', description='Create or revoke the code other servers use to sync this raid.')
    @app_commands.guild_only()
    async def share_command(self, interaction: discord.Interaction, revoke: bool = False):
        event = await self.command_event(interaction)
        if not event:
            return
        if self.store.ended(event, time.time()):
            await interaction.response.send_message('This event has ended.', ephemeral=True)
            return
        if self.store.poll_open(event['raid_id']):
            await interaction.response.send_message('This poll is not shared until it becomes an event.', ephemeral=True)
            return
        if revoke:
            self.store.write('UPDATE SharedEvents SET share_token=NULL WHERE raid_id=?', (event['raid_id'],))
            message = 'Sharing code revoked. Existing synchronized servers remain connected.'
        else:
            token = event['share_token'] or self.store.share(event['raid_id'])
            message = f'Give this code to a partner server’s raid leader: `/event-sync code:{token}`\nOr post this keyword in a watched channel: `mirrorkey:{token}`\nIt grants access to this event’s roster and announcements. This code stays valid until explicitly revoked.'
        await interaction.response.send_message(message, ephemeral=True)

    @app_commands.command(name='event-sync', description='Sync a shared raid into this server’s event forum.')
    @app_commands.guild_only()
    async def sync_command(self, interaction: discord.Interaction, code: str):
        if not self.raids.calendar_cog.is_raid_leader(interaction.user, interaction.guild):
            await interaction.response.send_message('A server administrator or configured raid leader must sync events.', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            thread_id, created = await self.sync_event(interaction.guild_id, code)
        except (ValueError, discord.HTTPException) as exc:
            await interaction.followup.send(f'Could not sync event: {exc}', ephemeral=True)
            return
        await interaction.followup.send(f"{'Synced' if created else 'Already synced'}: <#{thread_id}>", ephemeral=True)

    @app_commands.command(name='event-announce', description='Send an announcement to every synchronized event thread.')
    @app_commands.guild_only()
    async def announce_command(self, interaction: discord.Interaction, message: app_commands.Range[str, 1, 1800] | None = None):
        event = await self.command_event(interaction)
        if not event:
            return
        if self.store.lock_due(event, time.time()):
            await interaction.response.send_message('This event is closed.', ephemeral=True)
            return
        planning = self.bot.get_cog('PlanningCog')
        if message is None:
            template = planning.template(event, 'announcement') if planning else None
            if not template:
                await interaction.response.send_message('Supply a message or set an announcement template.', ephemeral=True)
                return
            try:
                message = planning.render(event, template['body'])
            except ValueError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
        await interaction.response.defer(ephemeral=True)
        self.store.queue_announcement(event['raid_id'], message, interaction.user.id)
        await self.deliver_announcements()
        await interaction.followup.send('Announcement queued for all linked threads; failed deliveries retry automatically.', ephemeral=True)

    @app_commands.command(name='event-duration', description='Set this event’s duration in minutes.')
    @app_commands.guild_only()
    async def duration_command(self, interaction: discord.Interaction, minutes: app_commands.Range[int, 15, 1440]):
        event = await self.command_event(interaction)
        if not event:
            return
        if self.store.ended(event, time.time()):
            await interaction.response.send_message('Ended events cannot be extended. Create a new event.', ephemeral=True)
            return
        self.store.write('UPDATE SharedEvents SET duration=?, revision=revision+1 WHERE raid_id=?', (minutes * 60, event['raid_id']))
        await interaction.response.defer(ephemeral=True)
        await self.reconcile(event['raid_id'])
        await interaction.followup.send(f'Event duration is now {minutes} minutes.', ephemeral=True)

    async def cog_app_command_error(self, interaction, error):
        log.error('Event command failed', exc_info=error)
        text = 'The command could not complete. Check permissions and the bot logs.'
        if isinstance(error, app_commands.MissingPermissions):
            text = 'You need Manage Server permission for that command.'
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)


class TimezonePicker(discord.ui.View):
    """Common cities, plus a choice that clears a personal timezone."""

    def __init__(self, cog, user_id, current):
        super().__init__(timeout=180)
        self.cog = cog
        self.user_id = user_id
        zones = list(COMMON_TIMEZONES[:24])
        if current and current not in zones:
            zones = [current, *zones[:23]]
        options = [discord.SelectOption(label='Use server timezone', value='__server__', default=not current)]
        options.extend(
            discord.SelectOption(label=zone, value=zone, default=zone == current) for zone in zones)
        self.menu = discord.ui.Select(placeholder='Personal timezone', options=options[:25])
        self.menu.callback = self.chosen
        self.add_item(self.menu)

    async def chosen(self, interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message('This menu is for the person who opened it.', ephemeral=True)
            return
        value = self.menu.values[0]
        tz = None if value == '__server__' else value
        upsert(self.cog.bot.conn, 'Timezone', ['timezone'], [tz], ['player_id'], [interaction.user.id])
        self.cog.bot.conn.commit()
        if tz:
            content = f'Set your time zone to {tz}.'
        else:
            server = 'the server timezone'
            time_cog = self.cog.bot.get_cog('TimeCog')
            if time_cog:
                server = time_cog.get_server_timezone(interaction.guild_id) or server
            content = f'Cleared your personal timezone. Times you type use {server}.'
        await interaction.response.edit_message(content=content, view=None)


class CreationView(discord.ui.View):
    def __init__(self, cog, preset):
        super().__init__(timeout=None)
        for index, template in enumerate(PRESETS[preset]['buttons']):
            self.add_item(CreateButton(cog, preset, index, template))


class CreateButton(discord.ui.Button):
    def __init__(self, cog, preset, index, template):
        super().__init__(label=template['label'], style=discord.ButtonStyle.primary,
                         custom_id=f'event-create:{preset}:{index}', row=index // 5)
        self.cog, self.template = cog, template

    async def callback(self, interaction):
        if self.template.get('timezone'):
            await self.cog.open_timezone_picker(interaction)
            return
        if not self.cog.raids.calendar_cog.is_raid_leader(interaction.user, interaction.guild):
            await interaction.response.send_message('Ask an administrator to give you the configured raid leader role.', ephemeral=True)
            return
        if self.template.get('poll'):
            await interaction.response.send_modal(PollModal(self.cog, interaction.guild_id, interaction.user.id))
            return
        if self.template.get('time_poll'):
            await interaction.response.send_modal(TimePollModal(self.cog, interaction.guild_id, interaction.user.id))
            return
        await interaction.response.send_modal(CreateEventModal(self.cog, self.template, interaction.guild_id, interaction.user.id))


class TroubleshootView(discord.ui.View):
    """Read-only permission smoke test for the person who hit an unknown channel."""

    def __init__(self, cog, user_id):
        super().__init__(timeout=300)
        self.cog = cog
        self.user_id = user_id

    async def interaction_check(self, interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                'This troubleshooting report is for the person who hit the error.', ephemeral=True)
            return False
        return True

    @discord.ui.button(label='Troubleshooting', style=discord.ButtonStyle.secondary)
    async def troubleshoot(self, interaction, button):
        await interaction.response.defer(ephemeral=True)
        try:
            embed = await self.cog.troubleshoot_saved_setup(interaction.guild)
        except Exception as exc:
            embed = self.cog.setup_failure_embed([f'**Troubleshooting stopped:** {type(exc).__name__}: {exc}'])
        await interaction.followup.send(embed=embed, ephemeral=True)


class CreateEventModal(discord.ui.Modal):
    def __init__(self, cog, template, guild_id=None, user_id=None):
        super().__init__(title='Create event')
        self.cog, self.template = cog, template
        self.name = discord.ui.TextInput(label='Title', default=template['name'], max_length=80)
        self.when = discord.ui.TextInput(
            label='Start time',
            placeholder=time_entry_placeholder(cog.bot, guild_id, user_id, 'Friday 8pm'),
            max_length=64)
        self.tier = discord.ui.TextInput(label='Tier (optional)', placeholder='T1–T5', required=False, max_length=3)
        self.duration = discord.ui.TextInput(label='Duration in minutes (blank = server default)', required=False, max_length=4)
        self.aim = discord.ui.TextInput(label='Aim', required=False, max_length=1000, style=discord.TextStyle.paragraph)
        for field in (self.name, self.when, self.tier, self.duration, self.aim):
            self.add_item(field)

    async def on_submit(self, interaction):
        if not self.cog.raids.calendar_cog.is_raid_leader(interaction.user, interaction.guild):
            await interaction.response.send_message('You no longer have permission to create events.', ephemeral=True)
            return
        try:
            timestamp = Time().converter(self.cog.bot, interaction.guild_id, interaction.user.id, str(self.when))
            if not time.time() < timestamp <= time.time() + 31536000:
                raise ValueError('Choose a future start within one year.')
            tier = str(self.tier).strip().upper()
            if tier and tier not in ('T1', 'T2', 'T2C', 'T3', 'T4', 'T5'):
                raise ValueError('Tier must be T1, T2, T2c, T3, T4 or T5.')
            minutes = int(str(self.duration)) if str(self.duration).strip() else None
            if minutes is not None and not 15 <= minutes <= 1440:
                raise ValueError('Duration must be 15–1440 minutes.')
        except (ValueError, commands.BadArgument) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            creep = bool(self.template.get('creep', False))
            raid_id = await self.cog.raids.post_raid(str(self.name), tier, str(self.aim), timestamp,
                bool(tier and int(tier[1]) > 2) and not creep, interaction.guild_id, interaction.channel, interaction.user.id,
                creep=creep, size=self.template['size'], duration=minutes * 60 if minutes else None)
            event = self.cog.store.event(raid_id)
            await interaction.followup.send(f"Created <#{event['channel_id']}>", ephemeral=True)
        except (ValueError, discord.HTTPException) as exc:
            await self.cog.report_create_failure(interaction, exc)


class PollVoteView(discord.ui.View):
    def __init__(self, cog, raid_id):
        super().__init__(timeout=None)
        self.cog = cog
        self.raid_id = raid_id
        options = []
        for choice in cog.store.poll_choices(raid_id):
            description = f"{choice['size']}-player" if choice['size'] else None
            options.append(discord.SelectOption(
                label=choice_label(choice)[:100], value=str(choice['position']), description=description))
        if not options:
            raise ValueError('Poll has no choices.')
        self.vote_menu = discord.ui.Select(
            custom_id=f'poll-vote:{raid_id}',
            placeholder='Vote for any you want to run',
            min_values=1,
            max_values=len(options),
            options=options,
        )
        self.vote_menu.callback = self.vote
        self.add_item(self.vote_menu)

    async def vote(self, interaction):
        if not self.cog.store.poll_open(self.raid_id):
            await interaction.response.send_message('This poll has closed.', ephemeral=True)
            return
        self.cog.store.replace_votes(self.raid_id, interaction.user.id, [int(value) for value in self.vote_menu.values])
        self.cog.store.touch(self.raid_id)
        await interaction.response.send_message('Vote recorded. Sending the menu again replaces your votes.', ephemeral=True)
        await self.cog.refresh(self.raid_id)


class PollModal(discord.ui.Modal):
    def __init__(self, cog, guild_id=None, user_id=None):
        super().__init__(title='Create content poll')
        self.cog = cog
        self.name = discord.ui.TextInput(label='Title', max_length=80)
        self.when = discord.ui.TextInput(
            label='Start time',
            placeholder=time_entry_placeholder(cog.bot, guild_id, user_id, 'Friday 8pm'),
            max_length=64)
        early, later = poll_select_options()
        self.early = ModalSelect(
            custom_id='poll-choices-early', placeholder='Levels 50–140',
            min_values=0, max_values=len(early), options=early, required=False)
        self.later = ModalSelect(
            custom_id='poll-choices-later', placeholder='Level 150, 160, and other',
            min_values=0, max_values=len(later), options=later, required=False)
        self.tiers = tier_select(modal=True)
        self.tiers.custom_id = 'poll-tiers'
        self.add_item(self.name)
        self.add_item(self.when)
        self.add_item(discord.ui.Label(
            text='Choices, levels 50–140',
            description='Each selected tier is added to every event. 2 to 25 choices.',
            component=self.early))
        self.add_item(discord.ui.Label(
            text='Choices, 150, 160, and other',
            component=self.later))
        self.add_item(discord.ui.Label(text='Tiers', component=self.tiers))

    async def on_submit(self, interaction):
        if not self.cog.raids.calendar_cog.is_raid_leader(interaction.user, interaction.guild):
            await interaction.response.send_message('You no longer have permission to create events.', ephemeral=True)
            return
        try:
            timestamp = Time().converter(self.cog.bot, interaction.guild_id, interaction.user.id, str(self.when))
            choices = choices_from_values([*self.early.values, *self.later.values], self.tiers.values)
            if not time.time() + POLL_LEAD < timestamp <= time.time() + 31536000:
                raise ValueError('Polls must start more than 24 hours from now, and within one year.')
        except (ValueError, commands.BadArgument) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            raid_id = await self.cog.create_poll(
                interaction.guild_id, interaction.user.id, str(self.name), '', timestamp, choices)
            event = self.cog.store.event(raid_id)
            await interaction.followup.send(f"Created <#{event['channel_id']}>", ephemeral=True)
        except (ValueError, discord.HTTPException) as exc:
            await self.cog.report_create_failure(interaction, exc, kind='poll')


class ConvertChoicesView(discord.ui.View):
    """Choices for turning an existing event into a two-hour poll."""

    def __init__(self, cog, user_id, raid_id):
        super().__init__(timeout=300)
        self.cog = cog
        self.user_id = user_id
        self.raid_id = raid_id
        early, later = poll_select_options()
        self.early = discord.ui.Select(
            placeholder='Levels 50–140', min_values=0, max_values=len(early), options=early)
        self.later = discord.ui.Select(
            placeholder='Level 150, 160, seasonal, and custom', min_values=0, max_values=len(later), options=later)
        self.early.callback = acknowledge_selection
        self.later.callback = acknowledge_selection
        self.tiers = tier_select()
        self.add_item(self.early)
        self.add_item(self.later)
        self.add_item(self.tiers)

    async def interaction_check(self, interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message('This poll setup is not yours.', ephemeral=True)
            return False
        return True

    @discord.ui.button(label='Start poll', style=discord.ButtonStyle.success, row=3)
    async def start(self, interaction, button):
        try:
            choices = choices_from_values([*self.early.values, *self.later.values], self.tiers.values)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            closes = await self.cog.convert_to_poll(self.raid_id, choices)
            await interaction.edit_original_response(
                content=f"Poll started. It closes <t:{closes}:R>. Signed-up players were pinged.", view=None)
        except (ValueError, discord.HTTPException) as exc:
            await interaction.followup.send(f'Could not start the poll: {exc}', ephemeral=True)


def template_from_value(value):
    for preset, index, button in iter_templates(EARLY_PRESETS + LATER_PRESETS):
        if f'{preset}:{index}' == value:
            return {'name': button['name'], 'size': int(button['size']), 'creep': int(bool(button.get('creep')))}
    raise ValueError('Choose one event.')


class TimePollTemplateView(discord.ui.View):
    """One event for a time poll, or a content poll that is chosen next."""

    def __init__(self, cog, user_id, title, hours):
        super().__init__(timeout=300)
        self.cog = cog
        self.user_id = user_id
        self.title = title
        self.hours = hours
        early, later = poll_select_options()
        self.early = discord.ui.Select(
            placeholder='Levels 50–140', min_values=1, max_values=1, options=early)
        self.later = discord.ui.Select(
            placeholder='Level 150, 160, seasonal, and custom', min_values=1, max_values=1, options=later)
        self.early.callback = acknowledge_selection
        self.later.callback = acknowledge_selection
        self.add_item(self.early)
        self.add_item(self.later)

    async def interaction_check(self, interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message('This poll setup is not yours.', ephemeral=True)
            return False
        return True

    @discord.ui.button(label='Use this event', style=discord.ButtonStyle.success, row=2)
    async def use_event(self, interaction, button):
        values = [*self.early.values, *self.later.values]
        if len(values) != 1:
            await interaction.response.send_message('Choose one event.', ephemeral=True)
            return
        try:
            template = template_from_value(values[0])
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            raid_id = await self.cog.create_time_poll(
                interaction.guild_id, interaction.user.id, self.title, self.hours, template=template)
            event = self.cog.store.event(raid_id)
            await interaction.edit_original_response(content=f"Created <#{event['channel_id']}>", view=None)
        except (ValueError, discord.HTTPException) as exc:
            await self.cog.report_create_failure(interaction, exc, kind='poll')

    @discord.ui.button(label='Content Poll', style=discord.ButtonStyle.primary, row=2)
    async def content_poll(self, interaction, button):
        earliest = min(choice['starts'] for choice in self.hours)
        if not time.time() + POLL_LEAD * 2 < earliest <= time.time() + 31536000:
            await interaction.response.send_message(
                'A time poll that becomes a content poll needs its earliest hour more than 48 hours away.',
                ephemeral=True)
            return
        view = TimePollChoicesView(self.cog, self.user_id, self.title, self.hours)
        await interaction.response.edit_message(
            content='Pick the events for the content poll. Choose at least 2 and at most 25, then create it.',
            view=view)


class TimePollChoicesView(discord.ui.View):
    """Content choices for a time poll whose template is the content poll."""

    def __init__(self, cog, user_id, title, hours):
        super().__init__(timeout=300)
        self.cog = cog
        self.user_id = user_id
        self.title = title
        self.hours = hours
        early, later = poll_select_options()
        self.early = discord.ui.Select(
            placeholder='Levels 50–140', min_values=0, max_values=len(early), options=early)
        self.later = discord.ui.Select(
            placeholder='Level 150, 160, seasonal, and custom', min_values=0, max_values=len(later), options=later)
        self.early.callback = acknowledge_selection
        self.later.callback = acknowledge_selection
        self.tiers = tier_select()
        self.add_item(self.early)
        self.add_item(self.later)
        self.add_item(self.tiers)

    async def interaction_check(self, interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message('This poll setup is not yours.', ephemeral=True)
            return False
        return True

    @discord.ui.button(label='Create content poll', style=discord.ButtonStyle.success, row=3)
    async def create(self, interaction, button):
        try:
            choices = choices_from_values([*self.early.values, *self.later.values], self.tiers.values)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            raid_id = await self.cog.create_time_poll(
                interaction.guild_id, interaction.user.id, self.title, self.hours, held=choices)
            event = self.cog.store.event(raid_id)
            await interaction.edit_original_response(content=f"Created <#{event['channel_id']}>", view=None)
        except (ValueError, discord.HTTPException) as exc:
            await self.cog.report_create_failure(interaction, exc, kind='poll')


class TimePollModal(discord.ui.Modal):
    def __init__(self, cog, guild_id=None, user_id=None):
        super().__init__(title='Create time poll')
        self.cog = cog
        self.name = discord.ui.TextInput(label='Title', max_length=80)
        self.day = discord.ui.TextInput(label='Day', placeholder='Friday', max_length=64)
        self.timeframe = discord.ui.TextInput(
            label='Timeframe',
            placeholder=time_entry_placeholder(cog.bot, guild_id, user_id, '5 to 10 pm'),
            max_length=80)
        for field in (self.name, self.day, self.timeframe):
            self.add_item(field)

    async def on_submit(self, interaction):
        if not self.cog.raids.calendar_cog.is_raid_leader(interaction.user, interaction.guild):
            await interaction.response.send_message('You no longer have permission to create events.', ephemeral=True)
            return
        try:
            server_tz = self.cog.server_timezone(interaction.guild_id)
            hours, tz = parse_timeframe(str(self.timeframe), server_tz)
            day = parse_poll_day(str(self.day), tz)
            choices = hour_choices(day, hours, tz)
            earliest = min(choice['starts'] for choice in choices)
            if not time.time() + POLL_LEAD < earliest <= time.time() + 31536000:
                raise ValueError('The earliest hour must be more than 24 hours away, and within one year.')
        except (ValueError, commands.BadArgument) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        view = TimePollTemplateView(self.cog, interaction.user.id, str(self.name), choices)
        await interaction.response.send_message(
            'Pick the event this time poll becomes, or choose Content Poll.',
            view=view, ephemeral=True)


async def setup(bot):
    await bot.add_cog(EventsCog(bot))
