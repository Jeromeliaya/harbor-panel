#!/usr/bin/env python3
"""Privileged helper for the native menu. Never run against the Compose deployment."""

import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import unicodedata
import uuid

PATH = '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'
ENV = {'PATH': PATH, 'LC_ALL': 'C', 'HOME': '/root'}
PATHS = Path('/etc/harbor-panel.paths')
CONFIG = (PATHS, Path('/etc/harbor-panel.port'), Path('/etc/harbor-panel.env'))
SYSTEMD = Path('/etc/systemd/system/harbor-panel.service')
OPENRC = Path('/etc/init.d/harbor-panel')
OPENRC_LOG = Path('/var/log/harbor-panel.log')
# Exact published service templates, including releases before ownership markers.
UNIT_HASHES = {
    'harbor-panel.service': {
        '2c404943c4a4e6e0d4ae3f81402e226ac7ba58d41747b814e83834999f64e6cc',
        '65587ef7aea701a7767c462a356fdce60e6f4c8ec2debf7b0cd65e3755904c3e',
        '65fafe59a157043cdf11b39ccc68300a5075afab9a9d49c3f79e0872e881b993',
        '99c60198c3704c015e49fe7fe3ee734af3bb930b0a739141479a07293075a421',
        'be8aa127cc6bdda7b4482ea993c2d0af035d13089868c60a533145528e17bc27',
        'dfc420d0de5a658d5c1a930f27fc23d8fa5cf3824e6d20560c7288751',
        'e67fd0f3bb42aa0ae2590a7520585e91c62f653136fa1492785c4f1b05aef071',
    },
    'harbor-panel.openrc': {
        '4501a68ef9cf3be59046f8bb3a3e236488189186e5ffc3b471793b6c5989e211',
        '60e694f737b12e6e05ede1f734160feeaf359ec56a6e164ea296ddb900fbd355',
        '965ccfef8653cb55175bb7b7c902ef1599e79be04d229fc1f2fecb73fc483176',
        'af12123e22d99a9321e5cba141b5a6d4425b092b38d7822d8b807c979ca8bcd8',
        'e51af30461505a586ce6054361e9fa2143e7ecfe97cfb790d75975b3c9ec7933',
    },
}
SHORTCUTS = Path('/usr/local/bin')
BACKUPS = Path('/root/harbor-panel-backups')
DOWNLOAD = 'harbor-panel-menu.sh'
MARKER = '.harbor-panel-install-v1'
RECORDS = '.harbor-panel-menu-v1.json'
SITE_ID = re.compile(r'[0-9a-f]{16}\Z')
DOCKER_ID = re.compile(r'[0-9a-f]{64}\Z')
OWNER = 'io.harbor-panel.website'
CADDY = 'harbor-panel-caddy'
WP_NAME = re.compile(r'harbor-wp-([0-9a-f]{16})(?:-(db|files|database))?\Z')
CONFIG_PREFIX = b'# harbor-sites-v1 '
BUILTINS = set('''alias autoload bg bind break builtin caller case cd chdir command compctl
compgen complete compopt continue coproc declare dirs disable disown do done echo elif else
emulate enable end esac eval exec exit export false fc fg fi for foreach function getopts glob
hash help history if in jobs kill let limit local log logout mapfile noglob popd print printf
pushd pwd read readarray readonly rehash repeat return sched select set setopt shift shopt source
stat suspend test then time times trap true type typeset ulimit umask unalias unfunction unhash
unlimit unset unsetopt until vared wait whence where which while zcompile zformat zle zmodload
zparseopts zregexparse zsocket zstyle ztcp abbr and argparse begin block contains count emit
fish_add_path fish_breakpoint fish_command_not_found fish_default_key_bindings fish_indent
functions math not or path psub random realpath set_color status string switch trap umask'''.split())


class Unsafe(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise Unsafe(message)


def present(path):
    return os.path.lexists(path)


def within(path, parent):
    return path == parent or parent in path.parents


def root_dirs(path, *, private=False, sticky=False):
    for item in (path, *path.parents):
        info = item.lstat()
        allowed_sticky = sticky and item in (Path('/tmp'), Path('/var/tmp')) and info.st_mode & stat.S_ISVTX
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0
                and (not info.st_mode & 0o022 or allowed_sticky), f'Unsafe directory: {item}')
        if private and item == path:
            require(not info.st_mode & 0o077, f'Directory must be root-only: {item}')
    return path.lstat()


def root_file(path, *, optional=False, sticky=False):
    if optional and not present(path):
        return None
    root_dirs(path.parent, sticky=sticky)
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022
            and info.st_nlink == 1, f'Unsafe file: {path}')
    return info


def mounts_beneath(path):
    with open('/proc/self/mountinfo', encoding='utf-8', errors='surrogateescape') as handle:
        for line in handle:
            mount = re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), line.split()[4])
            if within(Path(mount), path):
                return mount
    return None


def no_mounts(path):
    mount = mounts_beneath(path)
    require(mount is None, f'Mounted filesystem under {path}: {mount}')


def write_exclusive(path, data, mode=0o600):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode), 'wb') as handle:
        os.fchmod(handle.fileno(), mode)
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def file_id(info):
    return [info.st_dev, info.st_ino, info.st_ctime_ns, info.st_size]


