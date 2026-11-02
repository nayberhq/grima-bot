#!/usr/bin/env python3
"""Linux SSH deployment controller. No shell interpolation of user input."""
import argparse
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tarfile
import tempfile

ROOT=Path(__file__).resolve().parents[1]

def run(argv,**kwargs):
    return subprocess.run([str(x) for x in argv],check=True,**kwargs)

def parser():
    p=argparse.ArgumentParser(description='Deploy and manage Grima Docker on a 64-bit Debian/Pi host.')
    action=p.add_mutually_exclusive_group(required=True)
    for name in ('deploy','update','status','logs','restart','backup','test','publish'):
        action.add_argument('--'+name,action='store_true')
    p.add_argument('--user');p.add_argument('--host');p.add_argument('--port',type=int,default=22)
    p.add_argument('--identity',type=Path);p.add_argument('--config',type=Path)
    p.add_argument('--image',help='Versioned registry image, e.g. ghcr.io/you/grima-bot:v1')
    p.add_argument('--pull-git',action='store_true',help='Fast-forward this local deployment checkout from origin before testing')
    p.add_argument('--update-os',action='store_true',help='Run apt update and upgrade on remote host')
    p.add_argument('--reboot',action='store_true',help='Reboot after successful update if /var/run/reboot-required exists')
    return p

def validate(a):
    if a.image and not re.fullmatch(r'[a-z0-9][a-z0-9./_:@-]*',a.image):raise ValueError('Invalid image reference')
    if a.deploy or a.update or a.publish:
        if not a.image or (':' not in a.image.rsplit('/',1)[-1]):raise ValueError('Provide a versioned --image (tag or digest).')
    if a.deploy and not a.config:raise ValueError('First deploy requires --config with your Discord settings.')
    if a.config and (not a.config.is_file() or not (a.deploy or a.update)):raise ValueError('--config must be an existing file and requires --deploy/--update')
    if a.update_os and not (a.deploy or a.update):raise ValueError('--update-os requires --deploy/--update')
    if a.reboot and not a.update_os:raise ValueError('--reboot requires --update-os')
    if not a.test and not a.publish:
        if not a.user or not re.fullmatch(r'[a-z_][a-z0-9_-]*',a.user):raise ValueError('Provide a valid --user')
        if not a.host or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.:-]*',a.host):raise ValueError('Provide a valid --host IP or hostname')
        if not 1<=a.port<=65535:raise ValueError('Invalid SSH port')

def tests():
    run([sys.executable,'-m','unittest','discover','-s','tests','-q'],cwd=ROOT)
    run(['bash','-n',ROOT/'deploy.sh'])
    run(['bash','-n',ROOT/'deploy/remote.sh'])

def main(argv=None):
    p=parser();a=p.parse_args(argv)
    try:validate(a)
    except ValueError as exc:p.error(str(exc))
    if a.pull_git:
        if run(['git','status','--porcelain'],cwd=ROOT,capture_output=True,text=True).stdout.strip():p.error('Commit/stash changes before --pull-git')
        remote=run(['git','remote','get-url','origin'],cwd=ROOT,capture_output=True,text=True).stdout.strip()
        if re.search(r'(Baviaan/lotro|Chaeruman/bot-raid)(?:\.git)?$',remote,re.I):p.error('origin must be your Grima repository, not upstream')
        run(['git','pull','--ff-only','origin'],cwd=ROOT)
        args=list(sys.argv[1:] if argv is None else argv);args.remove('--pull-git')
        os.execv(sys.executable,[sys.executable,str(ROOT/'deploy/control.py'),*args])
    if a.test or a.publish or a.deploy or a.update:tests()
    if a.test:return
    if a.publish:
        run(['docker','buildx','build','--platform','linux/amd64,linux/arm64','-f',ROOT/'dockerfile','-t',a.image,'--push',ROOT]);return
    ssh=['ssh','-p',str(a.port),'-o','StrictHostKeyChecking=yes']
    if a.identity:ssh+=['-i',str(a.identity)]
    target=f'{a.user}@{a.host}'
    def remote(command,**kw):return run([*ssh,target,command],**kw)
    # Target needs working SSH key authentication and passwordless sudo.
    # SSH host key verification is deliberately not disabled.
    remote('sudo -n true')
    if a.deploy or a.update:
        upload=remote('umask 077; mktemp -d /tmp/grima-upload.XXXXXXXX',capture_output=True,text=True).stdout.strip()
        if not re.fullmatch(r'/tmp/grima-upload\.[A-Za-z0-9]+',upload):raise RuntimeError('Unexpected remote staging path')
        with tempfile.TemporaryDirectory() as folder:
            bundle=Path(folder)/'bundle.tar'
            with tarfile.open(bundle,'w') as tar:
                tar.add(ROOT/'deploy/remote.sh',arcname='remote.sh')
                if a.config:tar.add(a.config.resolve(),arcname='config.json')
            with bundle.open('rb') as stream:
                remote('umask 077; tar -xf - -C '+shlex.quote(upload),stdin=stream)
        flags=['install' if a.deploy else 'update',a.image,'1' if a.update_os else '0','1' if a.reboot else '0']
        try:
            remote(shlex.join(['sudo','-n','bash',upload+'/remote.sh',*flags]))
        finally:
            remote('rm -rf -- '+shlex.quote(upload))
    else:
        action=next(n for n in ('status','logs','restart','backup') if getattr(a,n))
        remote('sudo -n bash /opt/grima/remote.sh '+shlex.quote(action))

if __name__=='__main__':main()
