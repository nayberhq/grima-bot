"""Cross-repository contract test. CI and releases supply a real planner checkout."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'source'))
from planning import export_session, planner_link, import_plan, decode_plan_code


class VanguardIntegrationTests(unittest.TestCase):
    def test_export_url_generate_and_return_to_grima(self):
        checkout = os.environ.get('VANGUARD_PLANNER_PATH')
        if not checkout:
            if os.environ.get('CI'):
                self.fail('CI requires VANGUARD_PLANNER_PATH pointing to the real planner checkout.')
            self.skipTest('Set VANGUARD_PLANNER_PATH to run the real planner integration test.')
        root = Path(checkout).resolve()
        self.assertTrue((root / 'public/grima-import.js').is_file(), 'Planner checkout must include the Grima bridge')
        event = dict(raid_id=9007199254740001, name='Folly — integration', tier='T2', duration=3600, size=6)
        classes = ['Guardian', 'Minstrel', 'Captain', 'Burglar', 'Hunter', 'Warden']
        players = [dict(player_id=9007199254740993+i, byname=f'Éowyn {i}', **{c:1}) for i,c in enumerate(classes)]
        players[0]['byname'] = '<:net_vanguard_591528:1566670178362589284> Éowyn 0'
        specs = {players[0]['player_id']: {'Guardian':2 << 3}, players[1]['player_id']: {'Minstrel':2 << 3}}
        session = export_session(event, players, specs)
        self.assertEqual(session['players'][0]['name'], 'Éowyn 0')
        self.assertTrue(session['players'][0]['primary'])
        self.assertFalse(session['players'][1]['primary'])
        url = planner_link(session)
        self.assertIsNotNone(url, 'Representative six-player roster should fit the Discord link budget')
        result = subprocess.run(['node', str(Path(__file__).with_name('vanguard-roundtrip.cjs')), str(root)], input=json.dumps(dict(session=session,url=url)), text=True, capture_output=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        expected_ids = {str(p['player_id']) for p in players}
        plans = [output['full'], output['compact'], decode_plan_code(output['code'])]
        for plan in plans:
            with self.subTest(version=plan['version']):
                teams = import_plan(plan, event, players, specs, require_identity=True)
                self.assertEqual(len(teams),1)
                self.assertEqual({p['id'] for p in teams[0]['members']},expected_ids)
                if plan['version'] == 7:
                    self.assertEqual(sum(teams[0]['fellowship_sizes']),6)
                for member in teams[0]['members']:
                    self.assertIn('Éowyn', member['name'])


    def test_200_signups_compact_url_preserves_every_choice_and_priority(self):
        import random,string,base64,zlib
        from planning import CLASSES, PLANNER_URL
        checkout=os.environ.get('VANGUARD_PLANNER_PATH')
        if not checkout:
            if os.environ.get('CI'):self.fail('VANGUARD_PLANNER_PATH is required in CI')
            self.skipTest('Set VANGUARD_PLANNER_PATH')
        rng=random.Random(200)
        event=dict(raid_id=1566673606899933224,name='Large roster — 压力测试',tier='T2',duration=10800,size=24)
        for name_length in (12,32):
            with self.subTest(name_length=name_length):
                players=[]
                for i in range(200):
                    name=''.join(rng.choices(string.ascii_letters,k=name_length-1))+'é'
                    if i%2==0:name='<:net_vanguard_591528:1566670178362589284> '+name
                    players.append(dict(player_id=rng.randrange(10**17,10**18),byname=name,**{c:1 for c in CLASSES}))
                session=export_session(event,players,{})
                url=planner_link(session,max_length=20000)
                self.assertIsNotNone(url)
                legacy=len(PLANNER_URL+'#grima='+base64.urlsafe_b64encode(zlib.compress(json.dumps(session,separators=(',',':'),ensure_ascii=False).encode(),9)).decode().rstrip('='))
                self.assertLess(len(url),legacy)
                result=subprocess.run(['node',str(Path(__file__).with_name('vanguard-roundtrip.cjs')),str(Path(checkout).resolve())],input=json.dumps(dict(session=session,url=url,decodeOnly=True)),text=True,capture_output=True,timeout=120)
                self.assertEqual(result.returncode,0,result.stderr)
                decoded=json.loads(result.stdout)
                for actual, expected in zip(decoded['players'], session['players']):
                    for key in ('id','name','available','max','primary'):
                        self.assertEqual(actual[key],expected[key])
                    self.assertEqual([{k:c[k] for k in ('c','spec','role')} for c in actual['choices']],expected['choices'])
                self.assertEqual(len(decoded['players']),200)
                self.assertEqual(decoded['runs'],session['runs'])
                self.assertEqual(decoded['settings'],session['settings'])
                self.assertEqual(decoded['grimaEventId'],session['grimaEventId'])
                self.assertNotIn('<:', ''.join(p['name'] for p in decoded['players']))
                self.assertEqual(sum(p['primary'] for p in decoded['players']),100)
                self.assertIsNone(planner_link(session,max_length=512))
                self.assertIsNone(planner_link(session))
                print(f'200 signups, {name_length}-character names, ALL class/specs: {len(url)} URL characters vs {legacy} legacy')
