import asyncio
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

import pulumi

from putils import background


def _get_root(relpath):
    assert not relpath.is_absolute()
    # Parents goes from longest to shortest, so:
    # -1: '.'
    # -2: first directory
    # -3: second directory
    # ...
    # 0: The given file
    p = list(relpath.parents)
    if len(p) == 1:
        return relpath
    else:
        return p[-2]


def mkzinfo(name, contents):
    """
    Generate a zinfo for a virtual name
    """
    zi = zipfile.ZipInfo(name)
    # date_time defaults to minimum date
    return zi


class PipenvPackage:
    def __init__(self, root, resgen):
        self.root = Path(root).resolve()
        self.resgen = resgen

    @property
    def pipfile(self):
        return self.root / 'Pipfile'

    @property
    def lockfile(self):
        return self.root / 'Pipfile.lock'

    @background
    def get_builddir(self):
        # FIXME: Linux only
        buildroot = Path('/tmp/deplumi')
        buildroot.mkdir(parents=True, exist_ok=True)
        contents = self.lockfile.read_bytes()
        dirname = hashlib.sha3_256(contents).hexdigest()
        return buildroot / dirname

    async def _call_subprocess(self, *cmd, check=True, **opts):
        cmd = [
            os.fspath(part) if hasattr(part, '__fspath__') else part
            for part in cmd
        ]
        proc = await asyncio.create_subprocess_exec(*cmd, **opts)
        stdout, stderr = await proc.communicate()
        if check and proc.returncode != 0:
            raise subprocess.SubprocessError
        return stdout, stderr

    async def _call_python(self, *cmd, **opts):
        return await self._call_subprocess(sys.executable, *cmd, **opts)

    async def _call_pipenv(self, *cmd, **opts):
        env = {
            'PIPENV_NOSPIN': '1',
            'PIPENV_PIPFILE': str(self.pipfile),
            'PIPENV_VIRTUALENV': await self.get_builddir(),
            'PIPENV_VERBOSITY': '-1',
            **os.environ,
        }
        return await self._call_subprocess(
            'pipenv', *cmd,
            env=env,
            cwd=str(self.root),
            **opts,
        )

    async def warmup(self):
        """
        Do pre-build prep
        """
        builddir = await self.get_builddir()
        pulumi.debug(f"Using build dir {builddir}")

        # PyUp has terrible uptime, so this breaks a lot
        # if pulumi.runtime.is_dry_run():
        #     # Only do this on preview. Don't fail an up for this.
        #     await self._call_pipenv('check')

        if not builddir.exists():
            builddir.mkdir()
            # We use pip for the actual installation to use the --target argument
            # to minimize what has to be installed in the actual env.
            out, _ = await self._call_pipenv('lock', '--requirements', stdout=subprocess.PIPE)
            with tempfile.NamedTemporaryFile() as ntf:
                ntf.write(out)
                ntf.flush()
                await self._call_subprocess(
                    'pip', 'install', '--target', builddir, '-r', ntf.name,
                )

    async def build(self):
        """
        Actually build
        """
        builddir = await self.get_builddir()
        ziproot = builddir

        # Doing this instead of a NamedTempFile because we don't know what the
        # lifetime of the file needs to be.
        dest = str(builddir) + '.zip'

        await self._build_zip(
            dest, ziproot, self.root,
            virtuals={
                '__res__.py': await self.resgen.build(),
            },
            filter=self._filter,
        )

        return dest

    def _filter(self, path):
        root = _get_root(path)
        if root.name.endswith('.dist-info'):
            # Unnecessary metadata
            return False
        # FIXME: Scan for all the dependencies of boto3 recursively
        elif root.name in ('boto3', 'botocore'):
            # These are provided by the runtime
            return False
        else:
            return True

    @background
    def _build_zip(self, dest, *sources, virtuals={}, filter=None):
        with zipfile.ZipFile(dest, 'w') as zf:
            for source in sources:
                source = Path(source)
                for child in source.rglob('*'):
                    arcname = child.relative_to(source)
                    if filter is None or filter(arcname):
                        zf.write(child, arcname.as_posix())
            for name, data in virtuals.items():
                zi = mkzinfo(name, data)
                zf.writestr(zi, data)
