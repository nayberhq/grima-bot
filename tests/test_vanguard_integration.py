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
        specs = {players[0]['player_id']: {'Guardian':2 << 3}, players[1]['player_id']: {'Minstrel':2 << 3}}
        session = export_session(event, players, specs)
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
                    self.assertTrue(member['name'].startswith('Éowyn'))
