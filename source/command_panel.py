"""The four public slash commands: /settings and /watch live on EventsCog; /profile and /commands live here."""
from __future__ import annotations

import time

import discord
from discord.ext import commands

from database import select_one

SPEC_CHOICES = (
    ('Red', 0b001),
    ('Blue', 0b010),
    ('Yellow', 0b100),
    ('Red and Blue', 0b011),
    ('Blue and Yellow', 0b110),
    ('Red and Yellow', 0b101),
    ('Red, Blue and Yellow', 0b111),
    ('Clear specialization', 0b000),
)


def _manage_guild(interaction) -> bool:
    perms = getattr(interaction.user, 'guild_permissions', None)
    return bool(perms and perms.manage_guild)


async def _deny_manage(interaction):
    await interaction.response.send_message('You need Manage Server for that.', ephemeral=True)


class ArgModal(discord.ui.Modal):
    def __init__(self, title: str, fields: list[tuple], done):
        super().__init__(title=title[:45])
        self.done = done
        self.inputs = []
        for label, placeholder, required, maxlen in fields:
            item = discord.ui.TextInput(
                label=label[:45],
                placeholder=(placeholder or '')[:100],
                required=required,
                max_length=maxlen,
            )
            self.inputs.append(item)
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        values = [str(item.value or '').strip() for item in self.inputs]
        try:
            await self.done(interaction, values)
        except Exception as exc:
            await _oops(interaction, exc)


def _modal(title, fields, done):
    return ArgModal(title, fields, done)


async def _oops(interaction, exc):
    text = str(exc) or type(exc).__name__
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


async def _call(cog, name, *args):
    """Call a cog method whether or not it is still an application command."""
    if cog is None:
        raise RuntimeError(f'{name} is not loaded.')
    method = getattr(cog, name)
    if isinstance(method, discord.app_commands.Command):
        await method.callback(cog, *args)
    else:
        await method(*args)


COLOR_CYCLE = (0, 0b001, 0b010, 0b100, 0b011, 0b110, 0b101, 0b111)
TIER_CYCLE = ('T1', 'T2', 'T*')
_ALL_TIERS = 1 + 8 + 64 + 512 + 4096
_COLOR_MASK = (1 << 15) - 1
_TIER_CODE = {'T1': 0, 'T2': 1, 'T*': 2}
_TIER_NAME = {0: 'T1', 1: 'T2', 2: 'T*'}


def _colors(value: int) -> int:
    return int(value or 0) & _COLOR_MASK


def _pack(colors: int, which: str) -> int:
    return _colors(colors) | (_TIER_CODE.get(which, 0) << 15)


def _stored_tier(value: int) -> str:
    return _TIER_NAME.get((int(value or 0) >> 15) & 0b11, 'T1')


