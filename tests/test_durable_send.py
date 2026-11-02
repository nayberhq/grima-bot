import sqlite3
import unittest
import sys
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'source'))
from durable_send import DurableSender

class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_recovers_accepted_send_after_restart(self):
        conn=sqlite3.connect(':memory:');bot=NS(user=NS(id=9))
        sender=DurableSender(conn,bot)
        conn.execute("INSERT INTO SendIntents VALUES ('event:1',10,'body',100,NULL)");conn.commit()
        message=NS(id=20,author=NS(id=9),content='body')
        async def history(**kwargs):yield message
        channel=NS(id=10,history=history,send=AsyncMock())
        result=await DurableSender(conn,bot).send(channel,'event:1','edited body')
        self.assertEqual(result.id,20);channel.send.assert_not_awaited()
        self.assertEqual(conn.execute('SELECT message_id FROM SendIntents').fetchone()[0],20)
        conn.close()

    async def test_history_failure_does_not_duplicate(self):
        conn=sqlite3.connect(':memory:');sender=DurableSender(conn,NS(user=NS(id=9)))
        conn.execute("INSERT INTO SendIntents VALUES ('event:1',10,'body',100,NULL)");conn.commit()
        async def history(**kwargs):
            raise RuntimeError('history unavailable')
            yield
        channel=NS(id=10,history=history,send=AsyncMock())
        with self.assertRaises(RuntimeError):await sender.send(channel,'event:1','body')
        channel.send.assert_not_awaited();conn.close()

    async def test_embed_panel_recovers_after_restart(self):
        import discord,json
        conn=sqlite3.connect(':memory:');bot=NS(user=NS(id=9));sender=DurableSender(conn,bot)
        embed=discord.Embed(title='Create raid',description='Choose content')
        snapshot=json.dumps({'content':None,'embed':embed.to_dict()},sort_keys=True)
        conn.execute('INSERT INTO SendIntents VALUES (?,?,?,?,NULL)',('panel:1',10,snapshot,100));conn.commit()
        message=NS(id=20,author=NS(id=9),content='',embeds=[embed])
        async def history(**kwargs):yield message
        channel=NS(id=10,history=history,send=AsyncMock())
        result=await sender.send(channel,'panel:1',None,embed=embed)
        self.assertEqual(result.id,20);channel.send.assert_not_awaited();conn.close()

    async def test_known_denial_allows_retry_without_history(self):
        import discord
        conn=sqlite3.connect(':memory:');sender=DurableSender(conn,NS(user=NS(id=9)))
        channel=NS(id=10,send=AsyncMock(side_effect=discord.Forbidden(NS(status=403,reason='denied'),'denied')))
        with self.assertRaises(discord.Forbidden):await sender.send(channel,'announcement:1','body')
        self.assertEqual(conn.execute('SELECT count(*) FROM SendIntents').fetchone()[0],0)
        channel.send.side_effect=None;channel.send.return_value=NS(id=20)
        self.assertEqual((await sender.send(channel,'announcement:1','body')).id,20);conn.close()
