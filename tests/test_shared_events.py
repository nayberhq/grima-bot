import asyncio
import builtins
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'source'))
# Upstream modules read configuration and translation globals at import time.
previous_cwd = os.getcwd()
previous_translate = getattr(builtins, '_', None)
scratch = tempfile.TemporaryDirectory()
os.chdir(scratch.name)
for filename in ('common_timezones.txt', 'timezones.txt', 'list-of-raids.csv'):
    Path(filename).write_text((ROOT / 'source' / filename).read_text())
Path('config.json').write_text((ROOT / 'source/example-config.json').read_text())
builtins._ = lambda value: value
import discord
from database import create_table
from event_store import EventStore, forum_title, mirror_keys
from events_cog import EventsCog, CreationView
from raid_cog import RaidCog, RaidView
from calendar_cog import CalendarCog


def tearDownModule():
    os.chdir(previous_cwd)
    scratch.cleanup()
    if previous_translate is None:
        del builtins._
    else:
        builtins._ = previous_translate


def forbidden():
    return discord.Forbidden(NS(status=403, reason='Forbidden'), 'test denial')


def database():
    conn = sqlite3.connect(':memory:')
    for table in ('raid', 'player', 'assign', 'settings', 'specs'):
        create_table(conn, table)
    return conn


def insert_raid(store, raid_id=100, start=None):
    start = int(time.time()) + 600 if start is None else start
    store.write('''INSERT INTO Raids
        (raid_id,channel_id,guild_id,organizer_id,name,time,roster,size)
        VALUES (?,100,1,9,'Folly',?,0,12)''', (raid_id, start))
    store.configure(1, 10, 11, 3600, 600)
    store.add_event(raid_id, store.settings(1))
    store.add_copy(raid_id, 1, 100, 100, 11)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.conn = database()
        self.addCleanup(self.conn.close)
        self.store = EventStore(self.conn)
        insert_raid(self.store, start=1000)

    def test_copy_maps_to_single_event_and_duplicate_rejected(self):
        self.store.add_copy(100, 2, 200, 201, 22)
        self.assertEqual(self.store.resolve(201), 100)
        self.assertEqual(self.store.resolve(200), 100)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.add_copy(100, 2, 202, 203, 22)

    def test_end_and_delayed_lock_boundaries(self):
        event = self.store.event(100)
        self.assertFalse(self.store.ended(event, 4599))
        self.assertTrue(self.store.ended(event, 4600))
        self.assertFalse(self.store.lock_due(event, 5199))
        self.assertTrue(self.store.lock_due(event, 5200))

    def test_tokens_rotate_and_revoke(self):
        first = self.store.share(100)
        second = self.store.share(100)
        self.assertIsNone(self.store.by_token(first))
        self.assertEqual(self.store.by_token(second)['raid_id'], 100)
        self.store.write('UPDATE SharedEvents SET share_token=NULL')
        self.assertIsNone(self.store.by_token(second))

    def test_marker_parser(self):
        key = 'aB0_' * 8
        key2 = 'zY9-' * 8
        self.assertEqual(mirror_keys(f'mirrorkey:{key}\nmirrorkey:{key}'), [key])
        self.assertEqual(mirror_keys(f'notmirrorkey:{key} mirrorkey:short mirrorkey:{key}x'), [])
        embed = discord.Embed(title='Shared Event', description=f'Join via mirrorkey:{key}')
        embed.add_field(name='Extra', value=f'mirrorkey:{key2}')
        msg = NS(content='', embeds=[embed])
        self.assertEqual(mirror_keys(msg), [key, key2])

    def test_title_uses_destination_timezone_and_truncates(self):
        # 2026-11-02 01:00 UTC is still Sunday on the Pacific coast.
        from datetime import datetime, timezone
        timestamp = int(datetime(2026, 11, 2, 1, tzinfo=timezone.utc).timestamp())
        self.assertEqual(forum_title('Folly', timestamp, 'America/Los_Angeles'), 'Sunday Folly')
        self.assertEqual(forum_title('Folly', timestamp, 'UTC'), 'Monday Folly')
        self.assertEqual(len(forum_title('x' * 200, timestamp, 'UTC')), 100)

    def test_migrations_preserve_existing_data(self):
        second = EventStore(self.conn)
        self.assertEqual(second.event(100)['name'], 'Folly')

    def test_outbox_snapshots_destinations(self):
        self.store.add_copy(100, 2, 200, 200, 22)
        self.store.queue_announcement(100, 'Starting soon', 9)
        self.store.add_copy(100, 3, 300, 300, 33)
        self.assertEqual({x['thread_id'] for x in self.store.pending_announcements()}, {100, 200})


class EventTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.conn = database()
        self.addCleanup(self.conn.close)
        self.channels = {}
        self.guilds = {1: NS(id=1), 2: NS(id=2)}
        self.raids = RaidCog.__new__(RaidCog)
        self.raids.conn = self.conn
        self.raids.role_names = ('Warden',)
        self.raids.creep_names = ()
        self.raids.class_emojis = []
        self.raids.slots_class_names = [('Warden',)] * 12
        self.raids.spec_emojis = []
        self.raids.emojis_dict = {'Warden': 'W'}
        self.raids.specs = ['', 'Red', 'Blue', 'Red_Blue', 'Yellow']
        self.raids.calendar_cog = NS(update_calendar=AsyncMock(),
            is_raid_leader=lambda member, guild: getattr(member, 'leader', False))
        cogs = {'RaidCog': self.raids, 'TimeCog': NS(get_server_timezone=lambda guild_id: 'UTC')}
        self.bot = NS(conn=self.conn, get_channel=lambda cid: self.channels.get(cid),
            get_guild=lambda gid: self.guilds.get(gid), get_cog=lambda name: cogs.get(name), add_view=Mock())
        self.cog = EventsCog(self.bot)
        cogs['EventsCog'] = self.cog
        self.raids.bot = self.bot
        self.raids.event_store = self.cog.store
        insert_raid(self.cog.store)
        self.cog.store.configure(2, 20, 22, 10800, 3600)
        self.thread = self.make_thread(100)
        self.channels[10]=NS(create_invite=AsyncMock(return_value=NS(url='https://discord.gg/owner')))

    def make_thread(self, thread_id):
        post = NS(edit=AsyncMock())
        thread = NS(id=thread_id, locked=False, archived=False,
            edit=AsyncMock(), fetch_message=AsyncMock(), get_partial_message=Mock(return_value=post), send=AsyncMock(return_value=NS(id=thread_id+10000)))
        self.channels[thread_id] = thread
        return thread

    async def test_persistent_creation_buttons(self):
        panel = CreationView(self.cog, 'cluster')
        self.assertTrue(panel.is_persistent())
        self.assertEqual([button.label for button in panel.children], ['Folly', 'Wailing Hold', 'Palace', 'Garden', 'Hunter Dailies'])

    async def test_duplicate_concurrent_sync_creates_one_post(self):
        token = self.cog.store.share(100)
        target = self.make_thread(200)
        forum = NS(create_thread=AsyncMock(return_value=NS(thread=target, message=NS(id=200))))
        self.cog.validate_destinations = AsyncMock(return_value=forum)
        results = await asyncio.gather(self.cog.sync_event(2, token), self.cog.sync_event(2, token))
        self.assertEqual(sorted(created for _, created in results), [False, True])
        forum.create_thread.assert_awaited_once()
        self.assertEqual(self.cog.store.resolve(200), 100)
        self.assertEqual(len(self.cog.store.copies(100)), 2)

    async def test_leader_controls_require_source_role_without_owner_or_admin_bypass(self):
        self.cog.store.write('INSERT OR REPLACE INTO Settings(guild_id,raid_leader) VALUES (1,77)')
        member = NS(id=9, roles=[], guild_permissions=NS(administrator=True))
        self.guilds[1].get_member = Mock(return_value=member)
        self.guilds[1].fetch_member = AsyncMock()
        self.assertFalse(await self.raids.has_raid_leader_role(member, self.guilds[2], 100))
        member.roles = [NS(id=88)]
        self.assertFalse(await self.raids.has_raid_leader_role(member, self.guilds[2], 100))
        member.roles = [NS(id=77)]
        self.assertTrue(await self.raids.has_raid_leader_role(member, self.guilds[2], 100))
        self.cog.store.write('UPDATE Settings SET raid_leader=NULL WHERE guild_id=1')
        self.assertFalse(await self.raids.has_raid_leader_role(member, self.guilds[1], 100))

    async def test_button_rows_order_and_denied_leader_callbacks(self):
        self.raids.class_emojis = [discord.PartialEmoji(name=f'Class{n}',id=123456789012345678+n) for n in range(12)]
        view = RaidView(self.raids, planner_url='https://nayberhq.github.io/vanguard-raid-planner/')
        self.assertEqual([c.custom_id for c in view.children if c.row==0], ['raid_view:settings','raid_view:import','raid_view:refresh'])
        self.assertEqual([c.custom_id for c in view.children if c.row==1], ['raid_view:check','raid_view:cancel','raid_view:vanguard'])
        self.assertNotIn('raid_view:select', [c.custom_id for c in view.children])
        self.assertEqual([len([c for c in view.children if c.row==row]) for row in range(5)], [3,3,4,4,4])
        for cid in ('raid_view:vanguard','raid_view:import','raid_view:refresh'):
            self.assertEqual(next(c for c in view.children if c.custom_id==cid).style, discord.ButtonStyle.primary)

        self.raids.has_raid_leader_role = AsyncMock(return_value=False)
        i=NS(user=NS(id=9),guild=self.guilds[1],message=NS(id=100),response=NS(send_message=AsyncMock(),send_modal=AsyncMock(),defer=AsyncMock()))
        for cid in ('raid_view:settings','raid_view:import','raid_view:refresh'):
            i.response.send_message.reset_mock()
            await next(c for c in view.children if c.custom_id==cid).callback(i)
            i.response.send_message.assert_awaited_once()
        i.response.send_modal.assert_not_awaited()
        i.response.defer.assert_not_awaited()
        from raid_cog import ConfigureModal
        modal=NS(raid_cog=self.raids,raid_id=100)
        i.response.send_message.reset_mock()
        await ConfigureModal.on_submit(modal,i)
        i.response.send_message.assert_awaited_once()


    async def test_signup_and_cancel_from_mirror_use_canonical_roster(self):
        self.cog.store.add_copy(100, 2, 200, 200, 22)
        self.make_thread(200)
        view = RaidView(self.raids)
        role = NS(id=80, name='Warden')
        user = NS(id=99, roles=[role], display_name='Remote Warden')
        interaction = NS(message=NS(id=200), user=user,
            guild=NS(id=2, roles=[role]), channel=self.channels[200],
            response=NS(send_message=AsyncMock(), defer=AsyncMock()))
        interaction.channel = Mock(spec=discord.Thread, id=200, send=AsyncMock(return_value=NS(delete=AsyncMock())))
        await view.sign_up_class(interaction, 'Warden')
        interaction.channel.send.assert_awaited_once()
        rows = self.conn.execute('SELECT raid_id,player_id,Warden FROM Players').fetchall()
        self.assertEqual(rows, [(100, 99, 1)])
        for thread_id in (100, 200):
            self.channels[thread_id].get_partial_message.return_value.edit.assert_awaited()
        await view.sign_up_cancel(interaction)
        interaction.channel.remove_user.assert_not_called()
        self.assertEqual(self.conn.execute('SELECT count(*) FROM Players').fetchone()[0], 0)

    async def test_partner_admin_cannot_edit_source_event(self):
        self.cog.store.add_copy(100, 2, 200, 200, 22)
        self.guilds[1].get_member = lambda uid: NS(id=uid, leader=False)
        user = NS(id=88, guild_permissions=NS(administrator=True))
        interaction = NS(user=user, guild=self.guilds[2])
        self.assertFalse(await self.cog.can_manage(interaction, 100))
        self.assertFalse(await self.raids.has_raid_permission(user, self.guilds[2], 200))

    async def test_ended_event_rejects_signup(self):
        self.cog.store.write('UPDATE Raids SET time=?', (int(time.time())-4000,))
        interaction = NS(message=NS(id=100), guild=NS(id=1), response=NS(send_message=AsyncMock()))
        self.assertFalse(await self.raids.check_signup(interaction))
        interaction.response.send_message.assert_awaited_once()

    async def test_voice_created_at_start_then_locked_after_delay(self):
        now = int(time.time())
        self.cog.store.write('UPDATE Raids SET time=?', (now,))
        voice = NS(id=500, name='event-100', members=[NS(id=99)], delete=AsyncMock(), edit=AsyncMock())
        self.channels[500] = voice
        self.channels[11] = NS(voice_channels=[])
        self.guilds[1].create_voice_channel = AsyncMock(return_value=voice)
        await self.cog.reconcile(100, now-1)
        self.guilds[1].create_voice_channel.assert_not_awaited()
        await self.cog.reconcile(100, now)
        await self.cog.reconcile(100, now+1)
        self.guilds[1].create_voice_channel.assert_awaited_once()
        await self.cog.reconcile(100, now+3600)
        voice.delete.assert_not_awaited()
        self.assertFalse(self.cog.store.copies(100)[0]['locked'])
        await self.cog.reconcile(100, now+4200)
        self.assertTrue(self.cog.store.copies(100)[0]['locked'])
        voice.members = []
        await self.cog.reconcile(100, now+4201)
        voice.delete.assert_awaited_once()
        self.assertIsNone(self.cog.store.copies(100)[0]['voice_id'])

    async def test_mirrors_link_owner_voice_and_invite_without_creating_voice(self):
        now=int(time.time())
        self.cog.store.write('UPDATE Raids SET time=?',(now,))
        self.cog.store.add_copy(100,2,200,200,0)
        mirror=self.make_thread(200)
        voice=NS(id=500,members=[],delete=AsyncMock(),edit=AsyncMock())
        self.channels[500]=voice;self.channels[11]=NS(voice_channels=[])
        self.guilds[1].create_voice_channel=AsyncMock(return_value=voice)
        self.guilds[2].create_voice_channel=AsyncMock()
        await self.cog.reconcile(100,now)
        self.guilds[1].create_voice_channel.assert_awaited_once()
        self.guilds[2].create_voice_channel.assert_not_awaited()
        embed=mirror.get_partial_message.return_value.edit.call_args.kwargs['embed']
        values=[f.value for f in embed.fields]
        self.assertIn('https://discord.com/channels/1/500',values)
        self.assertIn('https://discord.gg/owner',values)
        self.assertIsNone(self.cog.store.copies(100)[1]['voice_id'])

    async def test_old_mirror_voice_waits_until_empty_then_retires(self):
        self.cog.store.add_copy(100,2,200,200,22);self.make_thread(200)
        self.cog.store.write('UPDATE EventCopies SET voice_id=600 WHERE guild_id=2')
        voice=NS(id=600,members=[NS(id=99)],delete=AsyncMock(),edit=AsyncMock())
        self.channels[600]=voice
        await self.cog.reconcile(100)
        voice.delete.assert_not_awaited()
        voice.members=[]
        await self.cog.reconcile(100)
        voice.delete.assert_awaited_once()
        self.assertIsNone(self.cog.store.copies(100)[1]['voice_id'])

    async def test_restart_after_end_does_not_create_voice(self):
        self.guilds[1].create_voice_channel = AsyncMock()
        self.cog.store.write('UPDATE Raids SET time=?', (int(time.time())-5000,))
        await self.cog.reconcile(100)
        self.guilds[1].create_voice_channel.assert_not_awaited()
        self.assertTrue(self.cog.store.copies(100)[0]['locked'])

    async def test_announcement_failure_does_not_block_other_servers(self):
        self.cog.store.add_copy(100, 2, 200, 200, 22)
        other = self.make_thread(200)
        self.thread.send.side_effect = forbidden()
        self.cog.store.queue_announcement(100, 'Hello', 9)
        with self.assertLogs('events_cog', level='ERROR'):
            await self.cog.deliver_announcements()
        other.send.assert_awaited_once()
        self.assertEqual(len(self.cog.store.pending_announcements()), 1)
        self.thread.send.side_effect = None
        await asyncio.gather(self.cog.deliver_announcements(), self.cog.deliver_announcements())
        self.assertEqual(other.send.await_count, 1)
        self.assertEqual(self.cog.store.pending_announcements(), [])

    async def test_watcher_cursor_retries_network_failure(self):
        key = self.cog.store.share(100)
        self.cog.store.write('INSERT INTO EventWatches VALUES (2,900,0)')
        messages = [NS(id=1, content='no marker'), NS(id=2, content=f'mirrorkey:{key}')]
        async def history(*, limit, oldest_first, after):
            for message in messages:
                if message.id > after.id:
                    yield message
        self.channels[900] = NS(history=history)
        self.cog.sync_event = AsyncMock(side_effect=forbidden())
        await self.cog.scan_watches()
        self.assertEqual(self.cog.store.one('SELECT * FROM EventWatches')['last_message_id'], 2)
        self.assertEqual(len(self.cog.store.rows('SELECT * FROM WatchFailures')),1)
        self.cog.sync_event.side_effect = None
        from network_cog import NetworkCog
        await NetworkCog(self.bot).tick()
        self.assertEqual(self.cog.store.rows('SELECT * FROM WatchFailures'),[])
        await self.cog.scan_watches()
        self.assertEqual(self.cog.store.one('SELECT * FROM EventWatches')['last_message_id'], 2)
        self.cog.sync_event.assert_awaited_with(2, key)
        count = self.cog.sync_event.await_count
        await self.cog.scan_watches()
        self.assertEqual(self.cog.sync_event.await_count, count)

    async def test_fixable_watch_setup_failure_is_durable(self):
        key=self.cog.store.share(100)
        self.cog.store.write('INSERT INTO EventWatches VALUES (2,900,0)')
        async def history(**kwargs):
            if kwargs['after'].id<1:yield NS(id=1,content='mirrorkey:'+key)
        self.channels[900]=NS(history=history)
        self.cog.sync_event=AsyncMock(side_effect=ValueError('Configure forum'))
        await self.cog.scan_watches()
        self.assertEqual(len(self.cog.store.rows('SELECT * FROM WatchFailures')),1)
        from network_cog import NetworkCog
        await NetworkCog(self.bot).tick()
        self.assertEqual(len(self.cog.store.rows('SELECT * FROM WatchFailures')),1)
        self.cog.sync_event.side_effect=None
        await NetworkCog(self.bot).tick()
        self.assertEqual(self.cog.store.rows('SELECT * FROM WatchFailures'),[])

    async def test_push_publishes_once_and_only_discovery(self):
        from network_cog import NetworkCog
        self.channels[800]=NS(id=800,send=AsyncMock(return_value=NS(id=801)))
        self.cog.store.write('INSERT INTO NetworkSettings VALUES (1,800)')
        network=NetworkCog(self.bot)
        await network.tick();await network.tick()
        self.channels[800].send.assert_awaited_once()
        self.assertIn('mirrorkey:',self.channels[800].send.call_args.args[0])
        self.cog.store.touch(100)
        await network.tick()
        self.channels[800].send.assert_awaited_once()

    async def test_announcement_draft_waits_for_manual_publish(self):
        from network_cog import NetworkCog
        key=self.cog.store.share(100)
        self.cog.store.write('INSERT INTO EventWatches VALUES (2,900,0)')
        message=NS(id=1,content='mirrorkey:'+key,flags=NS(crossposted=False))
        async def history(**kwargs):
            if kwargs['after'].id<1:yield message
        channel=Mock(spec=discord.TextChannel)
        channel.is_news.return_value=True
        channel.history=history
        channel.fetch_message=AsyncMock(return_value=message)
        self.channels[900]=channel
        self.cog.sync_event=AsyncMock()
        await self.cog.scan_watches()
        self.cog.sync_event.assert_not_awaited()
        self.assertEqual(len(self.cog.store.rows('SELECT * FROM PendingPublications')),1)
        await NetworkCog(self.bot).tick()
        self.cog.sync_event.assert_not_awaited()
        message.flags.crossposted=True
        # Fresh cog simulates restart: pending publication lives in the database.
        await NetworkCog(self.bot).tick()
        self.cog.sync_event.assert_awaited_once_with(2,key)
        self.assertEqual(self.cog.store.rows('SELECT * FROM PendingPublications'),[])

    async def test_network_push_never_crossposts(self):
        from network_cog import NetworkCog
        message=NS(id=801,publish=AsyncMock())
        self.channels[800]=NS(id=800,send=AsyncMock(return_value=message))
        self.cog.store.write('INSERT INTO NetworkSettings VALUES (1,800)')
        await NetworkCog(self.bot).tick()
        self.channels[800].send.assert_awaited_once()
        message.publish.assert_not_awaited()

    async def test_blocked_server_cannot_resync(self):
        from event_store import InvalidShare
        self.cog.store.write('INSERT INTO EventBlocks VALUES (100,2)')
        with self.assertRaises(InvalidShare):await self.cog.sync_event(2,self.cog.store.share(100))

    async def test_missing_mirror_is_recreated(self):
        self.cog.store.add_copy(100,2,200,200,22)
        old=self.make_thread(200)
        old.fetch_message.side_effect=discord.NotFound(NS(status=404,reason='Not Found'),'missing')
        new=self.make_thread(201)
        forum=NS(create_thread=AsyncMock(return_value=NS(thread=new,message=NS(id=201))))
        self.cog.validate_destinations=AsyncMock(return_value=forum)
        thread,created=await self.cog.sync_event(2,self.cog.store.share(100))
        self.assertEqual(thread,201)
        self.assertTrue(created)
        self.assertEqual(len(self.cog.store.copies(100)),2)
        self.assertEqual(self.cog.store.resolve(201),100)

    async def test_inline_signup_saves_spec_and_canonical_player(self):
        from signup_picker import SignupPicker
        picker=SignupPicker(self.raids,100,99,'Warden');picker.spec=2
        i=NS(user=NS(id=99,display_name='Warden',roles=[]),guild=NS(id=2),channel=self.thread,response=NS(edit_message=AsyncMock(),send_message=AsyncMock()))
        i.channel = Mock(spec=discord.Thread, id=100, send=AsyncMock(return_value=NS(delete=AsyncMock())))
        await picker.submit.callback(i)
        i.channel.send.assert_awaited_once()
        self.assertEqual(self.cog.store.one('SELECT raid_id,player_id,Warden FROM Players'),dict(raid_id=100,player_id=99,Warden=1))
        saved_spec = self.conn.execute('SELECT Warden FROM Specs WHERE player_id=99').fetchone()[0]
        for tier_idx in range(5):
            self.assertEqual((saved_spec >> (tier_idx * 3)) & 0b111, 2)
        i.response.edit_message.assert_awaited_once()

    async def test_saved_specs_auto_apply_on_signup_from_any_synced_server(self):
        self.cog.store.write("UPDATE Raids SET tier='T3' WHERE raid_id=100")
        self.cog.store.add_copy(100, 2, 200, 200, 22)
        self.make_thread(200)
        # Save Red+Blue (0b011 = 3) across all tiers for user 777 by Discord user ID
        all_tiers_val = 3 * (1 + 8 + 64 + 512 + 4096)
        self.conn.execute('INSERT INTO Specs (player_id, Warden) VALUES (?, ?)', (777, all_tiers_val))
        self.conn.commit()
        view = RaidView(self.raids)
        user = NS(id=777, roles=[], display_name='CrossServerWarden')
        interaction = NS(
            message=NS(id=200),
            user=user,
            guild=NS(id=2, roles=[]),
            channel=self.channels[200],
            response=NS(send_message=AsyncMock(), defer=AsyncMock()),
        )
        interaction.channel = Mock(spec=discord.Thread, id=200, send=AsyncMock(return_value=NS(delete=AsyncMock())))
        await view.sign_up_all(interaction)
        interaction.channel.send.assert_awaited_once()
        # Should NOT prompt with SignupPicker; should immediately sign up on canonical raid 100
        self.assertIsNone(interaction.response.send_message.call_args.kwargs.get('view'))
        self.assertEqual(
            self.cog.store.one('SELECT raid_id,player_id,Warden FROM Players WHERE player_id=777'),
            dict(raid_id=100, player_id=777, Warden=1),
        )

    async def test_calendar_includes_mirror_and_omits_ended_events(self):
        self.cog.store.add_copy(100, 2, 200, 200, 22)
        calendar = CalendarCog(self.bot)
        runs = calendar.calendar_embeds(2)[-1]
        self.assertEqual(len(runs.fields), 1)
        self.assertIn('/2/200/200', runs.fields[0].value)
        self.cog.store.write('UPDATE Raids SET time=?', (int(time.time())-5000,))
        self.assertEqual(len(calendar.calendar_embeds(2)[-1].fields), 0)

    async def test_calendar_groups_runs_by_server_day(self):
        calendar = CalendarCog(self.bot)
        base = int(time.time()) + 86400 * 3
        base -= base % 86400
        raids = [
            (10, 1, 'Folly', 'T1', base + 3600, 1),
            (11, 2, 'Palace', '', base + 7200, 1),
            (12, 3, 'Garden', 'T2', base + 86400 + 3600, 1),
        ]
        embed = calendar.calendar_runs_embed(1, raids)
        self.assertEqual(len(embed.fields), 2)
        self.assertEqual(len(embed.fields[0].value.split('\n')), 2)
        self.assertIn(f'<t:{base + 3600}:t> · ', embed.fields[0].value)
        self.assertIn('[Garden T2]', embed.fields[1].value)

    async def test_calendar_server_links_embed_for_mirrored_hosts(self):
        self.cog.store.add_copy(100, 2, 200, 200, 22)
        self.cog.store.write('INSERT OR REPLACE INTO SourceInvites VALUES (1, ?)', ('https://discord.gg/host',))
        calendar = CalendarCog(self.bot)
        embeds = calendar.calendar_embeds(2)
        self.assertEqual(len(embeds), 2)
        self.assertEqual(embeds[0].title, 'Server links')
        self.assertIn('https://discord.gg/host', embeds[0].description)
        self.cog.store.write('UPDATE SharedEvents SET cancelled=1 WHERE raid_id=100')
        after_cancel = calendar.calendar_embeds(2)
        self.assertEqual(len(after_cancel), 1)
        self.assertEqual(after_cancel[0].title, 'Scheduled runs:')
        self.assertEqual(len(after_cancel[0].fields), 0)

    async def test_cancel_refreshes_calendars_on_owner_and_mirrors(self):
        self.make_thread(200)
        self.cog.store.add_copy(100, 2, 200, 200, 22)
        self.raids.calendar_cog.update_calendar.reset_mock()
        await self.cog.cancel(100)
        updated = {call.args[0] for call in self.raids.calendar_cog.update_calendar.await_args_list}
        self.assertEqual(updated, {1, 2})

    async def test_all_cluster_presets_build_views_and_deploy_in_release_order(self):
        from events_cog import PRESETS
        from network_cog import NetworkCog
        expected_order = ['angmar', 'moria_mirkwood', 'isengard_rohan', 'helms_mordor', 'morgul_gundabad', 'umbar_morgoth', 'cluster', 'misc']
        self.assertEqual(list(PRESETS.keys()), expected_order)
        for key in expected_order:
            self.assertTrue(PRESETS[key].get('image', '').startswith('https://lotro-wiki.com/images/'))
            view = CreationView(self.cog, key)
            self.assertTrue(view.is_persistent())
            self.assertGreater(len(view.children), 0)
            for idx, btn in enumerate(view.children):
                self.assertEqual(btn.row, idx // 5)
        sent_titles = []
        sent_images = []
        async def send(*args, **kwargs):
            sent_titles.append(kwargs['embed'].title)
            sent_images.append(kwargs['embed'].image.url)
            return NS(id=5000 + len(sent_titles))
        self.channels[777] = NS(id=777, send=AsyncMock(side_effect=send), get_partial_message=Mock(return_value=NS(delete=AsyncMock())))
        network = NetworkCog(self.bot)
        await network.deploy_all_panels(1, 777)
        self.assertEqual(sent_titles, [PRESETS[k]['title'] for k in expected_order])
        self.assertEqual(sent_images, [PRESETS[k]['image'] for k in expected_order])

    async def test_setup_smoke_test_validates_permissions_and_applies_settings(self):
        from network_cog import NetworkCog
        network = NetworkCog(self.bot)
        self.bot.get_cog = lambda name: {'RaidCog': self.raids, 'EventsCog': self.cog, 'NetworkCog': network}.get(name)
        guild = NS(id=1, me=NS(guild_permissions=NS(manage_events=True)))
        interaction = NS(guild=guild, user=NS(id=9))

        forum = Mock(spec=discord.ForumChannel)
        forum.id = 10
        forum.guild = guild
        forum.mention = '<#10>'
        forum.flags = NS(require_tag=True)
        forum.permissions_for = lambda _: NS(
            view_channel=True, send_messages=True, send_messages_in_threads=False,
            embed_links=True, read_message_history=True, manage_threads=True,
            create_public_threads=True, create_instant_invite=True,
        )

        ann = Mock(spec=discord.TextChannel)
        ann.id = 12
        ann.guild = guild
        ann.mention = '<#12>'
        ann.permissions_for = lambda _: NS(view_channel=True, send_messages=False, read_message_history=True)

        panel_ch = Mock(spec=discord.TextChannel)
        panel_ch.id = 13
        panel_ch.guild = guild
        panel_ch.mention = '<#13>'
        panel_ch.permissions_for = lambda _: NS(view_channel=True, send_messages=True, embed_links=True, read_message_history=True)
        panel_ch.send = AsyncMock(side_effect=lambda *a, **kw: NS(id=9000 + panel_ch.send.await_count))
        panel_ch.get_partial_message = Mock(return_value=NS(delete=AsyncMock()))
        self.channels[13] = panel_ch

        voice_cat = Mock(spec=discord.CategoryChannel)
        voice_cat.id = 11
        voice_cat.guild = guild
        voice_cat.name = 'Raid Voice'
        voice_cat.permissions_for = lambda _: NS(view_channel=True, manage_channels=False)

        ok, fail_embed = await self.cog.smoke_test_and_apply_setup(
            interaction=interaction,
            raid_forum=forum,
            announcement_channel=ann,
            panel_channel=panel_ch,
            voice_category=voice_cat,
        )
        self.assertFalse(ok)
        self.assertIn('Send Messages in Posts', fail_embed.description)
        self.assertIn('Require people to select tags', fail_embed.description)
        self.assertIn('Send Messages', fail_embed.description)
        self.assertIn('Manage Channels', fail_embed.description)

        # Now fix permissions and verify setup succeeds
        forum.flags = NS(require_tag=False)
        forum.permissions_for = lambda _: NS(
            view_channel=True, send_messages=True, send_messages_in_threads=True,
            embed_links=True, read_message_history=True, manage_threads=True,
            create_public_threads=True, create_instant_invite=True,
        )
        ann.permissions_for = lambda _: NS(view_channel=True, send_messages=True, read_message_history=True)
        voice_cat.permissions_for = lambda _: NS(view_channel=True, manage_channels=True)
        watch_ch = Mock(spec=discord.TextChannel)
        watch_ch.id = 14
        watch_ch.guild = guild
        watch_ch.mention = '<#14>'
        watch_ch.permissions_for = lambda _: NS(view_channel=True, read_message_history=True)
        async def empty_history(**kwargs):
            if False:
                yield
        watch_ch.history = empty_history
        leader_role = NS(id=55, mention='<@&55>')
        kin_role = NS(id=66, mention='<@&66>')

        ok2, ok_embed = await self.cog.smoke_test_and_apply_setup(
            interaction=interaction,
            raid_forum=forum,
            announcement_channel=ann,
            panel_channel=panel_ch,
            voice_category=voice_cat,
            watch_channel=watch_ch,
            leader_role=leader_role,
            kin_role=kin_role,
            kin_icon='🛡️',
            server_timezone='America/New_York',
        )
        self.assertTrue(ok2)
        self.assertIn('Server Setup Complete', ok_embed.title)
        self.assertEqual(self.cog.store.settings(1)['forum_id'], 10)
        self.assertEqual(self.cog.store.settings(1)['voice_category_id'], 11)
        self.assertEqual(self.cog.store.one('SELECT push_id FROM NetworkSettings WHERE guild_id=1')['push_id'], 12)
        self.assertEqual(self.cog.store.one('SELECT source_channel_id FROM EventWatches WHERE guild_id=1')['source_channel_id'], 14)
        self.assertEqual(self.conn.execute('SELECT raid_leader, priority, server FROM Settings WHERE guild_id=1').fetchone(), (55, 66, 'America/New_York'))
        self.assertEqual(len(self.cog.store.rows('SELECT * FROM CreationPanels WHERE guild_id=1')), 8)

    async def test_setup_skips_panel_redeploy_when_unchanged(self):
        from events_cog import PRESETS
        from network_cog import NetworkCog
        network = NetworkCog(self.bot)
        network.deploy_all_panels = AsyncMock()
        self.bot.get_cog = lambda name: {'RaidCog': self.raids, 'EventsCog': self.cog, 'NetworkCog': network}.get(name)
        for index, preset in enumerate(PRESETS.keys()):
            self.cog.store.write(
                'INSERT INTO CreationPanels VALUES (?,?,?,?)',
                (9100 + index, 13, 1, preset),
            )
        guild = NS(id=1, me=NS(guild_permissions=NS(manage_events=True)))
        interaction = NS(guild=guild, user=NS(id=9))
        forum = Mock(spec=discord.ForumChannel)
        forum.id = 10
        forum.guild = guild
        forum.mention = '<#10>'
        forum.flags = NS(require_tag=False)
        forum.permissions_for = lambda _: NS(
            view_channel=True, send_messages=True, send_messages_in_threads=True,
            embed_links=True, read_message_history=True, manage_threads=True,
            create_instant_invite=True,
        )
        ann = Mock(spec=discord.TextChannel)
        ann.id = 12
        ann.guild = guild
        ann.mention = '<#12>'
        ann.permissions_for = lambda _: NS(view_channel=True, send_messages=True, read_message_history=True)
        panel_ch = Mock(spec=discord.TextChannel)
        panel_ch.id = 13
        panel_ch.guild = guild
        panel_ch.mention = '<#13>'
        panel_ch.permissions_for = lambda _: NS(view_channel=True, send_messages=True, embed_links=True, read_message_history=True)
        voice_cat = Mock(spec=discord.CategoryChannel)
        voice_cat.id = 11
        voice_cat.guild = guild
        voice_cat.name = 'Raid Voice'
        voice_cat.permissions_for = lambda _: NS(view_channel=True, manage_channels=True)
        ok, embed = await self.cog.smoke_test_and_apply_setup(
            interaction=interaction,
            raid_forum=forum,
            announcement_channel=ann,
            panel_channel=panel_ch,
            voice_category=voice_cat,
        )
        self.assertTrue(ok)
        network.deploy_all_panels.assert_not_awaited()
        self.assertIn('panels unchanged', embed.description)

    async def test_setup_preserves_watch_cursor(self):
        from network_cog import NetworkCog
        network = NetworkCog(self.bot)
        network.deploy_all_panels = AsyncMock()
        self.bot.get_cog = lambda name: {'RaidCog': self.raids, 'EventsCog': self.cog, 'NetworkCog': network}.get(name)
        self.cog.store.write('INSERT INTO EventWatches VALUES (1,14,999)')
        guild = NS(id=1, me=NS(guild_permissions=NS(manage_events=True)))
        interaction = NS(guild=guild, user=NS(id=9))
        forum = Mock(spec=discord.ForumChannel)
        forum.id = 10
        forum.guild = guild
        forum.mention = '<#10>'
        forum.flags = NS(require_tag=False)
        forum.permissions_for = lambda _: NS(
            view_channel=True, send_messages=True, send_messages_in_threads=True,
            embed_links=True, read_message_history=True, manage_threads=True,
            create_instant_invite=True,
        )
        ann = Mock(spec=discord.TextChannel)
        ann.id = 12
        ann.guild = guild
        ann.mention = '<#12>'
        ann.permissions_for = lambda _: NS(view_channel=True, send_messages=True, read_message_history=True)
        panel_ch = Mock(spec=discord.TextChannel)
        panel_ch.id = 13
        panel_ch.guild = guild
        panel_ch.mention = '<#13>'
        panel_ch.permissions_for = lambda _: NS(view_channel=True, send_messages=True, embed_links=True, read_message_history=True)
        voice_cat = Mock(spec=discord.CategoryChannel)
        voice_cat.id = 11
        voice_cat.guild = guild
        voice_cat.name = 'Raid Voice'
        voice_cat.permissions_for = lambda _: NS(view_channel=True, manage_channels=True)
        watch_ch = Mock(spec=discord.TextChannel)
        watch_ch.id = 14
        watch_ch.guild = guild
        watch_ch.mention = '<#14>'
        watch_ch.permissions_for = lambda _: NS(view_channel=True, read_message_history=True)
        async def empty_history(**kwargs):
            if False:
                yield
        watch_ch.history = empty_history
        ok, embed = await self.cog.smoke_test_and_apply_setup(
            interaction=interaction,
            raid_forum=forum,
            announcement_channel=ann,
            panel_channel=panel_ch,
            voice_category=voice_cat,
            watch_channel=watch_ch,
        )
        self.assertTrue(ok)
        self.assertEqual(self.cog.store.one('SELECT last_message_id FROM EventWatches WHERE guild_id=1')['last_message_id'], 999)
        self.assertIn('unchanged cursor', embed.description)

    async def test_forum_routing_tier_then_instance_then_default(self):
        self.cog.store.configure(1, 10, 11, 3600, 600)
        default_only = self.cog.store.settings(1)
        self.assertEqual(self.cog.store.select_forum_id(default_only, size=6, tier='T3'), 10)
        self.assertEqual(self.cog.store.select_forum_id(default_only, size=12, tier='T5'), 10)

        self.cog.store.configure(1, 10, 11, 3600, 600, instance_forum_id=15, t3_forum_id=103)
        mixed = self.cog.store.settings(1)
        self.assertEqual(self.cog.store.select_forum_id(mixed, size=6, tier='T3'), 103)
        self.assertEqual(self.cog.store.select_forum_id(mixed, size=6, tier='T2'), 15)
        self.assertEqual(self.cog.store.select_forum_id(mixed, size=12, tier='T2'), 10)
        self.assertEqual(self.cog.store.select_forum_id(mixed, size=12, tier='T2c'), 10)
        self.assertEqual(self.cog.store.select_forum_id(mixed, size=12, tier='T3'), 103)
        self.assertEqual(self.cog.store.select_forum_id(mixed, size=24, tier=''), 10)

        self.cog.store.configure(
            1, 10, 11, 3600, 600,
            t1_forum_id=101, t2_forum_id=102, t3_forum_id=103, t4_forum_id=104, t5_forum_id=105,
        )
        tiers = self.cog.store.settings(1)
        self.assertEqual(self.cog.store.select_forum_id(tiers, size=6, tier='T1'), 101)
        self.assertEqual(self.cog.store.select_forum_id(tiers, size=12, tier='T2c'), 102)
        self.assertEqual(self.cog.store.select_forum_id(tiers, size=12, tier='T5'), 105)

    async def test_announcement_and_description_templates_with_description_command(self):
        from network_cog import NetworkCog
        from planning_cog import PlanningCog
        self.guilds[1].name = 'Vanguard'
        self.cog.store.write("UPDATE Raids SET tier='T3', boss='Bring consumables' WHERE raid_id=100")
        self.cog.store.write("INSERT OR REPLACE INTO SourceInvites VALUES (1, 'https://discord.gg/vanguard')")
        planning = PlanningCog(self.bot)
        network = NetworkCog(self.bot)
        self.bot.get_cog = lambda name: {'RaidCog': self.raids, 'EventsCog': self.cog, 'PlanningCog': planning, 'NetworkCog': network, 'TimeCog': NS(get_server_timezone=lambda guild_id: 'UTC')}.get(name)

        self.cog.store.write(
            'INSERT OR REPLACE INTO EventTemplates VALUES (?,?,?)',
            (1, 'description', 'Must have tier 3 deed and Discord voice.'),
        )
        await self.cog.refresh(100)
        saved = self.cog.store.one("SELECT body FROM EventTemplates WHERE guild_id=1 AND kind='description'")
        self.assertEqual(saved['body'], 'Must have tier 3 deed and Discord voice.')

        # Verify event embed description matches description.txt template
        edit_kwargs = self.thread.get_partial_message.return_value.edit.call_args.kwargs
        desc = edit_kwargs['embed'].description
        self.assertIn('Folly T3\nBring consumables', desc)
        self.assertIn('----------------------------------------------\n\nMust have tier 3 deed and Discord voice.\n\n----------------------------------------------', desc)
        self.assertIn('Not in the Vanguard discord? join here!\nhttps://discord.gg/vanguard', desc)
        self.assertIn('mirrorkey:', desc)

        # Verify outgoing announcement matches announcement.txt template
        self.bot.fetch_invite = AsyncMock(return_value=NS(guild=NS(id=1), url='https://discord.gg/vanguard'))
        self.channels[800] = NS(id=800, send=AsyncMock(return_value=NS(id=801)))
        self.cog.store.write('INSERT OR REPLACE INTO NetworkSettings VALUES (1, 800)')
        await network.tick()
        ann_body = self.channels[800].send.call_args.args[0]
        self.assertIn('Vanguard is hosting an event!', ann_body)
        self.assertIn('Folly T3 at <t:', ann_body)
        self.assertIn('Bring consumables', ann_body)
        self.assertIn('<#100>', ann_body)
        self.assertIn('Not in the Vanguard discord? join here!\nhttps://discord.gg/vanguard', ann_body)
        self.assertIn('mirrorkey:', ann_body)

    def test_format_event_description_sections(self):
        event = dict(name='Folly', tier='T3', guild_id=1, boss='Bring consumables')
        desc = self.cog.format_event_description(
            event,
            template_text='Must have deed.',
            invite_url='https://discord.gg/x',
        )
        self.assertIn('Folly T3\nBring consumables', desc)
        self.assertIn('Must have deed.', desc)
        self.assertIn('Not in the', desc)

    async def test_calendar_displays_network_embed_emoji_or_kin_name_fallback(self):
        from event_store import extract_origin_badge
        key = self.cog.store.share(100)
        relayed_embed = NS(
            title=None,
            description=f'1 test is hosting an event!\nmirrorkey:{key}',
            url=None,
            footer=None,
            fields=[],
            author=NS(
                name='One',
                icon_url='https://cdn.discordapp.com/emojis/1542002113201180672.png?size=128',
                proxy_icon_url=None,
            ),
        )
        relayed_msg = NS(id=50, content='', embeds=[relayed_embed])
        kin_name, emoji_str, icon_url = extract_origin_badge(relayed_msg)
        self.assertEqual(kin_name, 'One')
        self.assertEqual(emoji_str, '<:net_one_180672:1542002113201180672>')

        # Watcher scans relayed message and records GuildIdentity for source guild 1
        self.cog.store.write('INSERT INTO EventWatches VALUES (2, 900, 0)')
        async def history(**kwargs):
            if kwargs['after'].id < 50:
                yield relayed_msg
        self.channels[900] = NS(history=history)
        target = self.make_thread(200)
        forum = NS(create_thread=AsyncMock(return_value=NS(thread=target, message=NS(id=200))))
        self.cog.validate_destinations = AsyncMock(return_value=forum)
        await self.cog.scan_watches()

        calendar = CalendarCog(self.bot)
        embed_with_icon = calendar.calendar_embeds(2)[-1]
        self.assertIn('<:net_one_180672:1542002113201180672> [Folly]', embed_with_icon.fields[0].value)

        # Fallback to kin name when emoji is absent
        self.cog.store.write("UPDATE GuildIdentity SET emoji=NULL WHERE guild_id=1")
        embed_with_name = calendar.calendar_embeds(2)[-1]
        self.assertIn('**[One]** [Folly]', embed_with_name.fields[0].value)






if __name__ == '__main__':
    unittest.main()