def run(args, *, check=True, timeout=60):
    result = subprocess.run(args, env=ENV, stdin=subprocess.DEVNULL, capture_output=True,
                            text=True, encoding='utf-8', timeout=timeout)
    if check:
        require(result.returncode == 0, f'Command failed: {" ".join(args)} (exit {result.returncode})')
    return result


def executable(name):
    return shutil.which(name, path=PATH)


def shortcut_name(name):
    def letter_number(char):
        return unicodedata.category(char)[0] in 'LN'
    return (bool(name) and len(name.encode('utf-8')) <= 120 and (letter_number(name[0]) or name[0] == '_')
            and all(letter_number(c) or unicodedata.category(c)[0] == 'M' or c in '_.+-' for c in name)
            and name not in BUILTINS)


class Installation:
    def __init__(self, app, state):
        self.app, self.state = Path(app), Path(state)
        self.trees = (self.app, self.state)
        require(all(re.fullmatch(r'/(?:[\w.-]+/)*[\w.-]+', str(p), re.ASCII)
                    and not set(p.parts) & {'.', '..'} for p in self.trees), 'Unsafe installation path')
        require(not (within(self.app, self.state) or within(self.state, self.app)), 'Overlapping installation paths')
        protected = (Path('/etc'), Path('/usr'), Path('/bin'), Path('/sbin'), Path('/lib'), Path('/lib64'),
                     Path('/proc'), Path('/sys'), Path('/dev'), Path('/run'), Path('/boot'), Path('/home'),
                     Path('/tmp'), Path('/var/tmp'), BACKUPS, Path('/var/lib/docker'), Path('/var/lib/containerd'))
        require(not any(within(p, x) or within(x, p) for p in self.trees for x in protected)
                and not set(self.trees) & {Path('/root'), Path('/opt'), Path('/srv'), Path('/var/lib'),
                                           Path('/var/log'), Path('/var/cache')}, 'Installation paths must be dedicated')
        self.records = self.state / RECORDS
        self.menu = self.app / 'deploy/menu.sh'

    def saved_paths(self):
        root_file(PATHS)
        require(PATHS.read_bytes() == (f'HARBOR_PANEL_DIR={self.app}\nHARBOR_PANEL_STATE={self.state}\n').encode(),
                'Selected paths do not match the installed service configuration')

    def tree_ids(self):
        return [[info.st_dev, info.st_ino] for info in
                (root_dirs(self.app), root_dirs(self.state, private=True))]

    def expected_marker(self):
        return {'paths': [str(p) for p in self.trees], 'directories': self.tree_ids()}

    def ownership(self, *, claim=False):
        self.saved_paths()
        expected = self.expected_marker()
        for tree in self.trees:
            no_mounts(tree)
        markers = [tree / MARKER for tree in self.trees]
        known = [present(p) for p in markers]
        if not all(known):
            require(not any(known) and claim, 'Missing or incomplete dedicated-directory ownership records')
            root_file(self.app / 'server.py')
            for marker in markers:
                write_exclusive(marker, json.dumps(expected).encode())
        for marker in markers:
            root_file(marker)
            require(json.loads(marker.read_bytes()) == expected, f'Ownership record does not match: {marker}')

    def adopt(self):
        if any(present(tree / MARKER) for tree in self.trees):
            self.ownership()
            return
        # Legacy adoption still requires both recorded directories and the exact installed service.
        self.saved_paths()
        self.tree_ids()
        Service(self, require_manager=False)
        self.verify_legacy_state()
        self.ownership(claim=True)

    def verify_legacy_state(self, *, recorded=False):
        entries = {p.name for p in self.state.iterdir()}
        primary = {'audit.sqlite3', 'auth.sqlite3', 'bootstrap.token'}
        legacy = primary | {'audit.sqlite3-wal', 'audit.sqlite3-shm', 'auth.sqlite3-wal', 'auth.sqlite3-shm',
                            'appearance.json', 'background-image', 'ssh_known_hosts', 'local-ssh', 'websites',
                            'host-settings.jobs.json', 'host-settings.lock', 'host-settings.worker.lock'}
        if recorded:
            legacy |= {'workspace', RECORDS}
        require(entries <= legacy and (bool(entries & primary) or recorded),
                f'Cannot verify legacy panel state; unrecognized contents in {self.state}')
        for name in entries & primary:
            source = self.state / name
            root_file(source)
            with source.open('rb') as handle:
                require(name == 'bootstrap.token' or handle.read(16) == b'SQLite format 3\0',
                        f'Invalid panel database: {source}')

    def recovery_trees(self):
        recorded = present(PATHS)
        if recorded:
            self.saved_paths()
        trees = [p for p in self.trees if present(p)]
        ids = {}
        markers = []
        for tree in trees:
            info = root_dirs(tree, private=tree == self.state)
            ids[tree] = [info.st_dev, info.st_ino]
            no_mounts(tree)
            marker = tree / MARKER
            if root_file(marker, optional=True):
                data = json.loads(marker.read_bytes())
                require(isinstance(data, dict) and data.get('paths') == [str(p) for p in self.trees],
                        f'Unexpected directory ownership record: {marker}')
                markers.append(data.get('directories'))
        if markers:
            for pair in markers:
                require(isinstance(pair, list) and len(pair) == len(self.trees) and pair == markers[0]
                        and all(pair[self.trees.index(p)] == ids[p] for p in trees),
                        'Directory identity does not match the installation record')
        else:
            if self.state in trees:
                self.verify_legacy_state(recorded=recorded)
            if self.app in trees:
                require((recorded and not any(self.app.iterdir())) or (
                    root_file(self.app / 'server.py', optional=True)
                    and any(root_file(source, optional=True)
                            and hashlib.sha256(source.read_bytes()).hexdigest() in hashes
                            for template, hashes in UNIT_HASHES.items()
                            for source in (self.app / 'deploy' / template,))),
                    f'Cannot verify legacy application directory: {self.app}')
        return trees

    def data(self):
        if not root_file(self.records, optional=True):
            return {}
        data = json.loads(self.records.read_bytes())
        require(isinstance(data, dict) and set(data) <= {'shortcut', 'download'}, 'Invalid installer records')
        return data

    def save(self, data):
        new = self.state / (RECORDS + '-' + uuid.uuid4().hex)
        try:
            write_exclusive(new, json.dumps(data, ensure_ascii=False).encode())
            new.replace(self.records)
        finally:
            new.unlink(missing_ok=True)

    def wrapper(self):
        return ('#!/bin/sh\nunset HARBOR_PANEL_DIR HARBOR_PANEL_STATE\n'
                'exec /bin/sh ' + shlex.quote(str(self.menu)) + '\n').encode()

    def old_shortcut(self, record):
        if not record:
            return None
        name = record['name']
        require(shortcut_name(name), 'Invalid recorded shortcut name')
        path = SHORTCUTS / name
        info = root_file(path, optional=True)
        if info is None:
            return None
        require(file_id(info) == record['identity'] and path.read_bytes() == self.wrapper(),
                f'Shortcut was replaced; refusing to manage another file: {path}')
        return path

    def shortcut(self, name, caller_path):
        self.ownership()
        require(shortcut_name(name), '快捷命令仅允许字母、数字、组合标记和 _ . + -，且不得使用 shell 内置命令。')
        root_file(self.menu)
        root_dirs(SHORTCUTS)
        data = self.data()
        old = self.old_shortcut(data.get('shortcut'))
        destination = SHORTCUTS / name
        for parent in set((PATH + ':' + caller_path).split(':')):
            other = Path(parent or '.') / name
            if present(other):
                require(old is not None and other.absolute() == old, f'命令名已存在：{other}')
        if old != destination:
            write_exclusive(destination, self.wrapper(), 0o755)
            previous = data.copy()
            data['shortcut'] = {'name': name, 'identity': file_id(destination.lstat())}
            try:
                self.save(data)
                if old is not None:
                    old.unlink()
            except OSError:
                if self.data() == data:
                    self.save(previous)
                destination.unlink(missing_ok=True)
                raise
        print(f'快捷命令已设置：{name}。如 PATH、别名或函数遮蔽，请运行 {destination}（普通用户请用 sudo）。')

    def download(self, raw, *, launcher=False):
        path = Path(os.path.abspath(raw))
        if path.name != DOWNLOAD or any(within(path, tree) for tree in self.trees):
            return None
        try:
            info = root_file(path, optional=True, sticky=True)
            if info is None or info.st_size > 1024 * 1024:
                return None
            content = path.read_bytes()
            if launcher or (root_file(self.menu, optional=True) and content == self.menu.read_bytes()):
                return {'path': str(path), 'identity': file_id(info),
                        'sha256': hashlib.sha256(content).hexdigest()}
        except (Unsafe, OSError):
            pass
        return None

    def remember(self, raw):
        self.ownership()
        data = self.data()
        record = self.download(raw)
        if record is not None and data.get('download') != record:
            data['download'] = record
            self.save(data)

    def remove_download(self, record):
        if not record:
            return
        path = Path(record['path'])
        if path.is_absolute() and path.name == DOWNLOAD:
            try:
                info = root_file(path, optional=True, sticky=True)
                if info is None:
                    return
                if file_id(info) == record['identity'] and hashlib.sha256(path.read_bytes()).hexdigest() == record['sha256']:
                    path.unlink()
                    return
            except (Unsafe, OSError):
                pass
        print(f'未删除已移动、替换或不安全的下载脚本：{path}', file=sys.stderr)


