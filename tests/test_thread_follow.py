import sys
from pathlib import Path
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock
import discord

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'source'))
from thread_follow import follow_signup


class ThreadFollowTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_failure_warns_without_raising(self):
        for following in (True, False):
            channel = Mock(spec=discord.Thread, id=123,
                           add_user=AsyncMock(), remove_user=AsyncMock())
            method = channel.add_user if following else channel.remove_user
            method.side_effect = discord.Forbidden(NS(status=403, reason='Forbidden'), 'denied')
            i = NS(channel=channel, user=NS(id=9), followup=NS(send=AsyncMock()))
            with self.assertLogs('thread_follow', level='WARNING'):
                await follow_signup(i, following)
            method.assert_awaited_once_with(i.user)
            self.assertIn('signup change was saved', i.followup.send.call_args.args[0])
            self.assertTrue(i.followup.send.call_args.kwargs['ephemeral'])

    async def test_ordinary_channel_is_untouched(self):
        channel = Mock(spec=discord.TextChannel)
        await follow_signup(NS(channel=channel), True)
        self.assertEqual(channel.mock_calls, [])
