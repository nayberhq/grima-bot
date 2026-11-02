"""Recover text sends accepted by Discord before the local completion commit."""
import asyncio
from datetime import datetime, timezone
import time
import json
from types import SimpleNamespace
import discord

class DurableSender:
    def __init__(self,conn,bot):
        self.conn,self.bot=conn,bot
        if not hasattr(bot,'_send_lock'):bot._send_lock=asyncio.Lock()
        self.lock=bot._send_lock
        conn.execute('CREATE TABLE IF NOT EXISTS SendIntents(key TEXT PRIMARY KEY,channel_id INTEGER,body TEXT,started REAL,message_id INTEGER)')
        conn.commit()

    async def send(self,channel,key,body,*,embed=None,view=None):
        async with self.lock:
            snapshot=json.dumps({'content':body,'embed':embed.to_dict() if embed else None},sort_keys=True) if embed else body
            row=self.conn.execute('SELECT channel_id,body,started,message_id FROM SendIntents WHERE key=?',(key,)).fetchone()
            if row:
                if row[0]!=channel.id:raise ValueError('Delivery key was reused for a different channel.')
                snapshot=row[1]
                if embed:
                    saved=json.loads(snapshot);body=saved['content'];embed=discord.Embed.from_dict(saved['embed'])
                else:body=snapshot
                if row[3]:return SimpleNamespace(id=row[3])
                # Refuse to resend if history cannot be read. A failed history request
                # is not proof the prior send failed.
                after=discord.Object(id=discord.utils.time_snowflake(datetime.fromtimestamp(row[2],timezone.utc)))
                count=0
                async for message in channel.history(limit=2001,oldest_first=True,after=after):
                    count+=1
                    if message.author.id==self.bot.user.id and message.content==(body or '') and (not embed or any(e.to_dict()==embed.to_dict() for e in message.embeds)):
                        self.conn.execute('UPDATE SendIntents SET message_id=? WHERE key=?',(message.id,key));self.conn.commit()
                        return message
                if count>2000:raise ValueError('Delivery recovery history exceeds 2000 messages; operator review required.')
            else:
                self.conn.execute('INSERT INTO SendIntents VALUES (?,?,?,?,NULL)',(key,channel.id,snapshot,time.time()));self.conn.commit()
            kwargs={'allowed_mentions':discord.AllowedMentions.none()}
            if embed:kwargs['embed']=embed
            if view:kwargs['view']=view
            try:
                message=await channel.send(body,**kwargs)
            except (discord.Forbidden,discord.NotFound):
                self.conn.execute('DELETE FROM SendIntents WHERE key=? AND message_id IS NULL',(key,));self.conn.commit()
                raise
            self.conn.execute('UPDATE SendIntents SET message_id=? WHERE key=?',(message.id,key));self.conn.commit()
            return message