class Service:
    def __init__(self, installation, *, require_manager=True, recovery=False):
        self.i = installation
        self.was_running = False
        self.links = {}
        units = [(SYSTEMD, 'harbor-panel.service'), (OPENRC, 'harbor-panel.openrc')]
        found = [(path, template) for path, template in units if present(path)]
        if recovery:
            for pattern, target in (('/etc/systemd/system/*.wants/harbor-panel.service', SYSTEMD),
                                    ('/etc/systemd/system/*.requires/harbor-panel.service', SYSTEMD),
                                    ('/etc/runlevels/*/harbor-panel', OPENRC)):
                for link in Path('/').glob(pattern[1:]):
                    root_dirs(link.parent)
                    require(link.is_symlink() and link.lstat().st_uid == 0 and link.resolve() == target,
                            f'Unverified service enablement link: {link}')
                    self.links[link] = target
        if recovery and not found:
            self.path = None
            self.systemd = present(Path('/run/systemd/system'))
            self.command = executable('systemctl') if self.systemd else None
            self.check_missing_unit()
            return
        require(len(found) == 1, 'Cannot verify exactly one installed Harbor Panel native service')
        self.path, template = found[0]
        self.systemd = self.path == SYSTEMD
        source = self.i.app / 'deploy' / template
        unit = root_file(self.path)
        require(unit.st_size <= 64 * 1024, f'Unexpected service config: {self.path}')
        content = self.path.read_bytes()
        require((recovery and hashlib.sha256(content).hexdigest() in UNIT_HASHES[template])
                or (root_file(source, optional=True) and source.read_bytes() == content),
                f'Unexpected service config: {self.path}')
        self.command = executable('systemctl' if self.systemd else 'rc-service')
        if require_manager:
            require(self.command, 'Cannot verify the service is stopped without its native service manager')
            if self.systemd:
                show = run([self.command, 'show', '--all', '--property=FragmentPath,DropInPaths',
                            'harbor-panel.service']).stdout
                properties = dict(line.split('=', 1) for line in show.splitlines() if '=' in line)
                require(properties.get('FragmentPath') == str(self.path) and properties.get('DropInPaths') == '',
                        'Unexpected service fragment or drop-ins; verify them manually')
            else:
                require(not present(Path('/etc/conf.d/harbor-panel')), 'Unexpected OpenRC overrides; verify them manually')

    def check_missing_unit(self):
        require(not present(OPENRC) and not present(SYSTEMD), 'Panel service file appeared during the scan')
        require(not present(Path('/run/openrc/started/harbor-panel')),
                'Stop the remaining OpenRC Harbor Panel service first')
        if self.systemd:
            require(self.command, 'Cannot verify the service without systemctl')
            result = run([self.command, 'show', '--all', '--property=LoadState,ActiveState,FragmentPath,DropInPaths',
                          'harbor-panel.service'], check=False)
            properties = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
            require(properties == {'LoadState': 'not-found', 'ActiveState': 'inactive',
                                   'FragmentPath': '', 'DropInPaths': ''},
                    'A Harbor Panel unit is still registered or running; verify it before cleaning up')

    def active(self):
        if self.path is None:
            self.check_missing_unit()
            return False
        if self.systemd:
            state = run([self.command, 'show', '--property=ActiveState', '--value', 'harbor-panel.service']).stdout.strip()
            require(state in {'active', 'inactive', 'failed'}, f'Cannot verify service state: {state}')
            return state == 'active'
        result = run([self.command, 'harbor-panel', 'status'], check=False)
        if result.returncode == 0:
            return True
        require(result.returncode == 3 and 'stopped' in result.stdout.lower(), 'Cannot verify stopped OpenRC service')
        return False

    def action(self, action):
        if self.systemd:
            run([self.command, action, 'harbor-panel.service'])
        else:
            run([self.command, 'harbor-panel', action])

    def stop(self):
        self.was_running = self.active()
        if self.was_running:
            self.action('stop')
        require(not self.active(), 'Panel service is still running')
        script = os.fsencode(self.i.app / 'server.py')
        state = os.fsencode(self.i.state)
        for item in Path('/proc').glob('[0-9]*/cmdline'):
            try:
                args = item.read_bytes().split(b'\0')
                script_arg = any(arg.endswith(b'/server.py') or arg == b'server.py' for arg in args)
                cwd_matches = script_arg and (item.parent / 'cwd').readlink() == self.i.app
            except (ProcessLookupError, FileNotFoundError):
                continue
            args_use_state = state in args or b'--state-dir=' + state in args
            require(script not in args and not (script_arg and (args_use_state or cwd_matches)),
                    'Stop the remaining manually launched Harbor Panel process first')

    def disable(self):
        if self.systemd:
            self.action('disable')
        else:
            update = executable('rc-update')
            require(update, 'Cannot remove OpenRC startup registration')
            run([update, 'del', 'harbor-panel', '-a'])

    def reload(self):
        if self.systemd:
            run([self.command, 'daemon-reload'])


