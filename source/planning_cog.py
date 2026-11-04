"""Templates and durable, directly synchronized Vanguard rosters."""
import asyncio
import hashlib
import io
import json
import logging
import time
from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands, tasks
from planning import CLASSES, context_for, render_template, export_session, import_plan, planner_link, PLANNER_URL, decode_plan_code

log = logging.getLogger(__name__)


class PlanningCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.lock = asyncio.Lock()
        from durable_send import DurableSender
        self.sender=DurableSender(bot.conn,bot)
        bot.conn.executescript('''
            CREATE TABLE IF NOT EXISTS LegacyMigration(raid_id INTEGER PRIMARY KEY,error TEXT);
            CREATE TABLE IF NOT EXISTS RosterDelivery(thread_id INTEGER PRIMARY KEY,last_success INTEGER,error TEXT);
            CREATE TABLE IF NOT EXISTS EventTemplates (
                guild_id INTEGER, kind TEXT, body TEXT NOT NULL, PRIMARY KEY(guild_id,kind));
            CREATE TABLE IF NOT EXISTS EventPlans (
                raid_id INTEGER PRIMARY KEY, revision INTEGER NOT NULL, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS PlanMessages (
                thread_id INTEGER, page INTEGER, message_id INTEGER, body TEXT NOT NULL,
                PRIMARY KEY(thread_id,page));
        ''')
        bot.conn.commit()

    @property
    def events(self):
        return self.bot.get_cog('EventsCog')

    @property
    def store(self):
        return self.events.store

    async def cog_load(self):
        self.migrate_legacy()
        self.refresh_plans.start()

    def migrate_legacy(self):
        # Never overwrite an existing plan or delete the legacy source rows.
        for row in self.store.rows('SELECT raid_id FROM SharedEvents WHERE raid_id NOT IN (SELECT raid_id FROM EventPlans)'):
            raid_id=row['raid_id'];event=self.store.event(raid_id)
            assigned=self.store.rows('SELECT * FROM Assignment WHERE raid_id=? AND player_id IS NOT NULL ORDER BY slot_id',(raid_id,))
            if not assigned:continue
            try:
                members=[]
                for a in assigned:
                    c=next((c for c in CLASSES if c.replace('-','').lower()==(a['class_name'] or '').replace('-','').lower()),None)
                    spec={1:'Red',2:'Blue',4:'Yellow'}.get(a['spec'])
                    role=dict(CLASSES.get(c,[])).get(spec)
                    if not role:raise ValueError('Legacy assignment has unspecified/ambiguous class or specialization; manual migration required.')
                    members.append(dict(id=str(a['player_id']),name=a['byname'] or str(a['player_id']),c=c,spec=spec,role=role))
                ids=[m['id'] for m in members]
                if len(ids)!=len(set(ids)) or len(ids)>event['size']:raise ValueError('Legacy lineup contains duplicate players or exceeds event size.')
                self.store.write('INSERT OR IGNORE INTO EventPlans VALUES (?,1,?)',(raid_id,json.dumps([dict(label=event['name'],members=members)])))
                self.store.write('DELETE FROM LegacyMigration WHERE raid_id=?',(raid_id,))
            except ValueError as exc:
                self.store.write('INSERT OR REPLACE INTO LegacyMigration VALUES (?,?)',(raid_id,str(exc)))

    async def cog_unload(self):
        self.refresh_plans.cancel()

    def players(self, raid_id):
        return self.store.rows('SELECT * FROM Players WHERE raid_id=? AND unavailable=0 ORDER BY timestamp,player_id', (raid_id,))

    def specs(self):
        return {r['player_id']:r for r in self.store.rows('SELECT * FROM Specs')}

    def template(self, event, kind):
        return self.store.one('SELECT body FROM EventTemplates WHERE guild_id=? AND kind=?', (event['guild_id'], kind))

    def render(self, event, body, limit=1800):
        return render_template(body, context_for(event, len(self.players(event['raid_id']))), limit)

    def plan(self, raid_id):
        return self.store.one('SELECT * FROM EventPlans WHERE raid_id=?', (raid_id,))

    def save(self, event, teams, expected):
        current = self.plan(event['raid_id'])
        if (current['revision'] if current else 0) != expected:
            raise ValueError('Roster changed. Use /roster-show to get the latest revision, then retry.')
        latest = self.store.event(event['raid_id'])
        if not latest or self.store.lock_due(latest, time.time()):
            raise ValueError('This event is closed.')
        self.store.write('INSERT OR REPLACE INTO EventPlans VALUES (?,?,?)',
                         (event['raid_id'], expected+1, json.dumps(teams)))
        return expected+1

    def cards(self, event, plan):
        signed = {str(p['player_id']):p for p in self.players(event['raid_id'])}
        raids = self.bot.get_cog('RaidCog')
        icons = getattr(raids, 'emojis_dict', {})
        roles = {'Tank':'🛡️', 'Healer':'💚', 'DPS':'⚔️', 'Buffer':'✨', 'Debuffer':'🌀'}
        cards = []
        for n, team in enumerate(json.loads(plan['body']), 1):
            members = team['members']
            embed = discord.Embed(title=f"Run {n} · {team['label']}"[:256], color=0xA82D42,
                                  description=f"**{len(members)}/{event['size']} players**" +
                                  (f" · Planner time: {team['time']:g}h" if team.get('time') is not None else ''))
            sizes = team.get('fellowship_sizes')
            if not sizes or sum(sizes) != len(members):
                sizes = [min(6,len(members)-i) for i in range(0,len(members),6)] or [0]
            start = 0
            for group, size in enumerate(sizes, 1):
                lines = []
                for slot, player in enumerate(members[start:start+size], start+1):
                    status = ' — ⚠ LEFT SIGNUPS' if player['id'] not in signed else ''
                    if player['id'] in signed and not any(v for k,v in signed[player['id']].items() if k.replace('-','').lower()==player['c'].replace('-','').lower()):
                        status = ' — ⚠ CLASS WITHDRAWN'
                    class_name = player['c'].replace('-', '')
                    class_icon = icons.get(class_name+'_'+player['spec']) or icons.get(player['c']+'_'+player['spec'])
                    if not class_icon:
                        class_icon = f"{icons.get(class_name) or icons.get(player['c']) or player['c']} ({player['spec']})"
                    lines.append(f"{slot}. {roles.get(player['role'],'•')} {class_icon} <@{player['id']}>{status}")
                embed.add_field(name=f"Fellowship {group}", value='\n'.join(lines) or 'No players assigned', inline=True)
                start += size
            embed.set_footer(text=f"Revision {plan['revision']} · 🛡️ Tank  💚 Healer  ⚔️ DPS  ✨ Buffer  🌀 Debuffer")
            cards.append(embed)
        return cards

    def pages(self, event, plan):
        return ['\n'.join([card.title, card.description]+[f"{f.name}\n{f.value}" for f in card.fields]) for card in self.cards(event, plan)]

    INLINE_CARD_LIMIT = 9
    INLINE_CHAR_BUDGET = 4000

    def split_cards(self, event, plan):
        """Split run cards into those shown on the event post and overflow messages."""
        cards = self.cards(event, plan) if plan else []
        inline, total = [], 0
        for index, card in enumerate(cards):
            if len(inline) >= self.INLINE_CARD_LIMIT or total + len(card) > self.INLINE_CHAR_BUDGET:
                return inline, cards[index:]
            inline.append(card)
            total += len(card)
        return inline, []

    async def sync(self, raid_id):
        if not self.store.event(raid_id):
            return
        self.store.touch(raid_id)
        await self.events.refresh(raid_id)
        await self.sync_overflow(raid_id)

    async def sync_overflow(self, raid_id):
        async with self.lock:
            event, plan = self.store.event(raid_id), self.plan(raid_id)
            if not event:
                return
            inline, overflow = self.split_cards(event, plan)
            offset = len(inline)
            for copy in self.store.copies(raid_id):
                if copy['locked']:
                    continue
                try:
                    thread = await self.events.channel(copy['thread_id'])
                    if getattr(thread, 'locked', False):
                        continue
                    if thread.archived:
                        await thread.edit(archived=False)
                    old = {r['page']:r for r in self.store.rows('SELECT * FROM PlanMessages WHERE thread_id=?', (copy['thread_id'],))}
                    if not overflow and not old:
                        continue
                    for index, card in enumerate(overflow, offset):
                        body = json.dumps(card.to_dict(), sort_keys=True)
                        key=f"roster:{copy['thread_id']}:{index}:{hashlib.sha256(body.encode()).hexdigest()}"
                        row = old.pop(index, None)
                        if row and row['body'] == body:
                            try:
                                await thread.fetch_message(row['message_id'])
                                continue
                            except discord.NotFound:
                                self.store.write('DELETE FROM SendIntents WHERE key=?',(key,))
                                row=None
                        message = None
                        if row:
                            try:
                                await thread.get_partial_message(row['message_id']).edit(content=None, embed=card, allowed_mentions=discord.AllowedMentions.none())
                                message = row['message_id']
                            except discord.NotFound:
                                self.store.write('DELETE FROM SendIntents WHERE key=?',(key,))
                        if message is None:
                            sent = await self.sender.send(thread,key,None,embed=card)
                            message = sent.id
                        self.store.write('INSERT OR REPLACE INTO PlanMessages VALUES (?,?,?,?)', (copy['thread_id'], index, message, body))
                    for index, row in old.items():
                        try:
                            await thread.get_partial_message(row['message_id']).delete()
                        except discord.NotFound:
                            pass
                        self.store.write('DELETE FROM PlanMessages WHERE thread_id=? AND page=?', (copy['thread_id'], index))
                    self.store.write('INSERT OR REPLACE INTO RosterDelivery VALUES (?,?,NULL)',(copy['thread_id'],int(time.time())))
                except discord.HTTPException as exc:
                    self.store.write('INSERT INTO RosterDelivery VALUES (?,NULL,?) ON CONFLICT(thread_id) DO UPDATE SET error=excluded.error',(copy['thread_id'],str(exc)))
                    log.exception('Roster delivery failed; retrying next cycle')

    @tasks.loop(seconds=30)
    async def refresh_plans(self):
        for row in self.store.rows('SELECT raid_id FROM EventPlans'):
            try:
                await self.sync_overflow(row['raid_id'])
            except Exception:
                log.exception('Roster refresh failed; continuing other events')

    @refresh_plans.before_loop
    async def ready(self):
        await self.bot.wait_until_ready()

    def raid_planner_url(self, raid_id):
        event = self.store.event(raid_id)
        if not event or event.get('creep'):
            return PLANNER_URL
        try:
            data = export_session(event, self.players(raid_id), self.specs())
            return planner_link(data, max_length=512)
        except Exception:
            return PLANNER_URL

    async def require_raid_leader(self, interaction, raid_id):
        raids = self.bot.get_cog('RaidCog')
        if raids and await raids.has_raid_leader_role(interaction.user, interaction.guild, raid_id):
            return True
        await interaction.response.send_message('Import requires the source server’s configured raid leader role.', ephemeral=True)
        return False

    async def open_import_modal(self, interaction, raid_id=None):
        if raid_id is None:
            event = await self.events.command_event(interaction)
            if not event:
                return
            raid_id = event['raid_id']
        else:
            event = self.store.event(raid_id)
            if not event:
                await interaction.response.send_message('Use this button inside an event forum post.', ephemeral=True)
                return
            if not await self.events.can_manage(interaction, raid_id):
                await interaction.response.send_message('Only the organizer or source raid leader can import.', ephemeral=True)
                return
        if not await self.require_raid_leader(interaction, raid_id):
            return
        plan = self.plan(raid_id)
        await interaction.response.send_modal(PlanCodeModal(self, raid_id, plan['revision'] if plan else 0))

    async def open_planner(self, interaction):
        event = await self.events.command_event(interaction, manage=False)
        if not event:
            return
        try:
            specs = {r['player_id']:r for r in self.store.rows('SELECT * FROM Specs')}
            data = export_session(event, self.players(event['raid_id']), specs)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        url = planner_link(data)
        if url:
            await interaction.response.send_message(f"[📋 Open Raid Planner]({url})", ephemeral=True, suppress_embeds=True)
        else:
            files = [discord.File(io.BytesIO(json.dumps(data).encode()), filename=f"grima-{event['raid_id']}.json")]
            long_url = planner_link(data, max_length=20000)
            instructions = 'Import the attached session JSON.'
            if long_url:
                files.append(discord.File(io.BytesIO(long_url.encode()), filename='planner-link.txt'))
                instructions = 'This roster exceeds Discord’s link limit. Open the URL in planner-link.txt, paste it into the planner URL field, or import the attached JSON.'
            await interaction.response.send_message(
                f"[📋 Open Raid Planner]({PLANNER_URL}) — {instructions}",
                files=files,
                ephemeral=True,
                suppress_embeds=True,
            )

    @app_commands.command(name='planner-export', description='Open current signups in Vanguard, with JSON fallback for larger rosters.')
    @app_commands.guild_only()
    async def export_command(self, interaction: discord.Interaction):
        await self.open_planner(interaction)

    @app_commands.command(name='planner-import', description='Replace this event’s planned roster from Vanguard Session JSON.')
    @app_commands.guild_only()
    async def import_command(self, interaction: discord.Interaction, session: discord.Attachment, expected_revision: app_commands.Range[int,0]):
        event = await self.events.command_event(interaction)
        if not event:
            return
        if session.size > 2_000_000:
            raise ValueError('Session must be at most 2 MB.')
        await interaction.response.defer(ephemeral=True)
        try:
            data = json.loads(await session.read())
        except (ValueError, UnicodeError) as exc:
            raise ValueError('Upload valid session JSON.') from exc
        teams = import_plan(data, event, self.players(event['raid_id']),self.specs(),require_identity=True)
        revision = self.save(event, teams, expected_revision)
        await self.sync(event['raid_id'])
        await interaction.followup.send(f'Roster revision {revision} saved ({len(teams)} teams). Linked threads update directly; failed deliveries retry.', ephemeral=True)

    @app_commands.command(name='roster-create',description='Start the authoritative shared roster without using Vanguard.')
    @app_commands.guild_only()
    async def create_roster(self,interaction:discord.Interaction):
        event=await self.events.command_event(interaction)
        if not event:return
        self.save(event,[dict(label=event['name'],members=[])],0)
        await interaction.response.defer(ephemeral=True)
        await self.sync(event['raid_id'])
        await interaction.followup.send('Roster revision 1 created. Add players with /roster-edit.',ephemeral=True)

    async def open_editor(self,interaction):
        event=await self.events.command_event(interaction)
        if not event:return
        if not self.plan(event['raid_id']) and self.store.one('SELECT 1 FROM LegacyMigration WHERE raid_id=?',(event['raid_id'],)):
            await interaction.response.send_message('Legacy assignments need review before creating a replacement. Use /event-status for details; original Assignment rows are retained.',ephemeral=True);return
        if not self.plan(event['raid_id']):
            self.save(event,[dict(label=event['name'],members=[])],0)
        from roster_editor import RosterEditor
        await interaction.response.send_message('Choose team, slot, player and class/spec, then Save.',view=RosterEditor(self,event,interaction.user.id),ephemeral=True)

    @app_commands.command(name='roster-manage',description='Edit the roster with player and slot dropdowns.')
    @app_commands.guild_only()
    async def manage_command(self,interaction:discord.Interaction):
        await self.open_editor(interaction)

    @app_commands.command(name='roster-show', description='Show the current planned roster and revision.')
    @app_commands.guild_only()
    async def show_command(self, interaction: discord.Interaction):
        event = await self.events.command_event(interaction, manage=False)
        if not event:
            return
        plan = self.plan(event['raid_id'])
        if not plan:
            await interaction.response.send_message('No plan yet. Use expected_revision:0 for your first import.', ephemeral=True)
            return
        await interaction.response.send_message(f"Revision {plan['revision']}", file=discord.File(io.BytesIO('\n\n'.join(self.pages(event, plan)).encode()), filename='roster.txt'), ephemeral=True)

    @app_commands.command(name='roster-edit', description='Replace/remove a slot or append a signup; team and slot numbers start at 1.')
    @app_commands.guild_only()
    async def edit_command(self, interaction: discord.Interaction, expected_revision: app_commands.Range[int,1], team: app_commands.Range[int,1,16], slot: app_commands.Range[int,1,25], player_id: str | None = None, class_name: str | None = None, specialization: Literal['Red','Blue','Yellow'] | None = None):
        event = await self.events.command_event(interaction)
        if not event:
            return
        plan = self.plan(event['raid_id'])
        if not plan:
            raise ValueError('Use /roster-create to start a plan or import one from Vanguard.')
        teams = json.loads(plan['body'])
        if team > len(teams) or slot > len(teams[team-1]['members'])+1:
            raise ValueError('Choose an existing slot or the next slot to append.')
        members = teams[team-1]['members']
        if player_id is None:
            if slot > len(members):
                raise ValueError('That slot is empty.')
            members.pop(slot-1)
            teams[team-1]['fellowship_sizes']=None
        else:
            role = dict(CLASSES.get(class_name, [])).get(specialization)
            if not role:
                raise ValueError('Supply a Vanguard class name and supported specialization.')
            member = dict(id=player_id, c=class_name, spec=specialization, role=role)
            checked = import_plan(dict(format='vanguard-raid-planner-session', version=6,
                teams=[dict(label='', members=[member])]), event, self.players(event['raid_id']),self.specs())
            member = checked[0]['members'][0]
            if slot > len(members):
                members.append(member)
                teams[team-1]['fellowship_sizes']=None
            else:
                members[slot-1] = member
        ids = [m['id'] for m in members]
        if len(ids) != len(set(ids)) or len(ids) > event['size']:
            raise ValueError('Duplicate player or team is full. Remove or swap a slot first.')
        revision = self.save(event, teams, expected_revision)
        await interaction.response.defer(ephemeral=True)
        await self.sync(event['raid_id'])
        await interaction.followup.send(f'Roster revision {revision} saved and synchronization queued.', ephemeral=True)

    @app_commands.command(name='roster-swap', description='Swap two planned roster slots, within or between teams.')
    @app_commands.guild_only()
    async def swap_command(self, interaction: discord.Interaction, expected_revision: app_commands.Range[int,1], team_a: app_commands.Range[int,1,16], slot_a: app_commands.Range[int,1,24], team_b: app_commands.Range[int,1,16], slot_b: app_commands.Range[int,1,24]):
        event = await self.events.command_event(interaction)
        if not event:
            return
        plan = self.plan(event['raid_id'])
        if not plan:
            raise ValueError('Use /roster-create to start a plan or import one from Vanguard.')
        teams = json.loads(plan['body'])
        try:
            a, b = teams[team_a-1]['members'], teams[team_b-1]['members']
            a[slot_a-1], b[slot_b-1] = b[slot_b-1], a[slot_a-1]
        except IndexError as exc:
            raise ValueError('Choose existing teams and slots.') from exc
        for members in (a,b):
            ids = [p['id'] for p in members]
            if len(ids) != len(set(ids)):
                raise ValueError('This swap would duplicate a player within a team.')
        revision = self.save(event, teams, expected_revision)
        await interaction.response.defer(ephemeral=True)
        await self.sync(event['raid_id'])
        await interaction.followup.send(f'Roster revision {revision} saved and synchronization queued.', ephemeral=True)

    async def cog_app_command_error(self, interaction, error):
        original = getattr(error, 'original', error)
        message = str(original) if isinstance(original, (ValueError, app_commands.MissingPermissions)) else 'Could not complete command. Check bot logs.'
        if not isinstance(original, ValueError):
            log.error('Planning command failed', exc_info=error)
        sender = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
        await sender(message[:1900], ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


class PlanReturnView(discord.ui.View):
    def __init__(self,cog,raid_id):
        super().__init__(timeout=900)
        self.cog,self.raid_id=cog,raid_id

    @discord.ui.button(label='Import plan code',style=discord.ButtonStyle.primary)
    async def import_code(self,i,button):
        if not await self.cog.require_raid_leader(i,self.raid_id):
            return
        plan=self.cog.plan(self.raid_id)
        await i.response.send_modal(PlanCodeModal(self.cog,self.raid_id,plan['revision'] if plan else 0))

class PlanCodeModal(discord.ui.Modal,title='Import Vanguard plan'):
    code=discord.ui.TextInput(
        label='Paste exported Session JSON or plan code',
        placeholder='Paste JSON or grimaplan:... exported from Raid Planner',
        style=discord.TextStyle.paragraph,
        max_length=4000,
    )
    def __init__(self,cog,raid_id,revision):
        super().__init__();self.cog,self.raid_id,self.revision=cog,raid_id,revision
    async def on_submit(self,i):
        if not await self.cog.require_raid_leader(i,self.raid_id):
            return
        try:
            event=self.cog.store.event(self.raid_id)
            teams=import_plan(decode_plan_code(str(self.code)),event,self.cog.players(self.raid_id),self.cog.specs(),require_identity=False)
            revision=self.cog.save(event,teams,self.revision)
        except ValueError as exc:
            await i.response.send_message(str(exc),ephemeral=True);return
        await i.response.defer(ephemeral=True)
        await self.cog.sync(self.raid_id)
        await i.followup.send(f'Roster revision {revision} synchronized.',ephemeral=True)


async def setup(bot):
    await bot.add_cog(PlanningCog(bot))