def _tier_bits(value: int, which: str) -> int:
    value = _colors(value)
    if which == 'T1':
        return value & 0b111
    if which == 'T2':
        return (value // 8) & 0b111
    masks = [(value // (8 ** tier)) & 0b111 for tier in range(5)]
    return masks[0] if len(set(masks)) == 1 else 0


def _write_bits(value: int, which: str, mask: int) -> int:
    mask &= 0b111
    if which == 'T*':
        return mask * _ALL_TIERS
    mult = 1 if which == 'T1' else 8
    return (value & ~(mult * 0b111)) | (mult * mask)


def _next_mask(mask: int, duo: bool) -> int:
    cycle = [item for item in COLOR_CYCLE if not (duo and item & 0b100)]
    mask &= 0b111
    if mask not in cycle:
        return cycle[0]
    return cycle[(cycle.index(mask) + 1) % len(cycle)]


class ProfileView(discord.ui.LayoutView):
    def __init__(self, cog, user_id: int, guild_id: int | None):
        super().__init__(timeout=900)
        self.cog = cog
        self.user_id = user_id
        self.guild_id = guild_id
        self.names = list(cog.bot.role_names)
        self.values = {name: 0 for name in self.names}
        self.tiers = {name: 'T1' for name in self.names}
        self.timezone = ''
        self.saved = False
        self._load()
        self._build()

    def _load(self):
        if self.names:
            row = select_one(self.cog.bot.conn, 'Specs', self.names, ['player_id'], [self.user_id])
            if row is not None:
                values = row if isinstance(row, tuple) else (row,)
                for name, value in zip(self.names, values):
                    self.values[name] = int(value or 0)
                    self.tiers[name] = _stored_tier(self.values[name])
        personal = select_one(self.cog.bot.conn, 'Timezone', ['timezone'], ['player_id'], [self.user_id])
        self.timezone = personal if isinstance(personal, str) else ''

    def _visible_mask(self, name: str) -> int:
        return _tier_bits(self.values.get(name, 0), self.tiers.get(name, 'T1'))

    def overview(self) -> str:
        lines = ['Saved.'] if self.saved else []
        for name in self.names:
            if not self._visible_mask(name):
                continue
            icon = self._spec_emoji(name)
            shown = str(icon) if icon else name
            lines.append(f'{shown} {self.tiers[name]}')
        return '\n'.join(lines) or 'No classes configured.'

    def _emoji(self, key: str):
        raids = self.cog.bot.get_cog('RaidCog')
        icons = getattr(raids, 'emojis_dict', None) or {}
        text = icons.get(key)
        if isinstance(text, str) and text.startswith('<') and text.endswith('>'):
            return discord.PartialEmoji.from_str(text)
        return None

    def _spec_emoji(self, name: str):
        mask = self._visible_mask(name)
        raids = self.cog.bot.get_cog('RaidCog')
        specs = getattr(raids, 'specs', None)
        if mask and specs and mask < len(specs) and specs[mask]:
            emoji = self._emoji(f'{name}_{specs[mask]}')
            if emoji:
                return emoji
        return self._emoji(name)

    def _button(self, custom_id: str, callback, *, emoji=None, label=None, style=discord.ButtonStyle.secondary):
        kwargs = {'style': style, 'custom_id': custom_id}
        if emoji is not None:
            kwargs['emoji'] = emoji
        else:
            kwargs['label'] = label
        button = discord.ui.Button(**kwargs)
        button.callback = callback
        return button

    def _add_rows(self, buttons):
        for start in range(0, len(buttons), 5):
            row = discord.ui.ActionRow()
            for button in buttons[start:start + 5]:
                row.add_item(button)
            self.add_item(row)

    def _build(self):
        self.clear_items()
        from raid_cog import duo_spec
        duo = set(duo_spec or ())

        def cycle_spec(name):
            async def click(interaction: discord.Interaction):
                which = self.tiers[name]
                mask = _next_mask(_tier_bits(self.values[name], which), name in duo)
                self.values[name] = _pack(_write_bits(_colors(self.values[name]), which, mask), which)
                self.saved = False
                self._build()
                await interaction.response.edit_message(view=self)
            return click

        def cycle_tier(name):
            async def click(interaction: discord.Interaction):
                current = self.tiers[name]
                self.tiers[name] = TIER_CYCLE[(TIER_CYCLE.index(current) + 1) % len(TIER_CYCLE)]
                self.values[name] = _pack(self.values[name], self.tiers[name])
                self.saved = False
                self._build()
                await interaction.response.edit_message(view=self)
            return click

        buttons = []
        for name in self.names:
            buttons.append(self._button(
                f'profile:spec:{name}', cycle_spec(name), emoji=self._spec_emoji(name), label=name,
            ))
        for name in self.names:
            which = self.tiers[name]
            suffix = {'T1': 'T1', 'T2': 'T2', 'T*': 'Tstar'}[which]
            buttons.append(self._button(
                f'profile:tier:{name}', cycle_tier(name), emoji=self._emoji(f'{name}_{suffix}'), label=which,
            ))

        async def save(interaction: discord.Interaction):
            try:
                self._commit()
            except Exception as exc:
                await _oops(interaction, exc)
                return
            self.saved = True
            self._build()
            await interaction.response.edit_message(view=self)
            await self._refresh_events()

        buttons.append(self._button(
            'profile:save', save, label='Save', style=discord.ButtonStyle.success,
        ))
        self.add_item(discord.ui.TextDisplay(self.overview()))
        self._add_timezone()
        self._add_rows(buttons)

    def _add_timezone(self):
        from events_cog import COMMON_TIMEZONES
        current = self.timezone
        zones = list(COMMON_TIMEZONES[:23])
        if current and current not in zones:
            zones = [current, *zones[:22]]
        options = [discord.SelectOption(label='Use server timezone', value='__server__', default=not current)]
        options.extend(discord.SelectOption(label=zone, value=zone, default=zone == current) for zone in zones)
        zone_select = discord.ui.Select(
            custom_id='profile:timezone', placeholder='Personal timezone', options=options[:25],
        )

        async def pick_zone(interaction: discord.Interaction):
            value = zone_select.values[0]
            self.timezone = '' if value == '__server__' else value
            self.saved = False
            self._build()
            await interaction.response.edit_message(view=self)

        zone_select.callback = pick_zone
        row = discord.ui.ActionRow()
        row.add_item(zone_select)
        self.add_item(row)

    def _commit(self):
        from database import upsert
        for name in self.names:
            self.values[name] = _pack(self.values[name], self.tiers.get(name, 'T1'))
            upsert(self.cog.bot.conn, 'Specs', [name], [self.values[name]], ['player_id'], [self.user_id])
        upsert(
            self.cog.bot.conn, 'Timezone', ['timezone'],
            [self.timezone or None], ['player_id'], [self.user_id],
        )
        self.cog.bot.conn.commit()

    async def _refresh_events(self):
        raids = self.cog.bot.get_cog('RaidCog')
        events = self.cog.bot.get_cog('EventsCog')
        store = getattr(raids, 'event_store', None)
        if not events or not store:
            return
        import sqlite3
        try:
            rows = self.cog.bot.conn.execute(
                'SELECT raid_id FROM Players WHERE player_id=? AND unavailable=0', (self.user_id,),
            ).fetchall()
        except sqlite3.Error:
            return
        for (raid_id,) in rows:
            if store.event(raid_id):
                store.touch(raid_id)
                await events.refresh(raid_id)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message('This profile is not yours.', ephemeral=True)
            return False
        return True


class CommandPanel(commands.Cog):
    """Ephemeral catalog of every command that is not one of the four slash commands."""

    def __init__(self, bot):
        self.bot = bot
        self.pending_imports: dict[tuple, tuple] = {}

    def catalog(self):
        return (
            ('Setup', (
                ('setup', 'Open the server settings editor.', self._open_settings),
                ('setup-reset', 'Clear this server’s saved setup. Existing events stay.', self._open_reset),
            )),
            ('Events', (
                ('event-share', 'Create the code other servers use to sync this raid.', self._share),
                ('event-share revoke', 'Revoke this raid’s sharing code.', self._share_revoke),
                ('event-sync', 'Sync a shared raid into this server’s forum. Asks for the code.', self._sync),
                ('event-announce', 'Send an announcement to every synchronized thread.', self._announce),
                ('event-duration', 'Set this event’s duration in minutes.', self._duration),
                ('list-raids', 'List the events you have signed up for.', self._list_raids),
                ('list-players', 'List signups for an upcoming raid. Raid leaders only.', self._list_players),
                ('events', 'Show upcoming official LotRO events in your local time.', self._events),
                ('server-time', 'Show the current server time.', self._server_time),
            )),
            ('Network', (
                ('event-status', 'Inspect linked servers and pending deliveries.', self._event_status),
                ('event-status retry', 'Refresh linked copies and retry pending deliveries.', self._event_status_retry),
                ('network-status', 'Inspect this server’s failed event discoveries.', self._network_status),
                ('network-status retry', 'Retry this server’s failed discoveries.', self._network_status_retry),
                ('event-repair', 'Repair a server’s event copy using its sharing code.', self._repair),
                ('event-reconnect', 'Allow a disconnected server to sync again.', self._reconnect),
                ('event-unlink', 'Disconnect a mirror and block it from rejoining.', self._unlink),
            )),
            ('Roster', (
                ('planner-export', 'Open current signups in Vanguard.', self._export),
                ('planner-import', 'Replace the roster from the next JSON file you post here.', self._import),
                ('roster-create', 'Start a shared roster without Vanguard.', self._roster_create),
                ('roster-manage', 'Edit the roster with dropdowns.', self._roster_manage),
                ('roster-show', 'Show the planned roster and revision.', self._roster_show),
                ('roster-edit', 'Replace, remove, or append one roster slot.', self._roster_edit),
                ('roster-swap', 'Swap two planned roster slots.', self._roster_swap),
            )),
            ('Info', self._info_commands()),
        )

    def _info_commands(self):
        items = [
            ('help', 'Show the server setup guide.', self._help),
            ('about', 'Show information about this bot.', self._about),
            ('welcome', 'Resend the welcome message.', self._welcome),
            ('privacy', 'Show what this bot stores.', self._privacy),
        ]
        if self.bot.get_cog('TreasureCog'):
            items.append(('loot', 'Show drop chances for a container.', self._loot))
        if self.bot.get_cog('TwitterCog'):
            items.append(('tweets on', 'Post tweets in this channel.', self._tweets_on))
            items.append(('tweets off', 'Stop posting tweets.', self._tweets_off))
        return tuple(items)

    def commands_for(self, category: str):
        for name, items in self.catalog():
            if name == category:
                return items
        return ()

    @discord.app_commands.command(name='profile', description='Set your class colors, tier, and personal timezone.')
    @discord.app_commands.guild_only()
    async def profile_command(self, interaction: discord.Interaction):
        view = ProfileView(self, interaction.user.id, interaction.guild_id)
        await interaction.response.send_message(view=view, ephemeral=True)

    @discord.app_commands.command(name='commands', description='Open every other Grima command, with a short description.')
    @discord.app_commands.guild_only()
    async def commands_command(self, interaction: discord.Interaction):
        view = CommandsView(self, interaction.user.id)
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)

    async def _open_settings(self, interaction):
        if not _manage_guild(interaction):
            await _deny_manage(interaction)
            return
        await _call(self.bot.get_cog('EventsCog'), 'settings_command', interaction)

    async def _open_reset(self, interaction):
        if not _manage_guild(interaction):
            await _deny_manage(interaction)
            return
        view = ResetConfirm(self, interaction.user.id)
        await interaction.response.send_message(
            'This clears the saved setup for this server. Existing events, rosters, and forum posts stay.',
            view=view,
            ephemeral=True,
        )

    async def _share(self, interaction):
        await _call(self.bot.get_cog('EventsCog'), 'share_command', interaction, False)

    async def _share_revoke(self, interaction):
        await _call(self.bot.get_cog('EventsCog'), 'share_command', interaction, True)

    async def _sync(self, interaction):
        async def done(i, values):
            await _call(self.bot.get_cog('EventsCog'), 'sync_command', i, values[0])
        await interaction.response.send_modal(_modal('Sync a raid', [('Share code', 'mirrorkey code', True, 80)], done))

    async def _announce(self, interaction):
        async def done(i, values):
            await _call(self.bot.get_cog('EventsCog'), 'announce_command', i, values[0])
        await interaction.response.send_modal(_modal(
            'Announce', [('Message', 'Sent to every linked thread', True, 1800)], done))

    async def _duration(self, interaction):
        async def done(i, values):
            try:
                minutes = int(values[0])
            except ValueError:
                await _oops(i, 'Duration must be a number of minutes from 15 to 1440.')
                return
            if not 15 <= minutes <= 1440:
                await _oops(i, 'Duration must be a number of minutes from 15 to 1440.')
                return
            await _call(self.bot.get_cog('EventsCog'), 'duration_command', i, minutes)
        await interaction.response.send_modal(_modal('Event duration', [('Minutes', '15–1440', True, 4)], done))

    async def _list_raids(self, interaction):
        await _call(self.bot.get_cog('RaidCog'), 'list_raids_respond', interaction)

    async def _list_players(self, interaction):
        async def done(i, values):
            try:
                number = int(values[0] or '1')
                cutoff = int(values[1] or '24')
            except ValueError:
                await _oops(i, 'Raid number and cut-off hours must be numbers.')
                return
            await _call(self.bot.get_cog('RaidCog'), 'list_respond', i, number, cutoff)
        await interaction.response.send_modal(_modal(
            'List players',
            [('Raid number', '1 is the next raid', False, 4), ('Cut-off hours', 'Defaults to 24', False, 4)],
            done,
        ))

    async def _events(self, interaction):
        await _call(self.bot.get_cog('CalendarCog'), 'events_respond', interaction)

    async def _server_time(self, interaction):
        await _call(self.bot.get_cog('TimeCog'), 'server_time_respond', interaction)

    async def _event_status(self, interaction):
        await _call(self.bot.get_cog('NetworkCog'), 'status', interaction, False)

    async def _event_status_retry(self, interaction):
        await _call(self.bot.get_cog('NetworkCog'), 'status', interaction, True)

    async def _network_status(self, interaction):
        if not _manage_guild(interaction):
            await _deny_manage(interaction)
            return
        await _call(self.bot.get_cog('NetworkCog'), 'network_status', interaction, False)

    async def _network_status_retry(self, interaction):
        if not _manage_guild(interaction):
            await _deny_manage(interaction)
            return
        await _call(self.bot.get_cog('NetworkCog'), 'network_status', interaction, True)

    async def _repair(self, interaction):
        if not _manage_guild(interaction):
            await _deny_manage(interaction)
            return
        async def done(i, values):
            if not _manage_guild(i):
                await _deny_manage(i)
                return
            await _call(self.bot.get_cog('NetworkCog'), 'repair', i, values[0])
        await interaction.response.send_modal(_modal('Repair event', [('Share code', '', True, 80)], done))

    async def _reconnect(self, interaction):
        async def done(i, values):
            await _call(self.bot.get_cog('NetworkCog'), 'reconnect', i, values[0])
        await interaction.response.send_modal(_modal('Reconnect server', [('Server ID', 'Numeric guild id', True, 24)], done))

    async def _unlink(self, interaction):
        async def done(i, values):
            await _call(self.bot.get_cog('NetworkCog'), 'unlink', i, values[0])
        await interaction.response.send_modal(_modal('Unlink server', [('Server ID', 'Numeric guild id', True, 24)], done))

    async def _export(self, interaction):
        await _call(self.bot.get_cog('PlanningCog'), 'export_command', interaction)

    async def _import(self, interaction):
        async def done(i, values):
            try:
                revision = int(values[0])
            except ValueError:
                await _oops(i, 'Expected revision must be a number. Use 0 for the first import.')
                return
            if revision < 0:
                await _oops(i, 'Expected revision must be 0 or greater.')
                return
            if i.channel_id is None:
                await _oops(i, 'Open /commands inside the event post, then upload the JSON file there.')
                return
            self.pending_imports[(i.guild_id, i.channel_id, i.user.id)] = (revision, time.time() + 300)
            await i.response.send_message(
                'Post the Vanguard session JSON file in this event thread within 5 minutes. '
                'It replaces the planned roster.',
                ephemeral=True,
            )
        await interaction.response.send_modal(_modal(
            'Import roster', [('Expected revision', '0 for the first import', True, 8)], done))

    async def _roster_create(self, interaction):
        await _call(self.bot.get_cog('PlanningCog'), 'create_roster', interaction)

    async def _roster_manage(self, interaction):
        await _call(self.bot.get_cog('PlanningCog'), 'manage_command', interaction)

    async def _roster_show(self, interaction):
        await _call(self.bot.get_cog('PlanningCog'), 'show_command', interaction)

    async def _roster_edit(self, interaction):
        async def done(i, values):
            try:
                revision = int(values[0])
                team = int(values[1])
                slot = int(values[2])
            except ValueError:
                await _oops(i, 'Revision, team, and slot must be numbers.')
                return
            player = values[3] or None
            class_name = spec = None
            if values[4]:
                parts = values[4].split()
                if len(parts) < 2:
                    await _oops(i, 'Class and spec look like `Guardian Red`. Leave the field blank to remove the slot.')
                    return
                class_name, spec = parts[0], parts[1]
            try:
                await _call(
                    self.bot.get_cog('PlanningCog'), 'edit_command',
                    i, revision, team, slot, player, class_name, spec)
            except ValueError as exc:
                await _oops(i, exc)
        await interaction.response.send_modal(_modal(
            'Edit roster slot',
            [
                ('Expected revision', 'Current revision', True, 8),
                ('Team', 'Starts at 1', True, 2),
                ('Slot', 'Starts at 1', True, 2),
                ('Player id', 'Blank removes the slot', False, 32),
                ('Class and spec', 'Guardian Red — blank with no player removes', False, 40),
            ],
            done,
        ))

    async def _roster_swap(self, interaction):
        async def done(i, values):
            try:
                numbers = [int(value) for value in values]
            except ValueError:
                await _oops(i, 'Revision, teams, and slots must be numbers.')
                return
            try:
                await _call(self.bot.get_cog('PlanningCog'), 'swap_command', i, *numbers)
            except ValueError as exc:
                await _oops(i, exc)
        await interaction.response.send_modal(_modal(
            'Swap roster slots',
            [
                ('Expected revision', '', True, 8),
                ('Team A', 'Starts at 1', True, 2),
                ('Slot A', 'Starts at 1', True, 2),
                ('Team B', 'Starts at 1', True, 2),
                ('Slot B', 'Starts at 1', True, 2),
            ],
            done,
        ))

    async def _help(self, interaction):
        await _call(self.bot.get_cog('ConfigCog'), 'help_respond', interaction, None, False)

    async def _about(self, interaction):
        await _call(self.bot.get_cog('ConfigCog'), 'about_respond', interaction)

    async def _welcome(self, interaction):
        await _call(self.bot.get_cog('ConfigCog'), 'welcome_respond', interaction)

    async def _privacy(self, interaction):
        await _call(self.bot.get_cog('ConfigCog'), 'privacy_respond', interaction)

    async def _loot(self, interaction):
        async def done(i, values):
            from raid_cog import Classes
            chest = values[0]
            try:
                classes = Classes[values[1] or 'Captain']
            except KeyError:
                await _oops(i, 'Class must be one of the configured class names, such as Captain.')
                return
            try:
                level = int(values[2] or '160')
            except ValueError:
                await _oops(i, 'Level must be a number from 1 to 160.')
                return
            tracery = (values[3] or '').lower() in ('1', 'true', 'yes', 'y')
            await _call(self.bot.get_cog('TreasureCog'), 'loot_respond', i, chest, classes, level, tracery)
        await interaction.response.send_modal(_modal(
            'Loot table',
            [
                ('Container', 'Chest name', True, 80),
                ('Class', 'Captain', False, 32),
                ('Level', '160', False, 3),
                ('Tracery', 'yes to expand the tracery list', False, 5),
            ],
            done,
        ))

    async def _tweets_on(self, interaction):
        await _call(self.bot.get_cog('TwitterCog'), 'tweets_on', interaction)

    async def _tweets_off(self, interaction):
        await _call(self.bot.get_cog('TwitterCog'), 'tweets_off', interaction)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None or not message.attachments:
            return
        key = (message.guild.id, message.channel.id, message.author.id)
        pending = self.pending_imports.get(key)
        if not pending:
            return
        revision, expires = pending
        if time.time() > expires:
            self.pending_imports.pop(key, None)
            await message.reply('That import expired. Open /commands and choose planner-import again.', mention_author=False)
            return
        attachment = next((item for item in message.attachments if item.filename.lower().endswith('.json')), None)
        if attachment is None:
            await message.reply('Post the Vanguard session as a .json file. This import is still waiting.', mention_author=False)
            return
        self.pending_imports.pop(key, None)
        planning = self.bot.get_cog('PlanningCog')
        adapter = _MessageCall(message)
        try:
            await _call(planning, 'import_command', adapter, attachment, revision)
        except Exception as exc:
            await message.reply(str(exc) or 'Import failed.', mention_author=False)


class _MessageCall:
    """Lets planner-import run from a posted file, where there is no slash interaction."""

    def __init__(self, message: discord.Message):
        self.message = message
        self.channel = message.channel
        self.channel_id = message.channel.id
        self.guild = message.guild
        self.guild_id = message.guild.id
        self.user = message.author
        self.response = self
        self.followup = self
        self.sent = False

    def is_done(self):
        return self.sent

    async def defer(self, ephemeral=True):
        self.sent = True

    async def send_message(self, content=None, **kwargs):
        self.sent = True
        await self.message.reply(content or 'Done.', mention_author=False)

    async def send(self, content=None, **kwargs):
        self.sent = True
        await self.message.reply(content or 'Done.', mention_author=False)


class ResetConfirm(discord.ui.View):
    def __init__(self, panel: CommandPanel, user_id: int):
        super().__init__(timeout=120)
        self.panel = panel
        self.user_id = user_id

    @discord.ui.button(label='Clear saved setup', style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message('This confirmation is not yours.', ephemeral=True)
            return
        if not _manage_guild(interaction):
            await _deny_manage(interaction)
            return
        await interaction.response.defer(ephemeral=True)
        notes = await self.panel.bot.get_cog('EventsCog').reset_guild_setup(interaction.guild)
        lines = [
            'Saved setup for this server was cleared. Existing events were left in place.',
            'Run /settings to configure it again.',
        ]
        if notes:
            lines.extend(notes)
        await interaction.followup.send('\n'.join(lines)[:1900], ephemeral=True)
        self.stop()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return True


class CommandsView(discord.ui.View):
    def __init__(self, panel: CommandPanel, user_id: int):
        super().__init__(timeout=900)
        self.panel = panel
        self.user_id = user_id
        self.category = panel.catalog()[0][0]
        self.reload()

    def embed(self) -> discord.Embed:
        lines = [f'**{name}** — {description}' for name, description, _run in self.panel.commands_for(self.category)]
        return discord.Embed(
            title='Commands',
            description=(
                f'**{self.category}**\n'
                'Event and roster commands have to be opened inside the event post.\n\n'
                + '\n'.join(lines)
            )[:4096],
            colour=discord.Colour(0x3498db),
        )

    def reload(self):
        self.clear_items()
        categories = [name for name, _items in self.panel.catalog()]
        category = discord.ui.Select(
            row=0,
            placeholder='Category',
            options=[discord.SelectOption(label=name, value=name, default=name == self.category) for name in categories],
        )

        async def pick_category(interaction: discord.Interaction):
            self.category = category.values[0]
            self.reload()
            await interaction.response.edit_message(embed=self.embed(), view=self)

        category.callback = pick_category
        self.add_item(category)

        items = self.panel.commands_for(self.category)
        command = discord.ui.Select(
            row=1,
            placeholder='Command',
            options=[
                discord.SelectOption(label=name[:100], value=name, description=description[:100])
                for name, description, _run in items
            ],
        )

        async def pick_command(interaction: discord.Interaction):
            name = command.values[0]
            runner = next(run for item_name, _description, run in items if item_name == name)
            try:
                await runner(interaction)
            except Exception as exc:
                await _oops(interaction, exc)

        command.callback = pick_command
        self.add_item(command)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message('This command panel is not yours.', ephemeral=True)
            return False
        return True


async def setup(bot):
    await bot.add_cog(CommandPanel(bot))