class Websites:
    def __init__(self, installation, *, recovery=False):
        self.i = installation
        self.state = installation.state / 'websites'
        self.proxy = self.state / 'proxy'
        self.orphaned = recovery and not present(self.state)
        self.wordpress = set()
        self.has_proxy = False
        if present(self.state):
            root_dirs(self.state, private=True)
            private = self.state / 'wordpress'
            if present(private):
                root_dirs(private, private=True)
                for site in private.iterdir():
                    require(SITE_ID.fullmatch(site.name), f'Unexpected WordPress state: {site}')
                    root_dirs(site, private=True)
                    self.wordpress.add(site.name)
            config = self.proxy / 'Caddyfile'
            if root_file(config, optional=True):
                with config.open('rb') as handle:
                    header = handle.readline(100_001)
                require(header.startswith(CONFIG_PREFIX) and len(header) <= 100_000, 'Unverified Caddy configuration')
                sites = json.loads(header[len(CONFIG_PREFIX):])
                require(isinstance(sites, list), 'Invalid Harbor website state')
                ids = set()
                for site in sites:
                    require(isinstance(site, dict) and isinstance(site.get('id'), str) and SITE_ID.fullmatch(site['id'])
                            and site['id'] not in ids and site.get('kind') in {'wordpress', 'proxy', 'https'},
                            'Invalid Harbor website state')
                    ids.add(site['id'])
                    if site['kind'] == 'wordpress':
                        require(site['id'] in self.wordpress, 'WordPress site is missing its private ownership state')
                self.has_proxy = True
        self.containers, self.networks, self.volumes = {}, {}, {}
        self.stopped = []
        self.command = executable('docker')
        socket = Path('/var/run/docker.sock')
        if self.command:
            result = self.docker('info', '--format', '{{.DockerRootDir}}', check=False)
            if result.returncode == 0:
                self.discover(Path(result.stdout.strip()))
                return
        require(not present(self.state) and not present(socket) and not present(Path('/var/lib/docker/volumes')),
                'Cannot verify website ownership: start the local Docker daemon and retry')

    def docker(self, *args, check=True, timeout=60):
        # Never pick up a remote context, home-directory config or environment override.
        return run([self.command, '--config', '/proc/self/fd', '--host', 'unix:///var/run/docker.sock', *args],
                   check=check, timeout=timeout)

    @staticmethod
    def reserved(name):
        return name == CADDY or name.startswith((CADDY + '-', 'harbor-wp-'))

    def owner(self, kind, name, labels):
        label = (labels or {}).get(OWNER)
        if label is None and not self.reserved(name):
            return None
        suffixes = {'container': {'', '-db'}, 'network': {''}, 'volume': {'-files', '-database'}}
        proxy_names = {'container': {CADDY}, 'network': set(), 'volume': {CADDY + '-data', CADDY + '-config'}}
        if name in proxy_names[kind]:
            require(label == 'proxy' and (self.has_proxy or self.orphaned), f'Cannot verify ownership of {kind}: {name}')
            return 'proxy'
        match = WP_NAME.fullmatch(name)
        require(match and ('-' + match[2] if match[2] else '') in suffixes[kind]
                and ((label == match[1] and (match[1] in self.wordpress or self.orphaned))
                     or kind == 'volume' and label is None and match[1] in self.wordpress),
                f'Cannot verify ownership of {kind}: {name}')
        return match[1]

    def objects(self, kind, ids):
        for first in range(0, len(ids), 40):
            batch = ids[first:first + 40]
            result = json.loads(self.docker(kind, 'inspect', '--', *batch).stdout)
            require(len(result) == len(batch), f'Incomplete Docker {kind} inspection')
            for requested, item in zip(batch, result):
                actual = item['Name'] if kind == 'volume' else item['Id']
                require(actual == requested, f'Docker {kind} changed during ownership inspection')
                yield item

    def names(self, kind, *args):
        ids = self.docker(kind, 'ls', *args).stdout.splitlines()
        pattern = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]*\Z') if kind == 'volume' else DOCKER_ID
        require(len(ids) == len(set(ids)) and all(pattern.fullmatch(item) for item in ids), 'Invalid Docker inventory')
        return ids

    def discover(self, docker_root):
        containers = list(self.objects('container', self.names('container', '--all', '--quiet', '--no-trunc')))
        networks = self.objects('network', self.names('network', '--quiet', '--no-trunc'))
        for item in networks:
            name = item['Name']
            if self.owner('network', name, item.get('Labels')):
                require(item['Driver'] == 'bridge' and item['Scope'] == 'local' and name not in self.networks,
                        f'Unexpected WordPress network: {name}')
                self.networks[name] = item
        named = self.names('volume', '--quiet')
        marked = self.names('volume', '--quiet', '--filter', f'label={OWNER}')
        selected = sorted({name for name in named if self.reserved(name)} | set(marked))
        for item in self.objects('volume', selected):
            name = item['Name']
            require(self.owner('volume', name, item.get('Labels')), f'Docker volume ownership changed: {name}')
            path = docker_root / 'volumes' / name / '_data'
            require(docker_root.is_absolute() and docker_root != Path('/') and item['Driver'] == 'local'
                    and item['Scope'] == 'local' and not item.get('Options') and item['Mountpoint'] == str(path)
                    and not any(within(path, tree) or within(tree, path) for tree in self.i.trees),
                    f'Unsupported Docker volume path or driver: {name}')
            root_dirs(path.parent)
            require(stat.S_ISDIR(path.lstat().st_mode), f'Not a regular volume directory: {name}')
            no_mounts(path)
            self.volumes[name] = item
        for item in containers:
            name = item['Name'].removeprefix('/')
            owner = self.owner('container', name, item['Config'].get('Labels'))
            mounts = item['Mounts']
            if owner == 'proxy':
                require(item['HostConfig']['NetworkMode'] == 'host'
                        and item['Config']['Image'] == 'docker.io/library/caddy:2'
                        and self.volume_mount(mounts, CADDY + '-data', '/data')
                        and self.volume_mount(mounts, CADDY + '-config', '/config')
                        and any(m['Type'] == 'bind' and m['Source'] == str(self.proxy)
                                and m['Destination'] == '/etc/harbor-sites' and m['RW'] is False for m in mounts),
                        'Caddy does not match the installed panel configuration and volumes')
            elif owner:
                prefix = 'harbor-wp-' + owner
                db = name.endswith('-db')
                volume, location = ('-database', '/var/lib/mysql') if db else ('-files', '/var/www/html')
                image = 'docker.io/library/mariadb:11.4' if db else 'docker.io/library/wordpress:php8.3-apache'
                net = (item['NetworkSettings'].get('Networks') or {}).get(prefix)
                require(prefix in self.networks and net and net['NetworkID'] == self.networks[prefix]['Id']
                        and item['Config']['Image'] == image and self.volume_mount(mounts, prefix + volume, location),
                        f'WordPress service does not match its verified network and data: {name}')
            for mount in mounts:
                linked = mount.get('Name') in self.volumes or any(
                    mount.get('Source') == vol['Mountpoint'] or within(Path(mount.get('Source') or '/'), Path(vol['Mountpoint']))
                    for vol in self.volumes.values())
                bind = mount['Type'] == 'bind' and any(within(Path(mount['Source']), tree) for tree in self.i.trees)
                if linked or bind:
                    require(owner and (mount.get('Name') in ({CADDY + '-data', CADDY + '-config'} if owner == 'proxy' else
                                                           {'harbor-wp-' + owner + s for s in ('-database', '-files')})
                                       or owner == 'proxy' and mount['Source'] == str(self.proxy)
                                       and mount['Destination'] == '/etc/harbor-sites' and mount['RW'] is False),
                            f'Unexpected container depends on panel data: {name}')
            if owner:
                require(name not in self.containers, f'Duplicate Docker container: {name}')
                self.containers[name] = item
        for name, net in self.networks.items():
            allowed = {item['Id'] for cname, item in self.containers.items() if cname in {name, name + '-db'}}
            attached = set((net.get('Containers') or {}).keys())
            attached |= {item['Id'] for item in containers if any(n.get('NetworkID') == net['Id']
                         for n in (item['NetworkSettings'].get('Networks') or {}).values())}
            require(attached <= allowed, f'Unrelated containers use managed network: {name}')

    def volume_mount(self, mounts, name, destination):
        return name in self.volumes and any(m['Type'] == 'volume' and m['Name'] == name
                    and m['Source'] == self.volumes[name]['Mountpoint'] and m['Destination'] == destination for m in mounts)

    def signature(self):
        return ([(n, v['Id']) for n, v in sorted(self.containers.items())],
                [(n, v['Id']) for n, v in sorted(self.networks.items())],
                [(n, volume_signature(v)) for n, v in sorted(self.volumes.items())])

    def stop(self):
        for name in sorted(self.containers, key=lambda n: (n != CADDY, n.endswith('-db'), n)):
            item = self.containers[name]
            if item['State']['Running']:
                self.stopped.append(item['Id'])
                self.docker('container', 'stop', '--time', '60', item['Id'], timeout=90)
                stopped = next(self.objects('container', [item['Id']]))['State']
                require(stopped['Status'] == 'exited' and stopped.get('ExitCode') in {0, 143},
                        f'Container did not shut down cleanly: {name}')

    def restart(self):
        for ident in reversed(self.stopped):
            try:
                self.docker('container', 'start', ident)
            except (Unsafe, subprocess.SubprocessError, OSError) as error:
                print(f'无法恢复已停止的容器 {ident}: {error}', file=sys.stderr)

    def remove(self):
        for item in self.containers.values():
            self.docker('container', 'rm', item['Id'])
        for item in self.networks.values():
            self.docker('network', 'rm', item['Id'])
        for name, item in self.volumes.items():
            current = next(self.objects('volume', [name]))
            require(volume_signature(current) == volume_signature(item), f'Docker volume changed: {name}')
            self.docker('volume', 'rm', name)


