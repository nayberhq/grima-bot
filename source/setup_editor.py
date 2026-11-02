"""Paged ephemeral editor for /settings."""
from __future__ import annotations

import discord


class SetupDraft:
    __slots__ = (
        'raid_forum_id', 'announcement_id', 'panel_id', 'voice_category_id',
        'forum_mode', 'instance_forum_id', 't1_forum_id', 't2_forum_id', 't3_forum_id',
        't4_forum_id', 't5_forum_id', 'watch_channel_id', 'leader_role_id', 'kin_role_id',
        'calendar_id', 'rss_id', 'discord_events', 'kin_icon', 'server_timezone',
        'server_invite_url', 'duration_minutes', 'lock_delay_minutes',
    )

    def __init__(self):
        self.raid_forum_id = 0
        self.announcement_id = 0
        self.panel_id = 0
        self.voice_category_id = 0
        self.forum_mode = 'single'
        self.instance_forum_id = 0
        self.t1_forum_id = 0
        self.t2_forum_id = 0
        self.t3_forum_id = 0
        self.t4_forum_id = 0
        self.t5_forum_id = 0
        self.watch_channel_id = 0
        self.leader_role_id = 0
        self.kin_role_id = 0
        self.calendar_id = 0
        self.rss_id = 0
        self.discord_events = False
        self.kin_icon = ''
        self.server_timezone = ''
        self.server_invite_url = ''
        self.duration_minutes = 180
        self.lock_delay_minutes = 60


