"""从固定哈希的离线缓存打包Linux宿主冷备运行时；不下载、不执行SDK、不连接HA。"""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import shutil
import stat
import tarfile
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
LOCK = Path(__file__).with_name('cold_runtime.lock.json')
SOURCES = ('tools/ha_cold_export.py','tools/ha_entity_preflight.py','tools/ha_production_readonly.py',
           'tools/ha_upgrade.py','custom_components/hwhomebridge/identity_store.py')
LAUNCHER = '''#!/bin/sh
set -eu
umask 077
root=$(CDPATH= cd -P -- "$(dirname -- "$0")" && pwd -P)
exec env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin HOME=/nonexistent LANG=C LC_ALL=C TZ=UTC \\
  "$root/python/bin/python3.12" -I -B "$root/runtime_entry.py" "$@"
'''


def safe_name(name):
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or str(path) != name or '\\' in name:
        raise ValueError('unsafe archive member')
    return path


def extract_python(archive, destination):
    with tarfile.open(archive) as source:
        members = source.getmembers()
        if len(members) > 30000 or sum(m.size for m in members) > 512*1024*1024:
            raise ValueError('Python archive exceeds limits')
        for member in members:
            path = safe_name(member.name.rstrip('/'))
            if path.parts[0] != 'python' or not (member.isfile() or member.isdir() or member.issym()):
                raise ValueError('ordinary pinned Python tree required')
            if member.issym():
                resolved = PurePosixPath(posixpath.normpath(str(path.parent/member.linkname)))
                if PurePosixPath(member.linkname).is_absolute() or resolved.parts[0] != 'python':
                    raise ValueError('Python link outside archive tree')
        # 冷备CLI不用终端数据库；其大小写别名在Mac文件系统会形成链接环。
        members = [m for m in members if not m.name.startswith('python/share/terminfo/') and m.name != 'python/share/terminfo']
        source.extractall(destination,members=members,filter='data')


def extract_wheel(archive, destination):
    with zipfile.ZipFile(archive) as source:
        for member in source.infolist():
            safe_name(member.filename.rstrip('/'))
            if member.file_size > 32*1024*1024 or stat.S_ISLNK(member.external_attr >> 16):
                raise ValueError('ordinary bounded wheel members required')
            path = destination/member.filename
            if path.exists() and not member.is_dir():
                raise ValueError('wheel file collision')
        source.extractall(destination)


def build(cache, output):
    output = output.absolute()
    if output.exists() or output.resolve() != output or not output.parent.is_dir():
        raise ValueError('new symlink-free output directory required')
    lock = json.loads(LOCK.read_text())
    inputs = []
    for asset in lock['assets']:
        safe_name(asset['name'])
        path = cache/asset['name']
        if not path.is_file() or path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != asset['sha256']:
            raise ValueError('offline cached dependency checksum mismatch')
        inputs.append((asset,path))
    with tempfile.TemporaryDirectory(prefix='.cold-runtime-',dir=output.parent) as temporary:
        stage = Path(temporary)/'output';stage.mkdir(mode=0o700)
        bundle = stage/'hwhomebridge-cold-runtime-x86_64';bundle.mkdir()
        for asset,path in inputs:
            if asset['kind'] == 'python':extract_python(path,bundle)
            else:extract_wheel(path,bundle/'third_party')
        # 只复制显式可信工具与离线解码器，不包含SDK、profile、令牌或网关身份。
        for name in SOURCES:
            target = bundle/'src'/name;target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(ROOT/name,target)
        shutil.copyfile(Path(__file__).with_name('cold_runtime_entry.py'),bundle/'runtime_entry.py')
        shutil.copyfile(ROOT/'LICENSE',bundle/'LICENSE')
        (bundle/'README.txt').write_text(
            'Linux x86_64 host cold-backup runtime. No system Python, pip install, SDK or HA lifecycle actions.\n'
            'Verify the published archive SHA256 before extraction. The internal manifest detects accidental changes, not publisher authenticity.\n'
            'Run ./cold-backup self-test, or ./cold-backup --help. Export requires the actual stopped-owner systemd host context and local Docker/Podman.\n'
            'Use export/verify/restore only. Restore writes a NEW private wrapper with config/; it never overwrites original HA.\n'
            'Keep exports private: they contain complete HA entry credentials and pairing state. A raw restore is not gateway activation.\n')
        (bundle/'cold-backup').write_text(LAUNCHER);(bundle/'cold-backup').chmod(0o755)
        (bundle/'inputs.lock.json').write_text(json.dumps(lock,indent=2)+'\n')
        files = {}
        for path in sorted(bundle.rglob('*')):
            if path.is_symlink():info={'type':'link','target':os.readlink(path)}
            elif path.is_file():info={'type':'file','sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
            else:continue
            files[str(path.relative_to(bundle))] = info
        manifest = {'version':1,'platform':lock['platform'],'python':lock['python'],'files':files,
                    'source_sha256':{name:files['src/'+name]['sha256'] for name in SOURCES},
                    'SDK_included':False,'production_credentials_included':False}
        (bundle/'runtime-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
        archive = stage/'hwhomebridge-cold-runtime-linux-x86_64.tar.gz'
        with archive.open('wb') as stream, gzip.GzipFile(fileobj=stream,mode='wb',filename='',mtime=0,compresslevel=6) as zipped, tarfile.open(fileobj=zipped,mode='w') as tar:
            for path in (bundle,*sorted(bundle.rglob('*'))):
                entry=tar.gettarinfo(str(path),arcname=str(path.relative_to(stage)))
                entry.uid=entry.gid=entry.mtime=0;entry.uname=entry.gname='';entry.pax_headers={}
                if path.is_file() and not path.is_symlink():
                    with path.open('rb') as content:tar.addfile(entry,content)
                else:tar.addfile(entry)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        (stage/(archive.name+'.sha256')).write_text(digest+'  '+archive.name+'\n')
        stage.rename(output)
    return {'passed':True,'archive_sha256':digest,'files':len(files),'platform':lock['platform'],'SDK_included':False}


if __name__ == '__main__':
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    print(json.dumps(build(args.cache,args.output)))
