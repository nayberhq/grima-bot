import builtins
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'source'))
builtins._ = getattr(builtins, '_', lambda value: value)
from dev_cog import DevCog


def emoji(name):
    return NS(name=name, delete=AsyncMock())


class SpecEmojiTests(unittest.IsolatedAsyncioTestCase):
    async def test_replaces_only_single_spec_names(self):
        with tempfile.TemporaryDirectory() as folder:
            for stem in ('Warden_Blue', 'Lore-master_Red', 'Warden_Red_Blue', 'Warden'):
                (Path(folder) / f'{stem}.webp').write_bytes(b'img')
            old_blue, old_combo, old_plain = emoji('Warden_Blue'), emoji('Warden_Red_Blue'), emoji('Warden')
            bot = NS(conn=None,
                     fetch_application_emojis=AsyncMock(return_value=[old_blue, old_combo, old_plain]),
                     create_application_emoji=AsyncMock())
            replaced, failed = await DevCog(bot).replace_spec_emojis(['Warden', 'Loremaster'], folder)
        self.assertEqual(sorted(replaced), ['Loremaster_Red', 'Warden_Blue'])
        self.assertEqual(failed, [])
        old_blue.delete.assert_awaited_once()
        old_combo.delete.assert_not_awaited()
        old_plain.delete.assert_not_awaited()
        names = sorted(call.kwargs['name'] for call in bot.create_application_emoji.await_args_list)
        self.assertEqual(names, ['Loremaster_Red', 'Warden_Blue'])

    async def test_reload_emojis_prefers_application_emojis(self):
        from raid_cog import RaidCog
        raids = RaidCog.__new__(RaidCog)
        raids.role_names = ('Warden',)
        raids.creep_names = ()
        raids.class_emojis, raids.creep_emojis = [], []
        raids.spec_emojis = [NS(name='Warden_Blue', __str__=lambda self: 'old')]
        new = NS(name='Warden_Blue')
        raids.bot = NS(fetch_application_emojis=AsyncMock(return_value=[new]))
        self.assertTrue(await raids.reload_emojis())
        self.assertEqual(raids.emojis_dict['Warden_Blue'], str(new))


if __name__ == '__main__':
    unittest.main()