def load_setup_draft(store, conn, guild_id) -> SetupDraft:
    draft = SetupDraft()
    ev = store.settings(guild_id)
    if ev:
        draft.raid_forum_id = ev.get('forum_id') or 0
        draft.voice_category_id = ev.get('voice_category_id') or 0
        draft.forum_mode = ev.get('forum_mode') or 'single'
        draft.instance_forum_id = ev.get('instance_forum_id') or 0
        draft.t1_forum_id = ev.get('t1_forum_id') or 0
        draft.t2_forum_id = ev.get('t2_forum_id') or 0
        draft.t3_forum_id = ev.get('t3_forum_id') or 0
        draft.t4_forum_id = ev.get('t4_forum_id') or 0
        draft.t5_forum_id = ev.get('t5_forum_id') or 0
        draft.duration_minutes = max(15, (ev.get('duration') or 10800) // 60)
        draft.lock_delay_minutes = max(0, (ev.get('lock_delay') or 3600) // 60)
    net = store.one('SELECT push_id FROM NetworkSettings WHERE guild_id=?', (guild_id,))
    if net and net.get('push_id'):
        draft.announcement_id = net['push_id']
    panels = store.rows('SELECT channel_id FROM CreationPanels WHERE guild_id=? LIMIT 1', (guild_id,))
    if panels:
        draft.panel_id = panels[0]['channel_id']
    watch = store.one('SELECT source_channel_id FROM EventWatches WHERE guild_id=?', (guild_id,))
    if watch:
        draft.watch_channel_id = watch['source_channel_id']
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
        await interaction.response.edit_message(
            embed=self.editor.embed(),
            view=self.editor,
        )


class SetupEditorView(discord.ui.View):
    PAGE_TITLES = (
        'Core channels',
        'Forum routing (1/2)',
        'Forum routing (2/2) & watch',
        'Roles & feeds',
        'Flags & text',
    )

    def __init__(self, cog, user_id: int, draft: SetupDraft):
        super().__init__(timeout=900)
        self.cog = cog
        self.user_id = user_id
        self.draft = draft
        self.page = 0
        self.reload()

    def embed(self) -> discord.Embed:
        d = self.draft
        extra = (
            f"Timezone: `{d.server_timezone or 'default'}` · Duration: {d.duration_minutes}m · "
            f"Lock: {d.lock_delay_minutes}m · Discord events: {'on' if d.discord_events else 'off'}"
        )
        return discord.Embed(
            title='Server setup',
            description=(
                f"**{self.PAGE_TITLES[self.page]}** (page {self.page + 1}/{len(self.PAGE_TITLES)})\n"
                f"{extra}\n\nChoose values, then **Save** on any page. Panels repost only if the panel channel changes."
            ),
            colour=discord.Colour(0x3498db),
        )

    def reload(self):
        self.clear_items()
        if self.page == 0:
            self._forum_select(0, 'Raid forum', 'raid_forum_id')
            self._text_select(1, 'Announcement push', 'announcement_id')
            self._text_select(2, 'Event panels', 'panel_id')
            self._category_select(3, 'Voice category', 'voice_category_id')
        elif self.page == 1:
            self._mode_select(0)
            self._forum_select(1, 'Instance forum (optional)', 'instance_forum_id', optional=True)
            self._forum_select(2, 'T1 forum (optional)', 't1_forum_id', optional=True)
            self._forum_select(3, 'T2 forum (optional)', 't2_forum_id', optional=True)
        elif self.page == 2:
            self._forum_select(0, 'T3 forum (optional)', 't3_forum_id', optional=True)
            self._forum_select(1, 'T4 forum (optional)', 't4_forum_id', optional=True)
            self._forum_select(2, 'T5 forum (optional)', 't5_forum_id', optional=True)
            self._watch_select(3)
        elif self.page == 3:
            self._role_select(0, 'Raid leader role', 'leader_role_id')
            self._role_select(1, 'Kinship role', 'kin_role_id')
            self._text_select(2, 'Calendar (optional)', 'calendar_id', optional=True)
            self._text_select(3, 'RSS (optional)', 'rss_id', optional=True)
        else:
            self._events_select(0)
            btn = discord.ui.Button(label='Edit text fields…', style=discord.ButtonStyle.secondary, row=1)
            btn.callback = self.open_text_modal
            self.add_item(btn)
        self._nav_row()

    def _set_id(self, field: str, value: str):
        setattr(self.draft, field, int(value) if value and value != '0' else 0)

    def _forum_select(self, row, label, field, optional=False):
        control = discord.ui.ChannelSelect(
            row=row,
            placeholder=label,
            channel_types=[discord.ChannelType.forum],
            min_values=0 if optional else 1,
            max_values=1,
        )
        current = getattr(self.draft, field)
        if current:
            control.default_values = [discord.Object(id=current)]

        async def chosen(i: discord.Interaction):
            if control.values:
                self._set_id(field, str(control.values[0].id))
            elif optional:
                self._set_id(field, '0')
            await i.response.defer()

        control.callback = chosen
        self.add_item(control)

    def _text_select(self, row, label, field, optional=False):
        control = discord.ui.ChannelSelect(
            row=row,
            placeholder=label,
            channel_types=[discord.ChannelType.text, discord.ChannelType.news],
            min_values=0 if optional else 1,
            max_values=1,
        )
        current = getattr(self.draft, field)
        if current:
            control.default_values = [discord.Object(id=current)]

        async def chosen(i: discord.Interaction):
            if control.values:
                self._set_id(field, str(control.values[0].id))
            elif optional:
                self._set_id(field, '0')
            await i.response.defer()

        control.callback = chosen
        self.add_item(control)

    def _category_select(self, row, label, field):
        control = discord.ui.ChannelSelect(
            row=row,
            placeholder=label,
            channel_types=[discord.ChannelType.category],
            min_values=1,
            max_values=1,
        )
        current = getattr(self.draft, field)
        if current:
            control.default_values = [discord.Object(id=current)]

        async def chosen(i: discord.Interaction):
            if control.values:
                self._set_id(field, str(control.values[0].id))
            await i.response.defer()

        control.callback = chosen
        self.add_item(control)

    def _watch_select(self, row):
        control = discord.ui.ChannelSelect(
            row=row,
            placeholder='Watch channel (optional)',
            channel_types=[discord.ChannelType.text, discord.ChannelType.news, discord.ChannelType.public_thread],
            min_values=0,
            max_values=1,
        )
        if self.draft.watch_channel_id:
            control.default_values = [discord.Object(id=self.draft.watch_channel_id)]

        async def chosen(i: discord.Interaction):
            if control.values:
                self._set_id('watch_channel_id', str(control.values[0].id))
            else:
                self._set_id('watch_channel_id', '0')
            await i.response.defer()

        control.callback = chosen
        self.add_item(control)

    def _role_select(self, row, label, field):
        control = discord.ui.RoleSelect(row=row, placeholder=label, min_values=0, max_values=1)
        current = getattr(self.draft, field)
        if current:
            control.default_values = [discord.Object(id=current)]

        async def chosen(i: discord.Interaction):
            if control.values:
                self._set_id(field, str(control.values[0].id))
            else:
                self._set_id(field, '0')
            await i.response.defer()

        control.callback = chosen
        self.add_item(control)

    def _mode_select(self, row):
        options = [
            discord.SelectOption(label='Single channel', value='single', default=self.draft.forum_mode == 'single'),
            discord.SelectOption(label='Instance + raid', value='instance_raid', default=self.draft.forum_mode == 'instance_raid'),
            discord.SelectOption(label='Tiers', value='tiers', default=self.draft.forum_mode == 'tiers'),
        ]
        control = discord.ui.Select(row=row, placeholder='Forum layout mode', options=options)

        async def chosen(i: discord.Interaction):
            self.draft.forum_mode = control.values[0]
            await i.response.defer()

        control.callback = chosen
        self.add_item(control)

    def _events_select(self, row):
        options = [
            discord.SelectOption(label='Discord scheduled events off', value='0', default=not self.draft.discord_events),
            discord.SelectOption(label='Discord scheduled events on', value='1', default=self.draft.discord_events),
        ]
        control = discord.ui.Select(row=row, placeholder='Native Discord events', options=options)

        async def chosen(i: discord.Interaction):
            self.draft.discord_events = control.values[0] == '1'
            await i.response.defer()

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
            except ValueError as exc:
                await i.followup.send(str(exc), ephemeral=True)
                return
            ok, result_embed = await self.cog.smoke_test_and_apply_setup(interaction=i, **kwargs)
            await i.edit_original_response(embed=result_embed, view=None if ok else self)
            if ok:
                self.stop()

        back.callback = go_back
        nxt.callback = go_next
        save.callback = do_save
        self.add_item(back)
        self.add_item(nxt)
        self.add_item(save)

    async def open_text_modal(self, interaction: discord.Interaction):
        await interaction.response.send_modal(SetupTextModal(self))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message('This setup editor is not yours.', ephemeral=True)
            return False
        return True
