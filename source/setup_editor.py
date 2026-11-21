"""Paged ephemeral editor for /settings."""
from __future__ import annotations

import discord

with open('common_timezones.txt', 'r') as f:
    COMMON_TIMEZONES = f.read().splitlines()[:25]

DESCRIPTION_TEMPLATE_EXAMPLE = (
    "Post a message like this in a channel, then paste its link in **Set description template…**:\n"
    "```\n"
    "Requirements: voice in Discord, consumables ready.\n"
    "When: {start} · Ends: {end}\n"
    "Aim: {description}\n"
    "Signups so far: {signup_count}\n"
    "Questions: ping {organizer}\n"
    "```\n"
    "Placeholders: `{title}` `{tier}` `{description}` `{start}` `{end}` `{organizer}` `{signup_count}` "
    "(`{description}` is the per-event Aim from creation)."
)


class SetupDraft:
    __slots__ = (
        'raid_forum_id', 'announcement_id', 'panel_id', 'voice_category_id',
        'instance_forum_id', 't1_forum_id', 't2_forum_id', 't3_forum_id',
        't4_forum_id', 't5_forum_id', 'leader_role_id', 'kin_role_id',
        'calendar_id', 'rss_id', 'discord_events', 'watch_sync', 'kin_icon', 'server_timezone',
        'server_invite_url', 'duration_minutes', 'lock_delay_minutes',
        'description_template_preview', 'description_template',
    )

    def __init__(self):
        self.raid_forum_id = 0
        self.announcement_id = 0
        self.panel_id = 0
        self.voice_category_id = 0
        self.instance_forum_id = 0
        self.t1_forum_id = 0
        self.t2_forum_id = 0
        self.t3_forum_id = 0
        self.t4_forum_id = 0
        self.t5_forum_id = 0
        self.leader_role_id = 0
        self.kin_role_id = 0
        self.calendar_id = 0
        self.rss_id = 0
        self.discord_events = False
        self.watch_sync = True
        self.kin_icon = ''
        self.server_timezone = ''
        self.server_invite_url = ''
        self.duration_minutes = 180
        self.lock_delay_minutes = 60
        self.description_template_preview = ''
        self.description_template = None


