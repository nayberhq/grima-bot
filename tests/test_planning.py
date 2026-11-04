import copy
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'source'))
from planning import export_session, import_plan, render_template, context_for, planner_link, decode_plan_code, compact_signup_bytes
from planning_cog import PlanningCog

EVENT = dict(raid_id=100, guild_id=1, name='Folly', tier='T2', boss='Win', time=1000, duration=3600, size=12, organizer_id=9)
PLAYERS = [dict(player_id=9007199254740993, byname='Alice', Guardian=1)]
TEAM = dict(label='Folly', members=[dict(id=str(PLAYERS[0]['player_id']), c='Guardian', spec='Blue', role='Tank')])

class InterchangeTests(unittest.TestCase):
    def test_planner_link_roundtrip_and_limit(self):
        import base64, zlib
        data=export_session(EVENT, PLAYERS, {})
        token=planner_link(data).split('#grima=')[1]
        unpacked=zlib.decompress(base64.urlsafe_b64decode(token+'='*((-len(token))%4)))
        self.assertEqual(unpacked,compact_signup_bytes(data))
        self.assertTrue(unpacked.startswith(b'GS1'))
        import random,string
        rng=random.Random(1)
        self.assertIsNone(planner_link({'text': ''.join(rng.choices(string.ascii_letters,k=10000))}))

    def test_plan_paste_code_limits(self):
        import base64,zlib
        encode=lambda raw:'grimaplan:'+base64.urlsafe_b64encode(zlib.compress(raw)).decode().rstrip('=')
        data=dict(format='vanguard-raid-planner-session',version=7,teams=[TEAM])
        self.assertEqual(decode_plan_code(encode(json.dumps(data).encode())),data)
        self.assertEqual(decode_plan_code(json.dumps(data)),data)
        self.assertEqual(decode_plan_code(f"```json\n{json.dumps(data)}\n```"),data)
        for code in ('bad', 'grimaplan:bad', '{bad json}', encode(b'x'*2000001)):
            with self.assertRaises(ValueError):decode_plan_code(code)

    def test_identity_and_offered_spec_validation(self):
        data=dict(format='vanguard-raid-planner-session',version=7,teams=[TEAM])
        with self.assertRaises(ValueError):import_plan(data,EVENT,PLAYERS,{},require_identity=True)
        data['grimaEventId']=str(EVENT['raid_id'])
        self.assertTrue(import_plan(data,EVENT,PLAYERS,{},require_identity=True))
        with self.assertRaises(ValueError):import_plan(data,EVENT,PLAYERS,{PLAYERS[0]['player_id']:{'Guardian':1<<3}},require_identity=True)

    def test_templates(self):
        self.assertEqual(render_template('{title} {{literal}} {description} {signup_count}', context_for(EVENT, 3)), 'Folly {literal} Win 3')
        for text in ('{title.__class__}', '{title[0]}', '{title!r}', '{title:>90}', '{}', '{bad}', '{'):
            with self.assertRaises(ValueError):
                render_template(text, context_for(EVENT, 3))
        with self.assertRaises(ValueError):
            render_template('{title}', context_for(EVENT, 3), 2)

    def test_export_spec_bits_and_snowflake(self):
        data = export_session(EVENT, PLAYERS, {PLAYERS[0]['player_id']:dict(Guardian=2 << 3)})
        self.assertEqual(data['players'][0]['id'], '9007199254740993')
        self.assertEqual(data['players'][0]['choices'], [dict(c='Guardian',spec='Blue',role='Tank')])
        self.assertEqual(data['players'][0]['available'], [True])
        self.assertEqual(len(export_session(EVENT, PLAYERS, {})['players'][0]['choices']), 2)

    def test_import_compact_and_full(self):
        for version in (6,7):
            data = dict(format='vanguard-raid-planner-session', version=version, teams=[TEAM])
            teams = import_plan(data, EVENT, PLAYERS)
            self.assertEqual(teams[0]['members'][0]['name'], 'Alice')
            self.assertEqual(teams[0]['members'][0]['id'], '9007199254740993')

    def test_actual_vanguard_compact_export(self):
        data = json.loads((Path(__file__).parent / 'fixtures/vanguard-compact-roundtrip.json').read_text())
        teams = import_plan(data, EVENT, PLAYERS)
        self.assertEqual(teams[0]['members'][0]['id'], '9007199254740993')
        self.assertEqual(teams[0]['members'][0]['c'], 'Guardian')

    def test_explicit_fellowships_are_preserved_and_validated(self):
        team=copy.deepcopy(TEAM);team['grimaFellowships']=[[str(PLAYERS[0]['player_id'])]]
        data=dict(format='vanguard-raid-planner-session',version=7,teams=[team])
        self.assertEqual(import_plan(data,EVENT,PLAYERS)[0]['fellowship_sizes'],[1])
        team['grimaFellowships']=[['unknown']]
        with self.assertRaises(ValueError):import_plan(data,EVENT,PLAYERS)

    def test_invalid_plans(self):
        base = dict(format='vanguard-raid-planner-session', version=7, teams=[TEAM])
        cases = []
        for key,value in [('version',8), ('grimaEventId','101'), ('teams',[]), ('dirty',True)]:
            data=copy.deepcopy(base); data[key]=value; cases.append(data)
        for key,value in [('id','unknown'),('id',9007199254740993),('c','Hunter'),('spec','Yellow'),('role','DPS')]:
            data=copy.deepcopy(base); data['teams'][0]['members'][0][key]=value; cases.append(data)
        data=copy.deepcopy(base);data['teams'][0]['members']*=2;cases.append(data)
        for data in cases:
            with self.assertRaises(ValueError):
                import_plan(data, EVENT, PLAYERS)

