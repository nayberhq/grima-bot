import builtins
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'source'))
_previous_cwd = os.getcwd()
_previous_translate = getattr(builtins, '_', None)
_scratch = tempfile.TemporaryDirectory()
os.chdir(_scratch.name)
for filename in ('common_timezones.txt', 'timezones.txt', 'list-of-raids.csv'):
    Path(filename).write_text((ROOT / 'source' / filename).read_text())
Path('config.json').write_text((ROOT / 'source/example-config.json').read_text())
builtins._ = lambda value: value

from command_panel import SPEC_CHOICES, CommandPanel, ProfileView
from calendar_cog import CalendarCog
from config_cog import ConfigCog
from events_cog import EventsCog
from network_cog import NetworkCog
from planning_cog import PlanningCog
from raid_cog import RaidCog
from time_cog import TimeCog

os.chdir(_previous_cwd)


def tearDownModule():
    _scratch.cleanup()
    if _previous_translate is None and builtins._ is not None and getattr(builtins._, '__code__', None):
        return


def _names(cog_cls):
    return [command.name for command in cog_cls.__cog_app_commands__]


class CommandListTests(unittest.TestCase):
    def test_only_four_slash_commands_stay_registered(self):
        registered = []
        for cog in (EventsCog, ConfigCog, NetworkCog, CalendarCog, PlanningCog, RaidCog, TimeCog, CommandPanel):
            registered.extend(_names(cog))
        self.assertEqual(sorted(registered), ['commands', 'profile', 'settings', 'watch'])
        twitter = (ROOT / 'source' / 'twitter_cog.py').read_text()
        self.assertNotIn('@app_commands.command', twitter)
        self.assertNotIn('GroupCog', twitter)
        self.assertNotIn('@app_commands.command', (ROOT / 'source' / 'treasure_cog.py').read_text())

    def test_catalog_groups_fit_discord_menus(self):
        panel = CommandPanel(NS(get_cog=lambda name: None))
        catalog = panel.catalog()
        self.assertLessEqual(len(catalog), 25)
        names = []
        for _category, items in catalog:
            self.assertLessEqual(len(items), 25)
            names.extend(name for name, _description, _run in items)
        self.assertIn('setup', names)
        self.assertIn('planner-import', names)
        self.assertIn('event-sync', names)
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual([value for _label, value in SPEC_CHOICES], [1, 2, 4, 3, 6, 5, 7, 0])


class ProfileTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.execute('CREATE TABLE Specs (player_id INTEGER PRIMARY KEY, Hunter INTEGER, Guardian INTEGER)')
        self.conn.execute('CREATE TABLE Timezone (player_id INTEGER PRIMARY KEY, timezone TEXT)')
        self.conn.execute('INSERT INTO Specs VALUES (7, 1, 0)')
        self.conn.commit()
        self.raids = NS(emojis_dict={}, specs=['', 'Red', 'Blue', 'Red_Blue', 'Yellow', 'Red_Yellow', 'Blue_Yellow', 'Triple'])
        self.bot = NS(
            conn=self.conn,
            role_names=['Hunter', 'Guardian'],
            get_cog=lambda name: self.raids if name == 'RaidCog' else None,
        )
        self.view = ProfileView(NS(bot=self.bot), 7, 1)

    def _button(self, custom_id):
        return next(child for child in self.view.children if getattr(child, 'custom_id', None) == custom_id)

    def _interaction(self):
        return NS(response=NS(edit_message=AsyncMock(), is_done=lambda: False, send_message=AsyncMock()))

    async def test_profile_cycles_colors_and_tiers_without_writing(self):
        self.assertIn('Personal timezone: server timezone', self.view.embed().description)
        self.assertEqual(self.view.values['Hunter'] & 0b111, 0b001)
        self.assertEqual(self._button('profile:spec:Hunter').label, 'Hunter')
        self.assertEqual(self._button('profile:tier:Hunter').label, 'T1')
        interaction = self._interaction()
        await self._button('profile:spec:Hunter').callback(interaction)
        self.assertEqual(self.view.values['Hunter'] & 0b111, 0b010)
        self.assertEqual(self.conn.execute('SELECT Hunter FROM Specs WHERE player_id=7').fetchone()[0], 1)
        for label in ('T2', 'T*', 'T1'):
            await self._button('profile:tier:Hunter').callback(interaction)
            self.assertEqual(self.view.tiers['Hunter'], label)
        self.assertEqual(len([child for child in self.view.children if getattr(child, 'custom_id', '').startswith('profile:spec:')]), 2)
        self.assertEqual(len([child for child in self.view.children if getattr(child, 'custom_id', '').startswith('profile:tier:')]), 2)

    async def test_star_tier_writes_every_stored_tier_on_save(self):
        interaction = self._interaction()
        await self._button('profile:tier:Hunter').callback(interaction)
        await self._button('profile:tier:Hunter').callback(interaction)
        self.assertEqual(self.view.tiers['Hunter'], 'T*')
        await self._button('profile:spec:Hunter').callback(interaction)
        self.assertEqual(self.view.values['Hunter'], 1 * (1 + 8 + 64 + 512 + 4096))
        await self._button('profile:save').callback(interaction)
        self.assertEqual(self.view.screen, 'zone')
        self.assertIsNone(self.conn.execute('SELECT timezone FROM Timezone WHERE player_id=7').fetchone())
        zone = next(child for child in self.view.children if getattr(child, 'placeholder', None) == 'Personal timezone')
        zone._values = ['America/New_York']
        await zone.callback(interaction)
        self.assertIsNone(self.conn.execute('SELECT timezone FROM Timezone WHERE player_id=7').fetchone())
        await self._button('profile:back').callback(interaction)
        self.assertEqual(self.view.screen, 'grid')
        self.assertEqual(self.view.timezone, 'America/New_York')
        await self._button('profile:save').callback(interaction)
        await self._button('profile:zone-save').callback(interaction)
        self.assertEqual(self.conn.execute('SELECT Hunter FROM Specs WHERE player_id=7').fetchone()[0], 4681)
        self.assertEqual(self.conn.execute('SELECT timezone FROM Timezone WHERE player_id=7').fetchone()[0], 'America/New_York')

    def test_full_class_list_fits_one_message(self):
        names = ['Beorning', 'Brawler', 'Burglar', 'Captain', 'Champion', 'Guardian', 'Hunter', 'Loremaster', 'Mariner', 'Minstrel', 'Runekeeper', 'Warden']
        conn = sqlite3.connect(':memory:')
        columns = ', '.join(f'"{name}" INTEGER' for name in names)
        conn.execute(f'CREATE TABLE Specs (player_id INTEGER PRIMARY KEY, {columns})')
        conn.execute('CREATE TABLE Timezone (player_id INTEGER PRIMARY KEY, timezone TEXT)')
        bot = NS(conn=conn, role_names=names, get_cog=lambda name: None)
        view = ProfileView(NS(bot=bot), 1, 1)
        self.assertEqual(len(view.children), 25)
        self.assertTrue(all(child.row <= 4 for child in view.children))
        self.assertEqual(view.children[-1].label, 'Save')


class PanelHandoffTests(unittest.IsolatedAsyncioTestCase):
    async def test_planner_import_waits_for_a_json_file(self):
        panel = CommandPanel(NS(get_cog=lambda name: None))
        panel.pending_imports[(1, 2, 3)] = (4, time.time() + 100)
        message = NS(
            author=NS(bot=False, id=3),
            guild=NS(id=1),
            channel=NS(id=2),
            attachments=[NS(filename='notes.txt')],
            reply=AsyncMock(),
        )
        await panel.on_message(message)
        self.assertIn((1, 2, 3), panel.pending_imports)
        message.reply.assert_awaited()

    async def test_manage_server_is_checked_before_setup(self):
        panel = CommandPanel(NS(get_cog=lambda name: None))
        interaction = NS(
            user=NS(guild_permissions=NS(manage_guild=False)),
            response=NS(send_message=AsyncMock(), is_done=lambda: False),
        )
        await panel._open_settings(interaction)
        interaction.response.send_message.assert_awaited()
