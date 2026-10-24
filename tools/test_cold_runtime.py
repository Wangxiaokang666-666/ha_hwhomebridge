"""免安装打包的离线输入、解包边界及运行时篡改拒绝。"""
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import build_cold_runtime as build
import cold_runtime_entry as entry


class RuntimeTest(unittest.TestCase):
    def test_cache_checksum_mismatch_is_rejected_before_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary).resolve();output=root/'new'
            with self.assertRaises(ValueError):build.build(root,output)
            self.assertFalse(output.exists())

    def test_tar_path_and_link_escape_rejected(self):
        for name,link in (('../escape',''),('python/bin/bad','../../../escape')):
            with tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);archive=root/'bad.tar'
                with tarfile.open(archive,'w') as tar:
                    info=tarfile.TarInfo(name)
                    if link:info.type=tarfile.SYMTYPE;info.linkname=link
                    tar.addfile(info)
                with self.assertRaises(ValueError):build.extract_python(archive,root/'tree')
                self.assertFalse((root/'escape').exists())

    def test_wheel_traversal_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);archive=root/'bad.whl'
            with zipfile.ZipFile(archive,'w') as wheel:wheel.writestr('../escape','bad')
            with self.assertRaises(ValueError):build.extract_wheel(archive,root/'tree')
            self.assertFalse((root/'escape').exists())

    def test_relative_link_inside_python_tree_allowed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);archive=root/'python.tar'
            with tarfile.open(archive,'w') as tar:
                file=tarfile.TarInfo('python/lib/sample');file.size=2;tar.addfile(file,io.BytesIO(b'ok'))
                link=tarfile.TarInfo('python/bin/link');link.type=tarfile.SYMTYPE;link.linkname='../lib/sample';tar.addfile(link)
            build.extract_python(archive,root/'tree')
            self.assertEqual((root/'tree/python/bin/link').read_bytes(),b'ok')

    def test_runtime_changed_or_extra_files_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);file=root/'sample';file.write_bytes(b'ok')
            manifest={'version':1,'platform':'linux-x86_64','files':{'sample':{'type':'file','sha256':hashlib.sha256(b'ok').hexdigest()}}}
            (root/'runtime-manifest.json').write_text(json.dumps(manifest))
            with patch.object(entry.sys,'platform','linux'),patch.object(entry.platform,'machine',return_value='x86_64'):
                entry.validate(root)
                file.write_bytes(b'changed')
                with self.assertRaises(ValueError):entry.validate(root)
                file.write_bytes(b'ok');(root/'extra').write_bytes(b'x')
                with self.assertRaises(ValueError):entry.validate(root)

    def test_launcher_ignores_python_and_loader_environment(self):
        self.assertIn('env -i',build.LAUNCHER)
        self.assertIn('-I -B',build.LAUNCHER)
        self.assertNotIn('pip install',build.LAUNCHER)


if __name__=='__main__':unittest.main()
