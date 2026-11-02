import importlib.util
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('deploy_control',ROOT/'deploy/control.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)

class DeployTests(unittest.TestCase):
    def test_host_and_image_injection_rejected(self):
        for flags in [ ['--status','--user','pi','--host','host;touch /tmp/bad'],['--status','--user=-oProxyCommand=x','--host','pi.local'],['--publish','--image','x:1;echo bad'] ]:
            with self.assertRaises(ValueError):module.validate(module.parser().parse_args(flags))

    def test_failed_tests_prevent_any_ssh(self):
        with patch.object(module,'tests',side_effect=subprocess.CalledProcessError(1,['test'])),patch.object(module,'run') as run:
            with self.assertRaises(subprocess.CalledProcessError):module.main(['--update','--user','pi','--host','192.168.1.5','--image','ghcr.io/me/grima:v1'])
            run.assert_not_called()

    def test_status_uses_verified_ssh(self):
        with patch.object(module,'run') as run:
            module.main(['--status','--user','pi','--host','192.168.1.5'])
            args=run.call_args.args[0]
            self.assertIn('StrictHostKeyChecking=yes',args)
            self.assertIn('pi@192.168.1.5',args)
            self.assertEqual(args[-1],'sudo -n bash /opt/grima/remote.sh status')

    def test_os_upgrade_requires_update_action(self):
        with self.assertRaises(ValueError):module.validate(module.parser().parse_args(['--status','--user','pi','--host','pi.local','--update-os']))

    def test_help_embeds_cover_forum_sync_and_roles_within_discord_limits(self):
        import builtins, sys
        sys.path.insert(0, str(ROOT / 'source'))
        builtins._ = lambda s: s
        import config_cog
        embeds = config_cog.ConfigCog.build_help_embeds('all')
        self.assertEqual(len(embeds), 3)
        total_chars = sum(len(e) for e in embeds)
        self.assertLessEqual(total_chars, 6000)
        for e in embeds:
            self.assertLessEqual(len(e.description), 4096)
        combined = '\n'.join(e.description for e in embeds)
        for expected in ('/event-setup', '/event-panel', '/network-push', '/event-watch', '/specs', '/leader', '/remove_roles'):
            self.assertIn(expected, combined)
        self.assertEqual(len(config_cog.ConfigCog.build_help_embeds('forum')), 1)
        self.assertEqual(len(config_cog.ConfigCog.build_help_embeds('sync')), 1)
        self.assertEqual(len(config_cog.ConfigCog.build_help_embeds('roles')), 1)