def load_setup_draft(store, conn, guild_id) -> SetupDraft:
    draft = SetupDraft()
    ev = store.settings(guild_id)
    if ev:
        draft.raid_forum_id = ev.get('forum_id') or 0
        draft.voice_category_id = ev.get('voice_category_id') or 0
        draft.instance_forum_id = ev.get('instance_forum_id') or 0
        draft.t1_forum_id = ev.get('t1_forum_id') or 0
        draft.t2_forum_id = ev.get('t2_forum_id') or 0
        draft.t3_forum_id = ev.get('t3_forum_id') or 0
        draft.t4_forum_id = ev.get('t4_forum_id') or 0
        draft.t5_forum_id = ev.get('t5_forum_id') or 0
        draft.duration_minutes = max(15, (ev.get('duration') or 10800) // 60)
        draft.lock_delay_minutes = max(0, (ev.get('lock_delay') or 3600) // 60)
        if ev.get('watch_sync') is not None:
            draft.watch_sync = bool(ev['watch_sync'])
    net = store.one('SELECT push_id FROM NetworkSettings WHERE guild_id=?', (guild_id,))
    if net and net.get('push_id'):
        draft.announcement_id = net['push_id']
    panels = store.rows('SELECT channel_id FROM CreationPanels WHERE guild_id=? LIMIT 1', (guild_id,))
    if panels:
        draft.panel_id = panels[0]['channel_id']
    row = store.one(
        'SELECT raid_leader, priority, icon, rss, server, guild_events, calendar FROM Settings WHERE guild_id=?',
        (guild_id,),
    )
    if row:
        draft.leader_role_id = row.get('raid_leader') or 0
        draft.kin_role_id = row.get('priority') or 0
        draft.kin_icon = row.get('icon') or ''
        draft.rss_id = row.get('rss') or 0
        draft.server_timezone = row.get('server') or ''
        draft.discord_events = bool(row.get('guild_events'))
        cal = row.get('calendar')
        if cal and '/' in str(cal):
            draft.calendar_id = int(str(cal).split('/')[0])
    invite = store.one('SELECT url FROM SourceInvites WHERE guild_id=?', (guild_id,))
    if invite and invite.get('url'):
        draft.server_invite_url = invite['url']
    desc = store.one("SELECT body FROM EventTemplates WHERE guild_id=? AND kind='description'", (guild_id,))
    if not desc:
        desc = store.one("SELECT body FROM EventTemplates WHERE guild_id=? AND kind='footer'", (guild_id,))
    if desc and desc.get('body'):
        draft.description_template_preview = desc['body']
    return draft


class SetupTextModal(discord.ui.Modal, title='Setup text options'):
    kin_icon = discord.ui.TextInput(label='Kin icon (emoji)', required=False, max_length=80)
    server_timezone = discord.ui.TextInput(label='Server timezone (IANA)', required=False, max_length=64)
    server_invite_url = discord.ui.TextInput(label='Server invite URL', required=False, max_length=200)
    duration_minutes = discord.ui.TextInput(label='Default duration (minutes)', required=False, max_length=4)
    lock_delay_minutes = discord.ui.TextInput(label='Lock delay (minutes)', required=False, max_length=5)

    def __init__(self, editor: 'SetupEditorView'):
        super().__init__()
        self.editor = editor
        self.kin_icon.default = editor.draft.kin_icon or ''
        self.server_timezone.default = editor.draft.server_timezone or ''
        self.server_invite_url.default = editor.draft.server_invite_url or ''
        self.duration_minutes.default = str(editor.draft.duration_minutes)
        self.lock_delay_minutes.default = str(editor.draft.lock_delay_minutes)

    async def on_submit(self, interaction: discord.Interaction):
        self.editor.draft.kin_icon = str(self.kin_icon.value or '').strip()
        self.editor.draft.server_timezone = str(self.server_timezone.value or '').strip()
        self.editor.draft.server_invite_url = str(self.server_invite_url.value or '').strip()
        try:
            if str(self.duration_minutes.value).strip():
                self.editor.draft.duration_minutes = int(str(self.duration_minutes.value).strip())
            if str(self.lock_delay_minutes.value).strip():
                self.editor.draft.lock_delay_minutes = int(str(self.lock_delay_minutes.value).strip())
        except ValueError:
            await interaction.response.send_message('Duration and lock delay must be numbers.', ephemeral=True)
            return
        try:
            errors, states = await self.editor.cog.probe_text_settings(
                interaction.guild,
                self.editor.draft.server_timezone,
                self.editor.draft.kin_icon,
                self.editor.draft.server_invite_url,
                self.editor.draft.duration_minutes,
                self.editor.draft.lock_delay_minutes,
            )
        except Exception as exc:
            errors, states = [f'**Text fields:** {type(exc).__name__}: {exc}'], {}
        self.editor.probes.update(states)
        self.editor.reload()
        await interaction.response.edit_message(embed=self.editor.embed(), view=self.editor)
        if errors:
            await interaction.followup.send('\n'.join(errors), ephemeral=True)


class SetupDescriptionTemplateModal(discord.ui.Modal, title='Event description template'):
    message_link = discord.ui.TextInput(
        label='Discord message link',
        placeholder='https://discord.com/channels/… (same server)',
        required=True,
        max_length=200,
    )

    def __init__(self, editor: 'SetupEditorView'):
        super().__init__()
        self.editor = editor

    async def on_submit(self, interaction: discord.Interaction):
        try:
            body = await self.editor.cog.resolve_template_from_link(interaction.guild, self.message_link.value)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        self.editor.draft.description_template = body
        self.editor.draft.description_template_preview = body
        self.editor.probes['description_template'] = 'ok'
        self.editor.reload()
        await interaction.response.edit_message(
            embed=self.editor.embed(),
            view=self.editor,
        )


class SetupEditorView(discord.ui.View):
    REQUIRED = (
        ('raid_forum_id', 'Raid forum'),
        ('announcement_id', 'Announcement push'),
        ('panel_id', 'Event panels'),
    )
    PAGE_TITLES = (
        'Channels',
        'Optional forums (1/2)',
        'Optional forums & watch',
        'Optional roles & feeds',
        'Optional flags & text',
    )

    def missing_required(self):
        return [label for field, label in self.REQUIRED if not getattr(self.draft, field)]

    def required_ready(self):
        return not self.missing_required()

    def __init__(self, cog, user_id: int, draft: SetupDraft, guild_id: int | None = None):
        super().__init__(timeout=900)
        self.cog = cog
        self.user_id = user_id
        self.guild_id = guild_id
        self.draft = draft
        self.page = 0
        self.probes = {}
        self.reload()

    def embed(self) -> discord.Embed:
        d = self.draft
        extra = (
            f"Timezone: `{d.server_timezone or 'default'}` · Duration: {d.duration_minutes}m · "
            f"Lock: {d.lock_delay_minutes}m · Calendar event: {'on' if d.discord_events else 'off'}"
        )
        missing = self.missing_required()
        if missing:
            gate = '**Save** stays hidden until every required channel is set.\nMissing: ' + ', '.join(missing)
        else:
            gate = 'Required channels are set. **Save** checks permissions and writes them. Panels repost only if the panel channel changes.'
        embed = discord.Embed(
            title='Server setup',
            description=(
                f"**{self.PAGE_TITLES[self.page]}** (page {self.page + 1}/{len(self.PAGE_TITLES)})\n"
                f"{extra}\nWatch sync: {'create raid posts' if d.watch_sync else 'calendar only'}\n\n{gate}"
            ),
            colour=discord.Colour(0x3498db),
        )
        checked = self._checked_lines()
        if checked:
            embed.add_field(name='Checked', value='\n'.join(checked)[:1024], inline=False)
        if self.page == 2 and self.guild_id:
            rows = self.cog.store.rows(
                'SELECT source_channel_id FROM EventWatches WHERE guild_id=? ORDER BY source_channel_id',
                (self.guild_id,),
            )
            mentions = [f"<#{row['source_channel_id']}>" for row in rows]
            shown = []
            used = 0
            for mention in mentions:
                piece = (', ' if shown else '') + mention
                if used + len(piece) > 800:
                    break
                shown.append(mention)
                used += len(piece)
            body = ', '.join(shown) if shown else 'None yet.'
            hidden = len(mentions) - len(shown)
            if hidden:
                body += f' (+{hidden} more)'
            body += '\nAdd one here. Use `/watch` to add or remove any number.'
            embed.add_field(name=f'Watch channels ({len(mentions)})', value=body, inline=False)
        if self.page == len(self.PAGE_TITLES) - 1:
            preview = d.description_template_preview.strip()
            if preview:
                snippet = preview if len(preview) <= 200 else preview[:197] + '…'
                embed.add_field(name='Saved template', value=snippet, inline=False)
            else:
                embed.add_field(name='Saved template', value='*(not set)*', inline=False)
            embed.add_field(name='How to write the template message', value=DESCRIPTION_TEMPLATE_EXAMPLE, inline=False)
        return embed

    def reload(self):
        self.clear_items()
        if self.page == 0:
            self._forum_select(0, 'Raid forum (required)', 'raid_forum_id', refresh=True, kind='raid_forum')
            self._text_select(1, 'Announcement push (required)', 'announcement_id', refresh=True, kind='announcement')
            self._text_select(2, 'Event panels (required)', 'panel_id', refresh=True, kind='panel')
            self._category_select(3, 'Voice category (optional)', 'voice_category_id', refresh=True)
        elif self.page == 1:
            self._forum_select(0, 'Instance forum (optional)', 'instance_forum_id', optional=True)
            self._forum_select(1, 'T1 forum (optional)', 't1_forum_id', optional=True)
            self._forum_select(2, 'T2 forum (optional)', 't2_forum_id', optional=True)
            self._forum_select(3, 'T3 forum (optional)', 't3_forum_id', optional=True)
        elif self.page == 2:
            self._forum_select(0, 'T4 forum (optional)', 't4_forum_id', optional=True)
            self._forum_select(1, 'T5 forum (optional)', 't5_forum_id', optional=True)
            self._watch_sync_select(2)
            self._watch_add_select(3)
        elif self.page == 3:
            self._role_select(0, 'Raid leader role', 'leader_role_id')
            self._role_select(1, 'Kinship role', 'kin_role_id')
            self._text_select(2, 'Calendar (optional)', 'calendar_id', optional=True, kind='calendar')
            self._text_select(3, 'RSS (optional)', 'rss_id', optional=True, kind='rss')
        else:
            self._events_select(0)
            self._timezone_select(3)
            set_btn = discord.ui.Button(label='Set description template…', style=discord.ButtonStyle.primary, row=1)
            set_btn.callback = self.open_description_modal
            self.add_item(set_btn)
            clear_btn = discord.ui.Button(label='Clear description template', style=discord.ButtonStyle.danger, row=1)
            clear_btn.callback = self.clear_description_template
            self.add_item(clear_btn)
            btn = discord.ui.Button(label='Edit text fields…', style=discord.ButtonStyle.secondary, row=2)
            btn.callback = self.open_text_modal
            self.add_item(btn)
        self._nav_row()

    def _set_id(self, field: str, value: str):
        setattr(self.draft, field, int(value) if value and value != '0' else 0)

    def _marked(self, field, label):
        state = self.probes.get(field)
        if state == 'ok':
            return f'✅ {label}'[:150]
        if state == 'bad':
            return f'⚠️ {label}'[:150]
        return label

    def _checked_lines(self):
        names = {
            'raid_forum_id': 'Raid forum',
            'announcement_id': 'Announcement push',
            'panel_id': 'Event panels',
            'voice_category_id': 'Voice category',
            'instance_forum_id': 'Instance forum',
            't1_forum_id': 'T1 forum',
            't2_forum_id': 'T2 forum',
            't3_forum_id': 'T3 forum',
            't4_forum_id': 'T4 forum',
            't5_forum_id': 'T5 forum',
            'leader_role_id': 'Raid leader role',
            'kin_role_id': 'Kinship role',
            'calendar_id': 'Calendar',
            'rss_id': 'RSS',
            'discord_events': 'Calendar event',
            'server_timezone': 'Server timezone',
            'watch_sync': 'Watch sync',
            'watch_channel': 'Watch channel',
            'kin_icon': 'Kin icon',
            'server_invite_url': 'Server invite',
            'description_template': 'Description template',
        }
        lines = []
        for field, label in names.items():
            state = self.probes.get(field)
            if state == 'ok':
                lines.append(f'✅ {label}')
            elif state == 'bad':
                lines.append(f'⚠️ {label}')
        return lines

    async def _apply_channel(self, interaction, field, kind, channel, optional):
        if channel is None and optional:
            self._set_id(field, '0')
            self.probes.pop(field, None)
            errors, notes = [], []
        else:
            self._set_id(field, str(channel.id))
            try:
                errors, notes = await self.cog.probe_setup_channel(interaction.guild, channel, kind)
            except Exception as exc:
                errors, notes = [f'**{field}:** {type(exc).__name__}: {exc}'], []
            self.probes[field] = 'bad' if errors else 'ok'
        self.reload()
        await interaction.response.edit_message(embed=self.embed(), view=self)
        if errors or notes:
            await interaction.followup.send('\n'.join(errors + notes), ephemeral=True)

    def _forum_select(self, row, label, field, optional=False, refresh=False, kind='forum'):
        control = discord.ui.ChannelSelect(
            row=row,
            placeholder=self._marked(field, label),
            channel_types=[discord.ChannelType.forum],
            min_values=0 if optional else 1,
            max_values=1,
        )
        current = getattr(self.draft, field)
        if current:
            control.default_values = [discord.Object(id=current)]

        async def chosen(i: discord.Interaction):
            channel = control.values[0] if control.values else None
            if channel is None and not optional:
                await i.response.defer()
                return
            await self._apply_channel(i, field, kind, channel, optional)

        control.callback = chosen
        self.add_item(control)

    def _text_select(self, row, label, field, optional=False, refresh=False, kind='announcement'):
        control = discord.ui.ChannelSelect(
            row=row,
            placeholder=self._marked(field, label),
            channel_types=[discord.ChannelType.text, discord.ChannelType.news],
            min_values=0 if optional else 1,
            max_values=1,
        )
        current = getattr(self.draft, field)
        if current:
            control.default_values = [discord.Object(id=current)]

        async def chosen(i: discord.Interaction):
            channel = control.values[0] if control.values else None
            if channel is None and not optional:
                await i.response.defer()
                return
            await self._apply_channel(i, field, kind, channel, optional)

        control.callback = chosen
        self.add_item(control)

    def _category_select(self, row, label, field, refresh=False):
        control = discord.ui.ChannelSelect(
            row=row,
            placeholder=self._marked(field, label),
            channel_types=[discord.ChannelType.category],
            min_values=0,
            max_values=1,
        )
        current = getattr(self.draft, field)
        if current:
            control.default_values = [discord.Object(id=current)]

        async def chosen(i: discord.Interaction):
            channel = control.values[0] if control.values else None
            await self._apply_channel(i, field, 'voice', channel, optional=True)

        control.callback = chosen
        self.add_item(control)

    def _watch_sync_select(self, row):
        options = [
            discord.SelectOption(
                label='Create raid posts from watched channels',
                value='1',
                default=bool(self.draft.watch_sync),
            ),
            discord.SelectOption(
                label='Calendar only — do not create raid posts',
                value='0',
                default=not self.draft.watch_sync,
            ),
        ]
        control = discord.ui.Select(row=row, placeholder=self._marked('watch_sync', 'Watch sync (optional)'), options=options)

        async def chosen(i: discord.Interaction):
            self.draft.watch_sync = control.values[0] == '1'
            for option in control.options:
                option.default = option.value == control.values[0]
            self.probes['watch_sync'] = 'ok'
            self.reload()
            await i.response.edit_message(embed=self.embed(), view=self)

        control.callback = chosen
        self.add_item(control)

    def _watch_add_select(self, row):
        control = discord.ui.ChannelSelect(
            row=row,
            placeholder=self._marked('watch_channel', 'Watch channel (optional)'),
            channel_types=[discord.ChannelType.text, discord.ChannelType.news, discord.ChannelType.public_thread],
            min_values=1,
            max_values=1,
        )

        async def chosen(i: discord.Interaction):
            channel = control.values[0] if control.values else None
            if channel is None:
                await i.response.defer()
                return
            errors, added, kept = await self.cog.add_watch_channels(i.guild, [channel])
            self.probes['watch_channel'] = 'bad' if errors else 'ok'
            self.reload()
            await i.response.edit_message(embed=self.embed(), view=self)
            if errors:
                await i.followup.send('\n'.join(errors), ephemeral=True)

        control.callback = chosen
        self.add_item(control)

    def _role_select(self, row, label, field):
        control = discord.ui.RoleSelect(row=row, placeholder=self._marked(field, label), min_values=0, max_values=1)
        current = getattr(self.draft, field)
        if current:
            control.default_values = [discord.Object(id=current)]

        async def chosen(i: discord.Interaction):
            if control.values:
                self._set_id(field, str(control.values[0].id))
                self.probes[field] = 'ok'
            else:
                self._set_id(field, '0')
                self.probes.pop(field, None)
            self.reload()
            await i.response.edit_message(embed=self.embed(), view=self)

        control.callback = chosen
        self.add_item(control)

    def _events_select(self, row):
        options = [
            discord.SelectOption(label='Calendar event off', value='0', default=not self.draft.discord_events),
            discord.SelectOption(label='Calendar event on (needs a calendar channel)', value='1', default=self.draft.discord_events),
        ]
        control = discord.ui.Select(row=row, placeholder=self._marked('discord_events', 'Discord calendar event'), options=options)

        async def chosen(i: discord.Interaction):
            self.draft.discord_events = control.values[0] == '1'
            if self.draft.discord_events:
                try:
                    errors = self.cog.probe_manage_events(i.guild)
                except Exception as exc:
                    errors = [f'**Calendar event:** {type(exc).__name__}: {exc}']
                self.probes['discord_events'] = 'bad' if errors else 'ok'
            else:
                errors = []
                self.probes['discord_events'] = 'ok'
            for option in control.options:
                option.default = option.value == control.values[0]
            self.reload()
            await i.response.edit_message(embed=self.embed(), view=self)
            if errors:
                await i.followup.send('\n'.join(errors), ephemeral=True)

        control.callback = chosen
        self.add_item(control)

    def _timezone_select(self, row):
        current = self.draft.server_timezone
        zones = list(COMMON_TIMEZONES)
        if current and current not in zones:
            zones = [current, *zones[:24]]
        options = [discord.SelectOption(label=tz, value=tz, default=tz == current) for tz in zones]
        control = discord.ui.Select(
            row=row,
            placeholder=self._marked('server_timezone', 'Server timezone (calendar days, "server" times)'),
            options=options,
        )

        async def chosen(i: discord.Interaction):
            self.draft.server_timezone = control.values[0]
            self.probes['server_timezone'] = 'ok'
            for option in control.options:
                option.default = option.value == control.values[0]
            self.reload()
            await i.response.edit_message(embed=self.embed(), view=self)

        control.callback = chosen
        self.add_item(control)

    def _nav_row(self):
        back = discord.ui.Button(label='Back', style=discord.ButtonStyle.secondary, row=4, disabled=self.page == 0)
        nxt = discord.ui.Button(label='Next', style=discord.ButtonStyle.primary, row=4, disabled=self.page >= len(self.PAGE_TITLES) - 1)
        save = discord.ui.Button(label='Save', style=discord.ButtonStyle.success, row=4)

        async def go_back(i: discord.Interaction):
            self.page -= 1
            self.reload()
            await i.response.edit_message(embed=self.embed(), view=self)

        async def go_next(i: discord.Interaction):
            self.page += 1
            self.reload()
            await i.response.edit_message(embed=self.embed(), view=self)

        async def do_save(i: discord.Interaction):
            await i.response.defer(ephemeral=True)
            try:
                kwargs = await self.cog.resolve_setup_draft(i.guild, self.draft)
                ok, result_embed = await self.cog.smoke_test_and_apply_setup(interaction=i, **kwargs)
            except ValueError as exc:
                ok = False
                result_embed = self.cog.setup_failure_embed(str(exc).splitlines())
            except Exception as exc:
                ok = False
                result_embed = self.cog.setup_failure_embed([f'**Setup stopped:** {type(exc).__name__}: {exc}'])
            try:
                await i.edit_original_response(embed=result_embed, view=None if ok else self)
            except discord.HTTPException:
                await i.followup.send(embed=result_embed, ephemeral=True)
            if ok:
                self.stop()

        back.callback = go_back
        nxt.callback = go_next
        self.add_item(back)
        self.add_item(nxt)
        if self.required_ready():
            save.callback = do_save
            self.add_item(save)

    async def open_text_modal(self, interaction: discord.Interaction):
        await interaction.response.send_modal(SetupTextModal(self))

    async def open_description_modal(self, interaction: discord.Interaction):
        await interaction.response.send_modal(SetupDescriptionTemplateModal(self))

    async def clear_description_template(self, interaction: discord.Interaction):
        self.draft.description_template = ''
        self.draft.description_template_preview = ''
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message('This setup editor is not yours.', ephemeral=True)
            return False
        return True


WATCH_PAGE = 25
WATCH_TYPES = (discord.ChannelType.text, discord.ChannelType.news, discord.ChannelType.public_thread)


class WatchListView(discord.ui.View):
    """Add and remove mirrorkey watch channels. Changes apply immediately."""

    def __init__(self, cog, guild, user_id: int):
        super().__init__(timeout=900)
        self.cog = cog
        self.guild = guild
        self.user_id = user_id
        self.page = 0
        self.notice = ''
        self.reload()

    def embed(self) -> discord.Embed:
        rows = self.cog.watch_rows(self.guild.id)
        embed = discord.Embed(
            title='Watch channels',
            description=(
                'Gríma reads these channels for `mirrorkey:` codes.\n'
                'The add menu accepts up to 25 channels at a time — use it again to add more. '
                'The remove menu lists the current channels, 25 per page.'
            ),
            colour=discord.Colour(0x3498db),
        )
        pages = max(1, (len(rows) + WATCH_PAGE - 1) // WATCH_PAGE)
        page = min(self.page, pages - 1)
        visible = rows[page * WATCH_PAGE:(page + 1) * WATCH_PAGE]
        if not rows:
            listing = 'No channels yet.'
        else:
            listing = '\n'.join(f"• <#{row['source_channel_id']}>" for row in visible)
            if pages > 1:
                listing += f'\nPage {page + 1} of {pages}. Use Next and Previous to see the rest.'
        embed.add_field(name=f'Watching ({len(rows)})', value=listing[:1024], inline=False)
        if self.notice:
            embed.add_field(name='Latest change', value=self.notice[:1024], inline=False)
        return embed

    def reload(self):
        rows = self.cog.watch_rows(self.guild.id)
        pages = max(1, (len(rows) + WATCH_PAGE - 1) // WATCH_PAGE)
        self.page = min(self.page, pages - 1)
        self.clear_items()
        self._add_select()
        start = self.page * WATCH_PAGE
        visible = rows[start:start + WATCH_PAGE]
        if visible:
            self._remove_select(visible)
        if rows:
            self._action_row(pages)

    def _channel_label(self, channel_id: int) -> str:
        getter = getattr(self.guild, 'get_channel_or_thread', None) or getattr(self.guild, 'get_channel', None)
        channel = getter(channel_id) if getter else None
        name = getattr(channel, 'name', None)
        if name:
            return f'#{name}'[:100]
        return str(channel_id)

    def _add_select(self):
        control = discord.ui.ChannelSelect(
            row=0,
            placeholder='Add channels (up to 25)',
            channel_types=list(WATCH_TYPES),
            min_values=1,
            max_values=25,
        )

        async def chosen(interaction: discord.Interaction):
            errors, added, kept = await self.cog.add_watch_channels(self.guild, list(control.values))
            lines = list(errors)
            if added:
                lines.append('Added ' + ', '.join(channel.mention for channel in added) + '.')
            if kept:
                lines.append('Already watching ' + ', '.join(channel.mention for channel in kept) + '.')
            self.notice = '\n'.join(lines)
            self.reload()
            await interaction.response.edit_message(embed=self.embed(), view=self)

        control.callback = chosen
        self.add_item(control)

    def _remove_select(self, rows):
        options = [
            discord.SelectOption(label=self._channel_label(row['source_channel_id']), value=str(row['source_channel_id']))
            for row in rows
        ]
        control = discord.ui.Select(
            row=1,
            placeholder='Remove channels' if self.page == 0 else f'Remove channels (page {self.page + 1})',
            min_values=1,
            max_values=len(options),
            options=options,
        )

        async def chosen(interaction: discord.Interaction):
            removed = self.cog.remove_watch_channels(self.guild.id, [int(value) for value in control.values])
            self.notice = 'Removed ' + ', '.join(f'<#{channel_id}>' for channel_id in removed) + '.' if removed else 'Those channels were not on the list.'
            self.reload()
            await interaction.response.edit_message(embed=self.embed(), view=self)

        control.callback = chosen
        self.add_item(control)

    def _action_row(self, pages: int):
        if pages > 1:
            back = discord.ui.Button(label='Previous', style=discord.ButtonStyle.secondary, row=2, disabled=self.page == 0)
            nxt = discord.ui.Button(label='Next', style=discord.ButtonStyle.primary, row=2, disabled=self.page >= pages - 1)

            async def go_back(interaction: discord.Interaction):
                self.page -= 1
                self.notice = ''
                self.reload()
                await interaction.response.edit_message(embed=self.embed(), view=self)

            async def go_next(interaction: discord.Interaction):
                self.page += 1
                self.notice = ''
                self.reload()
                await interaction.response.edit_message(embed=self.embed(), view=self)

            back.callback = go_back
            nxt.callback = go_next
            self.add_item(back)
            self.add_item(nxt)

        clear = discord.ui.Button(label='Remove all', style=discord.ButtonStyle.danger, row=2)

        async def clear_all(interaction: discord.Interaction):
            count = self.cog.clear_watch_channels(self.guild.id)
            self.page = 0
            self.notice = f'Stopped watching {count} channel(s).'
            self.reload()
            await interaction.response.edit_message(embed=self.embed(), view=self)

        clear.callback = clear_all
        self.add_item(clear)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message('This watch list is not yours.', ephemeral=True)
            return False
        return True
