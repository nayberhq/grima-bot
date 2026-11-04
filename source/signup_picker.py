"""Inline class/spec setup that completes a canonical signup."""
import time
import discord
from planning import CLASSES
from database import upsert, select_one

class SignupPicker(discord.ui.View):
    def __init__(self,raids,raid_id,user_id,class_name=None):
        super().__init__(timeout=180)
        self.raids,self.raid_id,self.user_id=raids,raid_id,user_id
        self.class_name=class_name
        self.spec=None
        classes=[c for c in raids.role_names if any(k.replace('-','').lower()==c.lower() for k in CLASSES)]
        select=discord.ui.Select(placeholder='Choose class',options=[discord.SelectOption(label=c,default=c==class_name) for c in classes])
        async def choose(i):
            self.class_name=select.values[0]
            await i.response.defer()
        select.callback=choose;self.add_item(select)
        spec=discord.ui.Select(placeholder='Choose specialization',options=[discord.SelectOption(label=c,value=str(v)) for c,v in [('Red',1),('Blue',2),('Yellow',4)]])
        async def choose_spec(i):
            self.spec=int(spec.values[0]);await i.response.defer()
        spec.callback=choose_spec;self.add_item(spec)

    async def interaction_check(self,i):return i.user.id==self.user_id

    @discord.ui.button(label='Sign up',style=discord.ButtonStyle.success)
    async def submit(self,i,button):
        if not self.class_name or not self.spec:
            await i.response.send_message('Choose a class and specialization.',ephemeral=True);return
        canonical=next((c for c in CLASSES if c.replace('-','').lower()==self.class_name.lower()),None)
        color={1:'Red',2:'Blue',4:'Yellow'}[self.spec]
        if color not in dict(CLASSES.get(canonical,[])):
            await i.response.send_message('That class does not support this specialization.',ephemeral=True);return
        event=self.raids.event_store.event(self.raid_id)
        blocked=self.raids.event_store.one('SELECT 1 FROM EventBlocks WHERE raid_id=? AND guild_id=?',(self.raid_id,i.guild.id))
        if blocked or not event or self.raids.event_store.ended(event,time.time()):
            await i.response.send_message('This event has ended or was removed.',ephemeral=True);return
        tier=int(''.join(filter(str.isdigit,event['tier'] or '')) or 1)
        prior=select_one(self.raids.conn,'Specs',[self.class_name],['player_id'],[i.user.id]) or 0
        if prior:
            shift=(tier-1)*3
            value=(prior & ~(7<<shift)) | self.spec<<shift
        else:
            value=self.spec*(1+8+64+512+4096)
        upsert(self.raids.conn,'Specs',[self.class_name],[value],['player_id'],[i.user.id])
        upsert(self.raids.conn,'Players',['byname','timestamp','unavailable',self.class_name],[self.raids.process_name(i.guild.id,i.user),int(time.time()),False,True],['player_id','raid_id'],[i.user.id,self.raid_id])
        self.raids.conn.commit()
        await i.response.edit_message(content='Signed up. Your selection is shared across every linked server.',view=None)
        await self.raids.update_raid_post(self.raid_id,i.channel,delay=0)
        self.stop()