class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import sqlite3
        from event_store import EventStore
        self.conn=sqlite3.connect(':memory:')
        self.store=EventStore(self.conn)
        self.store.event=Mock(return_value={**EVENT, 'cancelled':0, 'lock_delay':600, 'time':9999999999})
        self.store.copies=Mock(return_value=[dict(thread_id=1,locked=0),dict(thread_id=2,locked=0),dict(thread_id=3,locked=1)])
        self.channels={i:NS(id=i,archived=False,fetch_message=AsyncMock(),send=AsyncMock(return_value=NS(id=i*10)),get_partial_message=Mock(return_value=NS(edit=AsyncMock(),delete=AsyncMock()))) for i in (1,2,3)}
        self.events=NS(store=self.store,channel=AsyncMock(side_effect=lambda i:self.channels[i]),refresh=AsyncMock())
        self.cog=PlanningCog(NS(conn=self.conn,get_cog=lambda _:self.events))
        self.cog.players=Mock(return_value=PLAYERS)
        self.cog.specs=Mock(return_value={})
        self.event=self.store.event(100)

    def force_overflow(self):
        """Route every card to separate messages so overflow delivery is exercised."""
        cards=self.cog.cards
        self.cog.split_cards=lambda event,plan:([],cards(event,plan) if plan else [])

    async def asyncTearDown(self):
        self.conn.close()

    async def test_inline_cards_refresh_main_post_without_messages(self):
        teams=import_plan(dict(format='vanguard-raid-planner-session',version=7,teams=[TEAM]),EVENT,PLAYERS)
        self.cog.save(self.event,[teams[0],teams[0]],0)
        inline,overflow=self.cog.split_cards(self.event,self.cog.plan(100))
        self.assertEqual((len(inline),len(overflow)),(2,0))
        self.store.write('INSERT INTO PlanMessages VALUES (1,0,555,?)',('old card',))
        await self.cog.sync(100)
        self.events.refresh.assert_awaited_once_with(100)
        for i in (1,2): self.channels[i].send.assert_not_awaited()
        self.channels[1].get_partial_message.return_value.delete.assert_awaited()
        self.assertIsNone(self.store.one('SELECT * FROM PlanMessages WHERE thread_id=1'))

    def test_split_cards_limits(self):
        import discord
        small=[discord.Embed(title=f'Run {n}',description='x') for n in range(12)]
        self.cog.cards=Mock(return_value=small)
        inline,overflow=self.cog.split_cards(self.event,{'body':'[]'})
        self.assertEqual((len(inline),len(overflow)),(9,3))
        big=[discord.Embed(title='Run',description='x'*1500) for _ in range(4)]
        self.cog.cards=Mock(return_value=big)
        inline,overflow=self.cog.split_cards(self.event,{'body':'[]'})
        self.assertEqual((len(inline),len(overflow)),(2,2))

    async def test_revision_and_all_mirrors(self):
        self.force_overflow()
        teams=import_plan(dict(format='vanguard-raid-planner-session',version=7,teams=[TEAM]),EVENT,PLAYERS)
        self.assertEqual(self.cog.save(self.event,teams,0),1)
        with self.assertRaises(ValueError): self.cog.save(self.event,teams,0)
        await self.cog.sync(100)
        for i in (1,2): self.channels[i].send.assert_awaited_once()
        self.channels[3].send.assert_not_awaited()
        await self.cog.sync(100)
        self.channels[1].send.assert_awaited_once()
        self.cog.players.return_value=[]
        await self.cog.sync(100)
        self.assertIn('LEFT SIGNUPS', str(self.channels[1].get_partial_message.return_value.edit.call_args.kwargs['embed'].to_dict()))

    async def test_one_embed_per_run_with_all_groups_and_icons(self):
        self.force_overflow()
        members=[dict(id=str(100+i),name='Player',c='Guardian',spec='Blue',role='Tank') for i in range(24)]
        teams=[dict(label='Run one',members=members,time=0,fellowship_sizes=[6,6,6,6]),dict(label='Run two',members=members[:6],time=1,fellowship_sizes=[6])]
        event={**self.event,'size':24}
        self.cog.save(event,teams,0)
        raids=NS(emojis_dict={'Guardian_Blue':'<:Guardian_Blue:123456789012345678>'})
        original=self.cog.bot.get_cog
        self.cog.bot.get_cog=lambda name:raids if name=='RaidCog' else original(name)
        cards=self.cog.cards(event,self.cog.plan(100))
        self.assertEqual(len(cards),2)
        self.assertEqual([len(c.fields) for c in cards],[4,1])
        for card in cards:
            self.assertLessEqual(len(card),6000)
            for field in card.fields:
                self.assertLessEqual(len(field.value),1024)
                self.assertIn('<:Guardian_Blue:',field.value)
                self.assertIn('🛡️',field.value)
                self.assertFalse(any(square in field.value for square in ('🟥','🟦','🟨')))
        await self.cog.sync(100)
        self.assertEqual(self.channels[1].send.await_count,2)
        self.assertTrue(all('embed' in call.kwargs for call in self.channels[1].send.call_args_list))
        # Moving from old per-group text messages removes obsolete extra posts.
        self.store.write('INSERT INTO PlanMessages VALUES (1,5,555,?)',('old group',))
        await self.cog.sync(100)
        self.channels[1].get_partial_message.return_value.delete.assert_awaited()
        self.assertIsNone(self.store.one('SELECT * FROM PlanMessages WHERE thread_id=1 AND page=5'))

    async def test_failed_delivery_retries(self):
        self.force_overflow()
        import discord
        teams=import_plan(dict(format='vanguard-raid-planner-session',version=7,teams=[TEAM]),EVENT,PLAYERS)
        self.cog.save(self.event,teams,0)
        self.channels[1].send.side_effect=discord.Forbidden(NS(status=403,reason='Forbidden'),'denied')
        with self.assertLogs('planning_cog',level='ERROR'): await self.cog.sync(100)
        self.channels[2].send.assert_awaited_once()
        self.channels[1].send.side_effect=None
        await self.cog.sync(100)
        self.assertEqual(self.channels[1].send.await_count,2)
        self.channels[2].send.assert_awaited_once()

    async def test_restart_uses_saved_messages(self):
        self.force_overflow()
        teams=import_plan(dict(format='vanguard-raid-planner-session',version=7,teams=[TEAM]),EVENT,PLAYERS)
        self.cog.save(self.event,teams,0)
        await self.cog.sync(100)
        restarted=PlanningCog(self.cog.bot)
        restarted.players=self.cog.players
        restarted.split_cards=self.cog.split_cards
        await restarted.sync(100)
        self.channels[1].send.assert_awaited_once()

    async def test_edit_and_authority(self):
        teams=import_plan(dict(format='vanguard-raid-planner-session',version=7,teams=[TEAM]),EVENT,PLAYERS)
        self.cog.save(self.event,teams,0)
        interaction=NS(response=NS(defer=AsyncMock()),followup=NS(send=AsyncMock()))
        self.cog.events.command_event=AsyncMock(return_value=None)
        await PlanningCog.edit_command.callback(self.cog,interaction,1,1,1)
        self.assertEqual(self.cog.plan(100)['revision'],1)
        self.cog.events.command_event.return_value=self.event
        await PlanningCog.edit_command.callback(self.cog,interaction,1,1,1,str(PLAYERS[0]['player_id']),'Guardian','Red')
        self.assertEqual(json.loads(self.cog.plan(100)['body'])[0]['members'][0]['spec'],'Red')
        self.assertEqual(self.cog.plan(100)['revision'],2)
        self.cog.players.return_value=[]
        await PlanningCog.edit_command.callback(self.cog,interaction,2,1,1)
        self.assertEqual(json.loads(self.cog.plan(100)['body'])[0]['members'],[])

    async def test_closed_event_does_not_mutate(self):
        self.store.event.return_value={**self.event,'cancelled':1}
        with self.assertRaises(ValueError): self.cog.save(self.event,[TEAM],0)
        self.assertIsNone(self.cog.plan(100))

    async def test_withdrawn_class_is_flagged(self):
        teams=import_plan(dict(format='vanguard-raid-planner-session',version=7,teams=[TEAM]),EVENT,PLAYERS)
        self.cog.save(self.event,teams,0)
        self.cog.players.return_value=[dict(player_id=PLAYERS[0]['player_id'],byname='Alice',Guardian=0,Hunter=1)]
        self.assertIn('CLASS WITHDRAWN',self.cog.pages(self.event,self.cog.plan(100))[0])

    async def test_deleted_unchanged_roster_is_recreated(self):
        self.force_overflow()
        import discord
        teams=import_plan(dict(format='vanguard-raid-planner-session',version=7,teams=[TEAM]),EVENT,PLAYERS)
        self.cog.save(self.event,teams,0)
        await self.cog.sync(100)
        self.channels[1].fetch_message.side_effect=discord.NotFound(NS(status=404,reason='missing'),'missing')
        await self.cog.sync(100)
        self.assertEqual(self.channels[1].send.await_count,2)

    async def test_editor_supports_all_players_and_specs(self):
        from roster_editor import RosterEditor
        from planning import CLASSES
        players=[dict(player_id=n,byname=f'Player {n}',**{c.replace('-',''):1 for c in CLASSES}) for n in range(31)]
        self.cog.players.return_value=players
        self.cog.save(self.event,[dict(label='Folly',members=[])],0)
        editor=RosterEditor(self.cog,self.event,9)
        editor.player=1;editor.reload()
        self.assertEqual(len(next(c for c in editor.children if c.row==2).options),25)
        editor.page=1;editor.spec_page=1;editor.reload()
        self.assertEqual(len(next(c for c in editor.children if c.row==2).options),6)
        self.assertGreater(len(next(c for c in editor.children if c.row==3).options),0)
        self.assertLessEqual(len(editor.children),9)

    async def test_legacy_migration_preserves_source_and_is_idempotent(self):
        self.conn.execute('CREATE TABLE Assignment(raid_id INTEGER,slot_id INTEGER,player_id INTEGER,byname TEXT,class_name TEXT,spec INTEGER)')
        self.conn.execute('INSERT INTO SharedEvents(raid_id,duration,lock_delay) VALUES (100,3600,600)')
        self.conn.execute("INSERT INTO Assignment VALUES(100,0,99,'Alice','Guardian',2)")
        self.conn.commit()
        self.cog.migrate_legacy();self.cog.migrate_legacy()
        plan=self.cog.plan(100)
        self.assertEqual(plan['revision'],1)
        self.assertEqual(json.loads(plan['body'])[0]['members'][0]['spec'],'Blue')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM Assignment').fetchone()[0],1)

    async def test_ambiguous_legacy_is_flagged_without_guessing(self):
        self.conn.execute('CREATE TABLE Assignment(raid_id INTEGER,slot_id INTEGER,player_id INTEGER,byname TEXT,class_name TEXT,spec INTEGER)')
        self.conn.execute('INSERT INTO SharedEvents(raid_id,duration,lock_delay) VALUES (100,3600,600)')
        self.conn.execute("INSERT INTO Assignment VALUES(100,0,99,'Alice','Guardian',0)")
        self.conn.commit();self.cog.migrate_legacy()
        self.assertIsNone(self.cog.plan(100))
        self.assertIsNotNone(self.store.one('SELECT * FROM LegacyMigration WHERE raid_id=100'))


