"""Private organizer controls; all writes use the existing validated command."""
import json
import re
import discord
from planning import signup_choices

class RosterEditor(discord.ui.View):
    def __init__(self,cog,event,user_id):
        super().__init__(timeout=900)
        self.cog,self.event,self.user_id=cog,event,user_id
        self.team=1;self.slot=1;self.page=0;self.player=None;self.choice=None;self.spec_page=0
        self.reload()

    def reload(self):
        plan=self.cog.plan(self.event['raid_id'])
        self.revision=plan['revision'];self.teams=json.loads(plan['body'])
        self.clear_items()
        def select(row,label,options,callback):
            control=discord.ui.Select(row=row,placeholder=label,options=options)
            async def chosen(i):
                callback(control.values[0]);self.reload()
                await i.response.edit_message(content=f'Roster revision {self.revision}. Choose team, slot, player and class/spec, then Save.',view=self)
            control.callback=chosen;self.add_item(control)
        def team(v):self.team=int(v);self.slot=1
        select(0,'Team',[discord.SelectOption(label=f'{n}: {t["label"]}'[:100],value=str(n),default=n==self.team) for n,t in enumerate(self.teams,1)],team)
        members=self.teams[self.team-1]['members']
        self.slot=min(self.slot,len(members)+1)
        select(1,'Slot',[discord.SelectOption(label=f'{n}: '+(members[n-1]['name'] if n<=len(members) else 'Append'),value=str(n),default=n==self.slot) for n in range(1,min(len(members)+1,self.event['size'])+1)],lambda v:setattr(self,'slot',int(v)))
        players=self.cog.players(self.event['raid_id']);self.page=min(self.page,max(0,(len(players)-1)//25))
        batch=players[self.page*25:self.page*25+25]
        def player(v):self.player=int(v);self.choice=None;self.spec_page=0
        if batch:select(2,f'Player (page {self.page+1})',[discord.SelectOption(label=p['byname'][:100],value=str(p['player_id']),default=p['player_id']==self.player) for p in batch],player)
        p=next((p for p in players if p['player_id']==self.player),None)
        if p:
            tier=int(''.join(re.findall(r'\d',self.event.get('tier') or '')) or 1)
            choices=signup_choices(p,self.cog.specs().get(self.player,{}),tier)
            # LOTRO has at most three offered specs per class; select class/spec pages if needed.
            self.spec_pages=max(1,(len(choices)+24)//25)
            self.spec_page%=self.spec_pages
            choices=choices[self.spec_page*25:self.spec_page*25+25]
            if choices:select(3,'Class and specialization',[discord.SelectOption(label=c['c']+' / '+c['spec'],value=c['c']+'|'+c['spec'],default=self.choice==c['c']+'|'+c['spec']) for c in choices],lambda v:setattr(self,'choice',v))
        for label,action in [('Previous players','previous'),('Next players','next'),('Save','save'),('Remove slot','remove'),('More specs','specs')]:
            b=discord.ui.Button(label=label,row=4,style=discord.ButtonStyle.primary if action=='save' else discord.ButtonStyle.secondary)
            async def click(i,action=action):
                if action=='specs':
                    self.spec_page+=1;self.reload();await i.response.edit_message(view=self);return
                if action in ('previous','next'):
                    self.page=max(0,self.page+(1 if action=='next' else -1));self.reload();await i.response.edit_message(view=self);return
                if action=='save' and (self.player is None or self.choice is None):
                    await i.response.send_message('Select a player and class/spec first.',ephemeral=True);return
                c,s=self.choice.split('|') if self.choice else (None,None)
                try:
                    await type(self.cog).edit_command.callback(self.cog,i,self.revision,self.team,self.slot,str(self.player) if action=='save' else None,c,s)
                except ValueError as exc:
                    await i.response.send_message(str(exc),ephemeral=True)
                self.stop()
            b.callback=click;self.add_item(b)

    async def interaction_check(self,i):
        if i.user.id!=self.user_id or not await self.cog.events.can_manage(i,self.event['raid_id']):
            await i.response.send_message('You cannot edit this roster.',ephemeral=True);return False
        plan=self.cog.plan(self.event['raid_id'])
        if not plan or plan['revision']!=self.revision:
            await i.response.send_message('Roster changed. Reopen the roster editor.',ephemeral=True);return False
        return True
