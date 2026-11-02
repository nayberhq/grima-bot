// Runs the real planner scripts against a fresh Grima export; no copied planner logic.
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const input=JSON.parse(fs.readFileSync(0,'utf8')),root=process.argv[2],els={},storage={};
const ctx=vm.createContext({console:{log(){},warn(){},error:console.error},Intl,Blob,Response,CompressionStream,DecompressionStream,TextDecoder,Uint8Array,atob,btoa,URL,URLSearchParams,setTimeout(){},localStorage:{getItem:k=>storage[k]??null,setItem(k,v){storage[k]=v},removeItem(k){delete storage[k]}},document:{getElementById(id){return els[id]??={style:{},value:'',showModal(){},close(){}}}},location:{hash:new URL(input.url).hash,pathname:'/',search:''},history:{replaceState(a,b,url){ctx.cleaned=url}}});
const html=fs.readFileSync(path.join(root,'public/index.html'),'utf8');
assert.match(html,/src="grima-import.js"/);
for(const name of ['app','role-priorities','fellowship','constraints','search','allocation','background','session-recovery','grima-import'])vm.runInContext(fs.readFileSync(path.join(root,`public/${name}.js`),'utf8'),ctx,{filename:name+'.js'});
const run=code=>vm.runInContext(code,ctx);
(async()=>{
 await run('receiveGrimaSnapshot()');
 assert.equal(ctx.cleaned,'/');assert.equal(run('tab'),'Roster');
 const actual=JSON.parse(run('JSON.stringify(sessionSnapshot())'));
 assert.equal(actual.grimaEventId,input.session.grimaEventId);
 assert.equal(actual.players[0].primary,true);assert.equal(actual.players[0].name,'Éowyn 0');
 assert.equal(actual.players[1].primary,false);
 assert.deepEqual(actual.players.map(p=>[p.id,p.name,p.choices.map(c=>[c.c,c.spec,c.role])]),input.session.players.map(p=>[p.id,p.name,p.choices.map(c=>[c.c,c.spec,c.role])]));
 // The organizer chooses composition after loading the signup snapshot.
 run("template={Tank:[1,1],Healer:[1,1],Buffer:[1,1],Debuffer:[1,1]};generateTeams()");
 assert.equal(run('hasGenerated && !dirty'),true);
 const full=JSON.parse(run('JSON.stringify(sessionSnapshot())'));
 const compact=JSON.parse(run('JSON.stringify(compactSessionSnapshot())'));
 assert.equal(compact.teams.length,1);assert.equal(compact.teams[0].members.length,input.session.players.length);
 const code=await run('grimaPlanCode()');
 process.stdout.write(JSON.stringify({full,compact,code}));
})().catch(e=>{console.error(e);process.exitCode=1});