class PlannerExportLinkTests(unittest.IsolatedAsyncioTestCase):
    async def test_export_uses_authorized_snapshot_link_flow(self):
        cog = NS(open_planner=AsyncMock())
        interaction = object()
        await PlanningCog.export_command.callback(cog, interaction)
        cog.open_planner.assert_awaited_once_with(interaction)

    async def test_raid_view_has_colored_planner_button_and_inbox_modal(self):
        import discord
        import sqlite3
        from event_store import EventStore
        conn = sqlite3.connect(':memory:')
        self.addCleanup(conn.close)
        store = EventStore(conn)
        events = NS(store=store, can_manage=AsyncMock(return_value=True), command_event=AsyncMock())
        bot = NS(conn=conn, get_cog=lambda name: {'EventsCog': events, 'PlanningCog': planning, 'RaidCog': raid_cog}.get(name))
        planning = PlanningCog(bot)
        planning.players = Mock(return_value=PLAYERS)
        planning.specs = Mock(return_value={})
        store.event = Mock(return_value={**EVENT, 'cancelled': 0, 'lock_delay': 600, 'time': 9999999999, 'creep': 0})
        store.resolve = Mock(return_value=100)

        raid_cog = NS(
            bot=bot,
            conn=conn,
            class_emojis=[],
            event_store=store,
            resolve_raid_id=lambda mid: 100,
        )
        from raid_cog import RaidView
        view = RaidView(raid_cog, raid_id=100)
        planner_btn = next(c for c in view.children if c.custom_id == 'raid_view:vanguard')
        self.assertEqual(str(planner_btn.emoji), '\U0001F4CB')
        self.assertIsNone(planner_btn.label)
        self.assertEqual(planner_btn.row, 1)
        self.assertEqual(planner_btn.style, discord.ButtonStyle.primary)
        planning.open_planner = AsyncMock()
        planner_i = NS()
        await planner_btn.callback(planner_i)
        planning.open_planner.assert_awaited_once_with(planner_i)

        inbox_btn = next(c for c in view.children if getattr(c, 'custom_id', None) == 'raid_view:import')
        self.assertEqual(str(inbox_btn.emoji), '\U0001F4E5')
        self.assertIsNone(inbox_btn.label)
        self.assertEqual(inbox_btn.row, 0)

        refresh_btn = next(c for c in view.children if getattr(c, 'custom_id', None) == 'raid_view:refresh')
        self.assertEqual(str(refresh_btn.emoji), '\U0001F504')
        self.assertIsNone(refresh_btn.label)
        self.assertEqual(refresh_btn.row, 0)

        raid_cog.has_raid_leader_role = AsyncMock(return_value=True)
        raid_cog.check_signup = AsyncMock(return_value=True)
        raid_cog.update_raid_post = AsyncMock()
        refresh_i = NS(
            message=NS(id=100),
            channel=NS(id=100),
            user=NS(id=9), guild=NS(id=1),
            response=NS(defer=AsyncMock()),
            followup=NS(send=AsyncMock()),
        )
        await refresh_btn.callback(refresh_i)
        raid_cog.update_raid_post.assert_awaited_once()

        interaction = NS(
            message=NS(id=100),
            user=NS(id=9), guild=NS(id=1),
            response=NS(send_modal=AsyncMock(), send_message=AsyncMock()),
        )
        await inbox_btn.callback(interaction)
        interaction.response.send_modal.assert_awaited_once()
        modal = interaction.response.send_modal.call_args.args[0]
        modal.code._value = json.dumps(dict(format='vanguard-raid-planner-session', version=7, grimaEventId='100', teams=[TEAM]))
        planning.sync = AsyncMock()
        submit_i = NS(
            user=NS(id=9), guild=NS(id=1),
            response=NS(defer=AsyncMock(), send_message=AsyncMock()),
            followup=NS(send=AsyncMock()),
        )
        await modal.on_submit(submit_i)
        self.assertEqual(planning.plan(100)['revision'], 1)
        planning.sync.assert_awaited_once_with(100)
        raid_cog.has_raid_leader_role.return_value = False
        submit_i.response.send_message.reset_mock()
        await modal.on_submit(submit_i)
        submit_i.response.send_message.assert_awaited_once()
        self.assertEqual(planning.plan(100)['revision'], 1)
        planning.sync.assert_awaited_once_with(100)