def volume_signature(item):
    return tuple(item.get(key) for key in ('Name', 'Mountpoint', 'Labels', 'Options', 'CreatedAt', 'Driver', 'Scope'))


def host_jobs(state):
    journal = state / 'host-settings.jobs.json'
    if root_file(journal, optional=True):
        jobs = json.loads(journal.read_bytes())
        require(isinstance(jobs, list) and all(isinstance(j, dict) and isinstance(j.get('status'), str) for j in jobs),
                'Invalid listener change journal')
        require(not any(j['status'] in {'queued', 'running', 'awaiting_confirmation', 'rollback_failed'} for j in jobs),
                'Resolve or roll back the listener change before uninstalling')


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def archive_backup(i, files, websites, *, trees):
    root_dirs(BACKUPS.parent)
    BACKUPS.mkdir(mode=0o700, exist_ok=True)
    root_dirs(BACKUPS, private=True)
    no_mounts(BACKUPS)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    dest = BACKUPS / (stamp + '-' + uuid.uuid4().hex[:12])
    dest.mkdir(mode=0o700)
    package = dest / 'harbor-panel.tar.gz'
    manifest = {'format': 1, 'app': str(i.app), 'state': str(i.state),
                'host_files': {str(path): 'host/' + path.as_posix().lstrip('/') for path in files},
                'volumes': sorted(websites.volumes), 'containers': sorted(websites.containers),
                'networks': sorted(websites.networks)}
    entries = [(tree, member) for tree, member in ((i.app, 'panel/app'), (i.state, 'panel/state')) if tree in trees]
    entries.extend((Path(path), member) for path, member in manifest['host_files'].items())
    entries.extend((Path(item['Mountpoint']), 'docker-volumes/' + name) for name, item in websites.volumes.items())
    count = 0

    def include(info):
        nonlocal count
        require(info.isdir() or info.isfile() or info.issym() or info.islnk(), 'Unexpected special file: ' + info.name)
        count += 1
        return info

    try:
        for source, _ in entries:
            no_mounts(source)
        with os.fdopen(os.open(package, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as stream:
            with tarfile.open(fileobj=stream, mode='w:gz', dereference=False) as tar:
                for source, member in entries:
                    tar.add(source, arcname=member, filter=include)
            stream.flush()
            os.fsync(stream.fileno())
        # Re-read the entire gzip stream, including the trailer, before deleting anything.
        import gzip
        with gzip.open(package, 'rb') as stream:
            while stream.read(1024 * 1024):
                pass
        with tarfile.open(package, 'r:gz') as tar:
            members = tar.getnames()
            require(len(members) == count and set(member for _, member in entries) <= set(members),
                    'Backup verification failed')
        write_exclusive(dest / 'manifest.json', (json.dumps(manifest, indent=2, ensure_ascii=False) + '\n').encode())
        if websites.containers:
            # Runtime environment includes the database password; volume data alone cannot recreate it.
            private = {name: {key: item[key] for key in ('Config', 'HostConfig', 'NetworkSettings', 'Mounts', 'Image')}
                       for name, item in websites.containers.items()}
            write_exclusive(dest / 'docker-containers.private.json', (json.dumps(private, indent=2) + '\n').encode())
        write_exclusive(dest / 'RESTORE.txt', b'''PRIVATE: this backup contains administrator credentials, MFA data and TLS keys.
Do not restore over a live panel. Inspect manifest.json and the tar member list first.
Extract to private staging as an unprivileged user before selecting what to restore.
Install the same Harbor Panel version at the tracked paths, then stop it.
Copy selected panel/state data while stopped. Restoring auth.sqlite3 revives old credentials;
for fresh registration leave auth.sqlite3 and its WAL/SHM and bootstrap.token untouched.
Review host/ configuration before manually applying it. The shortcut must be recreated via menu 5.
Docker volumes are cold filesystem backups. Restore to empty matching local volumes preserving
numeric uid/gid, modes and symlinks, using the same Caddy/WordPress/MariaDB versions.
Recreate matching networks and containers before serving; they are not part of the archive.
Use docker-containers.private.json, if present, to recover container configuration and
existing MariaDB credentials. It is sensitive and not safe to share.
Do not start a new WordPress initialization over restored files or MariaDB data.
''')
        for directory in (dest, BACKUPS):
            sync_directory(directory)
        return dest
    except BaseException:
        shutil.rmtree(dest)
        raise


class Cleanup:
    def __init__(self, i, launch, *, recovery):
        self.i = i
        self.recovery = recovery
        self.trees = i.recovery_trees() if recovery else list(i.trees)
        if not recovery:
            i.ownership()
        self.service = Service(i, recovery=recovery)
        data = i.data()
        shortcut = i.old_shortcut(data.get('shortcut'))
        self.files = [path for path in (*CONFIG, self.service.path, OPENRC_LOG)
                      if path is not None and root_file(path, optional=True)]
        if shortcut is not None:
            self.files.append(shortcut)
        if recovery and present(SHORTCUTS):
            root_dirs(SHORTCUTS)
            wrapper = i.wrapper()
            for path in SHORTCUTS.iterdir():
                if path in self.files or not shortcut_name(path.name):
                    continue
                try:
                    info = root_file(path)
                    if info.st_size == len(wrapper) and path.read_bytes() == wrapper:
                        self.files.append(path)
                except (OSError, Unsafe):
                    continue
        self.files.sort(key=lambda p: (p in CONFIG, p == PATHS, str(p)))
        for path in self.files:
            no_mounts(path)
        self.file_ids = {p: self.identity(p, p.lstat()) for p in self.files}
        self.tree_ids = {p: file_id(p.lstat())[:2] for p in self.trees}
        self.links = self.service.links if recovery else {}
        download = i.download(launch, launcher=recovery)
        self.downloads = {d['path']: d for d in (data.get('download'), download) if d}
        if Path(launch).name == DOWNLOAD and not download:
            print('当前下载脚本未能验证归属或内容，不会自动删除。', file=sys.stderr)

    @staticmethod
    def identity(path, info):
        identity = file_id(info)
        return identity[:2] if path == OPENRC_LOG else identity

    def websites(self):
        return Websites(self.i, recovery=self.recovery)

    def verify(self):
        i = self.i
        if self.recovery:
            require(i.recovery_trees() == self.trees, 'Installation paths changed during uninstall')
        else:
            i.ownership()
        for path, identity in self.tree_ids.items():
            info = path.lstat()
            require(file_id(info)[:2] == identity, f'Directory replaced: {path}')
        for path, identity in self.file_ids.items():
            require(self.identity(path, root_file(path)) == identity, f'Installer file changed: {path}')
            no_mounts(path)
        for link, target in self.links.items():
            root_dirs(link.parent)
            require(link.is_symlink() and link.lstat().st_uid == 0 and link.resolve() == target,
                    f'Service enablement link changed: {link}')


def cleanup_menu(i, launch):
    plan = Cleanup(i, launch, recovery=True)
    websites = plan.websites()
    if not (plan.trees or plan.files or plan.links or websites.containers or websites.networks or websites.volumes):
        print('未安装面板：未找到 Harbor Panel 的安装痕迹。')
        return False
    print('发现以下 Harbor Panel 安装痕迹；确认后将永久删除：')
    for path in (*plan.trees, *plan.files, *plan.links, *plan.downloads):
        print(f'  {path}')
    for kind, items in (('Docker 容器', websites.containers), ('Docker 网络', websites.networks),
                        ('Docker 数据卷', websites.volumes)):
        for name in sorted(items):
            print(f'  {kind}: {name}')
    try:
        while True:
            answer = input('先备份面板和网站数据？[Y/n，默认 Y]: ').lower()
            if answer in ('', 'y', 'yes', 'n', 'no'):
                break
            print('请输入 Y 或 N。')
        confirmed = input('确认永久卸载请输入 UNINSTALL: ') == 'UNINSTALL'
    except EOFError:
        confirmed = False
    if not confirmed:
        print('已取消。')
        return False
    uninstall(i, answer in ('', 'y', 'yes'), launch, plan=plan, preview=websites.signature())
    return True


def uninstall(i, backup, launch, *, plan=None, preview=None):
    import fcntl

    if plan is None:
        plan = Cleanup(i, launch, recovery=False)
    plan.verify()
    service = plan.service
    websites = None
    destructive = False
    fd = None
    try:
        if i.state in plan.trees:
            worker = i.state / 'host-settings.worker.lock'
            root_file(worker, optional=True)
            fd = os.open(worker, os.O_RDONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            host_jobs(i.state)
        service.stop()
        if i.state in plan.trees:
            host_jobs(i.state)
        websites = plan.websites()
        require(preview is None or websites.signature() == preview, 'Docker inventory changed after the scan')
        websites.stop()
        plan.verify()
        if backup:
            saved = archive_backup(i, plan.files, websites, trees=plan.trees)
            print(f'备份已完成：{saved}', flush=True)
        plan.verify()
        fresh = plan.websites()
        require(fresh.signature() == websites.signature() and not any(
            work['State']['Running'] for work in fresh.containers.values()), 'Docker inventory changed during uninstall')
        destructive = True
        # Remove the exclusive proxy and WordPress listeners before deleting their state/config.
        websites.remove()
        if service.path is not None:
            service.disable()
        for link in plan.links:
            link.unlink(missing_ok=True)
        for path in plan.files:
            if path != PATHS:
                path.unlink()
        service.reload()
        if fd is not None:
            os.close(fd)
            fd = None
        for tree in plan.trees:
            no_mounts(tree)
            shutil.rmtree(tree)
        if PATHS in plan.files:
            PATHS.unlink()
        for download in plan.downloads.values():
            i.remove_download(download)
    except BaseException:
        if not destructive:
            if websites:
                websites.restart()
            if service.was_running:
                try:
                    service.action('start')
                except (Unsafe, OSError, subprocess.SubprocessError) as error:
                    print(f'无法恢复 Harbor Panel 服务：{error}', file=sys.stderr)
        else:
            print('卸载在清理阶段中断，部分资源可能已移除。请检查上述错误后重试。', file=sys.stderr)
        raise
    finally:
        if fd is not None:
            os.close(fd)
    print('Harbor Panel 已卸载。重新安装请重新使用 curl 下载；新安装将生成一次性注册凭证。')
    print('由管理员启用的 Nginx/SSH 片段不会删除；如需清理，请先移除对应 include 并验证服务。')


def main(argv):
    action, app, state, *args = argv
    require(os.geteuid() == 0 and sys.platform == 'linux' and shutil.rmtree.avoids_symlink_attacks,
            'Native menu administration requires root on Linux')
    os.umask(0o077)
    i = Installation(app, state)
    if action in {'claim', 'adopt'}:
        if action == 'claim':
            i.ownership(claim=True)
        else:
            i.adopt()
        i.remember(args[0])
    elif action == 'shortcut':
        i.remember(args[2])
        i.shortcut(args[0], args[1])
    elif action == 'uninstall':
        require(args[0] in {'yes', 'no'}, 'Invalid backup choice')
        uninstall(i, args[0] == 'yes', args[1])
    elif action == 'cleanup':
        if not cleanup_menu(i, args[0]):
            sys.exit(3)
    else:
        raise Unsafe('Unknown menu operation')


if __name__ == '__main__':
    try:
        main(sys.argv[1:])
    except KeyboardInterrupt:
        print('操作已中断。', file=sys.stderr)
        sys.exit(130)
    except (Unsafe, OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError, tarfile.TarError) as error:
        print(f'操作失败：{error}', file=sys.stderr)
        sys.exit(1)