class KinMarkerExportTests(unittest.TestCase):
    def test_marker_names_and_priority(self):
        for raw, name, primary in [
            ('<:net_vanguard_591528:1566670178362589284> Éowyn', 'Éowyn', True),
            ('Sam <a:net_other_1:123456>', 'Sam', True),
            ('<:smile:123456> Frodo', 'Frodo', False),
            ('Éowyn', 'Éowyn', False),
            ('<:net_vanguard_591528:1566670178362589284>', str(PLAYERS[0]['player_id']), True),
        ]:
            with self.subTest(raw=raw):
                player = dict(PLAYERS[0], byname=raw)
                exported = export_session(EVENT, [player], {})['players'][0]
                self.assertEqual(exported['name'], name)
                self.assertEqual(exported['primary'], primary)
                self.assertEqual(player['byname'], raw)

class LargePlannerLinkDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_long_url_attached_with_json_without_oversized_discord_link(self):
        from unittest.mock import patch
        interaction=NS(response=NS(send_message=AsyncMock()))
        cog=NS(events=NS(command_event=AsyncMock(return_value=EVENT)),store=NS(rows=Mock(return_value=[])),players=Mock(return_value=PLAYERS))
        long_url='https://nayberhq.github.io/vanguard-raid-planner/#grima='+'x'*6000
        with patch('planning_cog.planner_link',side_effect=[None,long_url]):
            await PlanningCog.open_planner(cog,interaction)
        args,kwargs=interaction.response.send_message.call_args
        self.assertLess(len(args[0]),2000)
        self.assertTrue(kwargs['ephemeral'])
        self.assertEqual([f.filename for f in kwargs['files']],['grima-100.json','planner-link.txt'])
        self.assertEqual(kwargs['files'][1].fp.getvalue().decode(),long_url)
        self.assertEqual(json.loads(kwargs['files'][0].fp.getvalue())['grimaEventId'],'100')
