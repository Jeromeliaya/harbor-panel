import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import tarfile
import unittest
from unittest.mock import Mock, patch
import uuid
import shutil
from types import SimpleNamespace


DEPLOY = Path(__file__).resolve().parents[1] / 'deploy'
SPEC = importlib.util.spec_from_file_location('menu_admin', DEPLOY / 'menu_admin.py')
admin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(admin)
SITE = '0123456789abcdef'
WP = 'harbor-wp-' + SITE
DATA = WP + '-database'


class TestMenuAdmin(unittest.TestCase):
    def fixture(self):
        folder = Path(__file__).parent / ('.menu-fixture-' + uuid.uuid4().hex)
        folder.mkdir()
        self.addCleanup(shutil.rmtree, folder)
        return folder

    def installation_fixture(self):
        root = self.fixture()
        i = admin.Installation.__new__(admin.Installation)
        i.app = root / 'app'
        i.state = root / 'state'
        i.trees = (i.app, i.state)
        i.menu = i.app / 'deploy/menu.sh'
        i.menu.parent.mkdir(parents=True)
        i.state.mkdir()
        i.records = i.state / admin.RECORDS
        return root, i

    def test_shortcut_name_accepts_literal_unicode_and_rejects_shell_syntax_and_builtins(self):
        for name in ('面板', '港口-运维2', 'écran.panel', 'e\u0301cran+1', '_harbor'):
            self.assertTrue(admin.shortcut_name(name), name)
        for name in ('cd', 'echo', 'if', 'source', 'status', '', '../../test', 'foo bar', 'x;id', '$x',
                     '`date`', '\\name', '-x', 'zero\x1b', 'a\u202eb'):
            self.assertFalse(admin.shortcut_name(name), name)

    def test_shortcut_refuses_path_collision_and_does_not_overwrite(self):
        root, i = self.installation_fixture()
        private_bin, other_bin = [root / name for name in ('private', 'other')]
        private_bin.mkdir()
        other_bin.mkdir()
        seen = other_bin / '港口'
        seen.write_bytes(b'unrelated')
        # Test just the PATH lookup with POSIX separators, without touching any real PATH.
        with patch.object(i, 'ownership'), patch.object(i, 'data', return_value={}), patch.object(admin, 'root_file'), \
                patch.object(admin, 'root_dirs'), patch.object(admin, 'SHORTCUTS', private_bin), \
                patch.object(admin, 'PATH', 'fixture-bin'), patch.object(admin, 'present',
                    side_effect=lambda p: p.name == '港口' and 'fixture-bin' in str(p)), \
                patch.object(admin, 'write_exclusive') as write:
            with self.assertRaisesRegex(admin.Unsafe, '命令名已存在'):
                i.shortcut('港口', '')
        write.assert_not_called()
        self.assertEqual(seen.read_bytes(), b'unrelated')

    def test_replaced_download_and_unrecognized_filename_are_not_removed(self):
        root, i = self.installation_fixture()
        i.menu.write_bytes(b'installer contents')
        canonical = root / admin.DOWNLOAD
        unrelated = root / 'custom_download.sh'
        for path in (canonical, unrelated):
            path.write_bytes(i.menu.read_bytes())
        with patch.object(admin, 'root_file', side_effect=lambda p, **kw: p.lstat()):
            self.assertIsNone(i.download(str(unrelated)))
            saved = i.download(str(canonical))
            self.assertIsNotNone(saved)
            canonical.write_bytes(b'replaced')
            i.remove_download(saved)
            self.assertEqual(canonical.read_bytes(), b'replaced')
            canonical.write_bytes(i.menu.read_bytes())
            i.remove_download(i.download(str(canonical)))
        self.assertFalse(canonical.exists())
        self.assertTrue(unrelated.exists())

    def test_ownership_record_cannot_be_reused_for_substituted_tree(self):
        root, i = self.installation_fixture()
        (i.app / 'server.py').touch()
        fake_ids = [[10, 20], [10, 21]]
        with patch.object(admin, 'root_file'), patch.object(i, 'saved_paths'), patch.object(admin, 'no_mounts'), \
                patch.object(i, 'tree_ids', return_value=fake_ids) as ids, \
                patch.object(admin, 'write_exclusive', side_effect=lambda p, data, *a: p.write_bytes(data)):
            with self.assertRaisesRegex(admin.Unsafe, 'ownership records'):
                i.ownership()
            i.ownership(claim=True)
            i.ownership()
            ids.return_value = [[10, 20], [10, 22]]
            with self.assertRaisesRegex(admin.Unsafe, 'does not match'):
                i.ownership()

    def test_docker_owner_requires_both_known_site_and_exact_resource_shape(self):
        w = admin.Websites.__new__(admin.Websites)
        w.wordpress = {SITE}
        w.has_proxy = True
        w.orphaned = False
        self.assertEqual(w.owner('volume', DATA, None), SITE)  # legacy implicit named volume
        self.assertEqual(w.owner('container', WP, {admin.OWNER: SITE}), SITE)
        self.assertEqual(w.owner('volume', admin.CADDY + '-data', {admin.OWNER: 'proxy'}), 'proxy')
        self.assertIsNone(w.owner('container', 'my-production-app', {}))
        for kind, name, labels in (
            ('volume', DATA, {admin.OWNER: 'wrong'}), ('volume', 'harbor-wp-1111111111111111-files', {}),
            ('container', WP, {}), ('volume', WP + '-db', {admin.OWNER: SITE}),
            ('container', 'my-production-app', {admin.OWNER: SITE}), ('volume', admin.CADDY + '-data', {}),
        ):
            with self.subTest(name=name), self.assertRaisesRegex(admin.Unsafe, 'Cannot verify ownership'):
                w.owner(kind, name, labels)

    def test_docker_inventory_refuses_shared_volume_before_any_cleanup(self):
        def inspect_reply(kind, ids):
            return iter({'container': [unrelated], 'network': [], 'volume': [volume]}[kind])

        docker = PurePosixPath('/var/lib/docker')
        mountpoint = str(docker / 'volumes' / DATA / '_data')
        volume = {'Name': DATA, 'Mountpoint': mountpoint, 'Driver': 'local', 'Scope': 'local'}
        unrelated = {'Name': '/my-other-app', 'Config': {'Labels': {}},
                     'Mounts': [{'Type': 'volume', 'Name': DATA, 'Source': mountpoint}]}
        w = admin.Websites.__new__(admin.Websites)
        w.i = SimpleNamespace(trees=(PurePosixPath('/opt/panel'), PurePosixPath('/var/lib/panel')))
        w.wordpress, w.has_proxy, w.orphaned = {SITE}, False, False
        w.containers, w.networks, w.volumes = {}, {}, {}
        w.objects = Mock(side_effect=inspect_reply)
        w.names = Mock(side_effect=[['a' * 64], [], [DATA], []])
        with patch.object(admin, 'Path', PurePosixPath), patch.object(admin, 'root_dirs'), \
                patch.object(admin, 'no_mounts'), patch.object(PurePosixPath, 'lstat', create=True,
                                                               return_value=SimpleNamespace(st_mode=0o040700)):
            with self.assertRaisesRegex(admin.Unsafe, 'Unexpected container depends on panel data'):
                w.discover(docker)
        self.assertFalse(w.containers)

    def test_archive_includes_wordpress_data_and_manifest(self):
        root, i = self.installation_fixture()
        shutil.rmtree(i.app)
        (i.state / 'audit.sqlite3').write_bytes(b'audit fixture')
        volume = root / 'volume-data'
        volume.mkdir()
        (volume / 'table').write_bytes(b'data fixture')
        env = root / 'harbor-panel.env'
        env.write_bytes(b'ENV=fixture\n')
        w = SimpleNamespace(containers={}, networks={}, volumes={DATA: {'Mountpoint': str(volume)}})
        with patch.object(admin, 'BACKUPS', root / 'backups'), patch.object(admin, 'root_dirs'), \
                patch.object(admin, 'no_mounts'), patch.object(admin, 'sync_directory'), \
                patch.object(admin, 'write_exclusive', side_effect=lambda p, data, *a: p.write_bytes(data)):
            backup = admin.archive_backup(i, [env], w, trees=[i.state])
        self.assertTrue((backup / 'RESTORE.txt').is_file())
        manifest = json.loads((backup / 'manifest.json').read_bytes())
        self.assertEqual(manifest['volumes'], [DATA])
        self.assertIn(str(env), manifest['host_files'])
        with tarfile.open(backup / 'harbor-panel.tar.gz') as file:
            self.assertNotIn('panel/app', file.getnames())
            self.assertEqual(file.extractfile('panel/state/audit.sqlite3').read(), b'audit fixture')
            self.assertEqual(file.extractfile(f'docker-volumes/{DATA}/table').read(), b'data fixture')

    def test_failed_backup_does_not_remove_panel_or_docker_resources(self):
        root, i = self.installation_fixture()
        fcntl = SimpleNamespace(flock=Mock(), LOCK_EX=1, LOCK_NB=2)
        service = Mock(was_running=True)
        sites = Mock()
        with patch.dict('sys.modules', {'fcntl': fcntl}), patch.object(i, 'ownership'), \
                patch.object(i, 'data', return_value={}), patch.object(admin, 'CONFIG', ()), \
                patch.object(admin, 'root_file', return_value=None), patch.object(admin, 'host_jobs'), \
                patch.object(admin.os, 'O_NOFOLLOW', 0, create=True), \
                patch.object(admin, 'Service', return_value=service), patch.object(admin, 'Websites', return_value=sites), \
                patch.object(admin, 'archive_backup', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError, 'disk full'):
                admin.uninstall(i, True, 'unknown.sh')
        sites.stop.assert_called_once()
        sites.restart.assert_called_once()
        service.action.assert_called_once_with('start')
        sites.remove.assert_not_called()
        service.disable.assert_not_called()
        self.assertTrue(i.app.is_dir())
        self.assertTrue(i.state.is_dir())

    def test_no_backup_removes_only_verified_files_after_website_shutdown(self):
        root, i = self.installation_fixture()
        fcntl = SimpleNamespace(flock=Mock(), LOCK_EX=1, LOCK_NB=2)
        unit, unrelated, env = [root / name for name in ('panel.service', 'other.conf', 'panel.env')]
        for file in (unit, unrelated, env):
            file.touch()
        events = []
        service = Mock(systemd=True, path=unit)
        service.stop.side_effect = lambda: events.append('stop panel')
        service.disable.side_effect = lambda: events.append('disable panel')
        sites = Mock(containers={})
        sites.stop.side_effect = lambda: events.append('stop websites')
        sites.remove.side_effect = lambda: events.append('delete websites')
        with patch.dict('sys.modules', {'fcntl': fcntl}), patch.object(i, 'ownership'), \
                patch.object(i, 'data', return_value={}), patch.object(admin, 'CONFIG', (env,)), \
                patch.object(admin, 'root_file', side_effect=lambda p, **kw: p.lstat() if p.exists() else None), \
                patch.object(admin, 'host_jobs'), \
                patch.object(admin, 'no_mounts'), patch.object(admin.os, 'O_NOFOLLOW', 0, create=True), \
                patch.object(admin, 'Service', return_value=service), patch.object(admin, 'Websites', return_value=sites), \
                patch.object(admin, 'archive_backup') as archive:
            admin.uninstall(i, False, 'unknown.sh')
        archive.assert_not_called()
        self.assertEqual(events, ['stop panel', 'stop websites', 'delete websites', 'disable panel'])
        self.assertFalse(i.app.exists())
        self.assertFalse(i.state.exists())
        self.assertFalse(unit.exists())
        self.assertFalse(env.exists())
        self.assertTrue(unrelated.exists())

    @contextlib.contextmanager
    def recovery_fixture(self, root, i):
        paths = root / 'harbor-panel.paths'
        with patch.object(admin, 'PATHS', paths), patch.object(admin, 'CONFIG', (paths,)), \
                patch.object(admin, 'OPENRC_LOG', root / 'panel.log'), patch.object(admin, 'SHORTCUTS', root / 'bin'), \
                patch.object(admin, 'root_dirs', side_effect=lambda p, **kw: p.lstat()), \
                patch.object(admin, 'root_file', side_effect=lambda p, **kw: p.lstat() if p.exists() else None), \
                patch.object(admin, 'no_mounts'):
            yield paths

    def test_option_three_cleans_orphan_state_without_reinstalling(self):
        root, i = self.installation_fixture()
        shutil.rmtree(i.app)
        (i.state / 'bootstrap.token').write_bytes(b'old token')
        (i.state / 'workspace').mkdir()
        menu = root / admin.DOWNLOAD
        menu.write_bytes(b'current menu')
        other = root / 'my-app'
        other.mkdir()
        with self.recovery_fixture(root, i) as paths:
            paths.write_bytes(f'HARBOR_PANEL_DIR={i.app}\nHARBOR_PANEL_STATE={i.state}\n'.encode())
            bin_dir = root / 'bin'
            bin_dir.mkdir()
            (bin_dir / 'panel').write_bytes(i.wrapper())
            (bin_dir / 'other').write_bytes(b'another command')
            sites = Mock(containers={}, networks={}, volumes={})
            service = Mock(path=None, links={})
            fcntl = SimpleNamespace(flock=Mock(), LOCK_EX=1, LOCK_NB=2)
            log = io.StringIO()
            with patch.dict('sys.modules', {'fcntl': fcntl}), patch.object(admin.os, 'O_NOFOLLOW', 0, create=True), \
                    patch.object(admin, 'Service', return_value=service), \
                    patch.object(admin, 'Websites', return_value=sites), contextlib.redirect_stdout(log), \
                    patch('builtins.input', side_effect=['n', 'cancel', 'n', 'UNINSTALL']):
                self.assertFalse(admin.cleanup_menu(i, str(menu)))
                self.assertTrue((i.state / 'bootstrap.token').exists())
                service.stop.assert_not_called()
                self.assertTrue(admin.cleanup_menu(i, str(menu)))
                self.assertFalse(admin.cleanup_menu(i, str(menu)))
            self.assertIn(str(i.state), log.getvalue())
            self.assertIn('未安装面板', log.getvalue())
            self.assertNotIn('old token', log.getvalue())
            self.assertFalse(paths.exists())
            self.assertFalse(i.state.exists())
            self.assertFalse(menu.exists())
            self.assertFalse((bin_dir / 'panel').exists())
            self.assertTrue((bin_dir / 'other').exists())
            self.assertTrue(other.is_dir())
            service.disable.assert_not_called()

    def test_recovery_allows_missing_systemd_unit_but_blocks_running_unit(self):
        service = admin.Service.__new__(admin.Service)
        service.path, service.systemd, service.command = None, True, 'systemctl'
        gone = 'LoadState=not-found\nActiveState=inactive\nFragmentPath=\nDropInPaths=\n'
        with patch.object(admin, 'present', return_value=False), \
                patch.object(admin, 'run', side_effect=[SimpleNamespace(stdout=gone),
                    SimpleNamespace(stdout=gone.replace('inactive', 'active'))]):
            self.assertFalse(service.active())
            with self.assertRaisesRegex(admin.Unsafe, 'still registered or running'):
                service.active()

    def test_option_three_lists_verified_docker_only_residue(self):
        root, i = self.installation_fixture()
        for tree in i.trees:
            shutil.rmtree(tree)
        w = admin.Websites.__new__(admin.Websites)
        w.wordpress, w.has_proxy, w.orphaned = set(), False, True
        self.assertEqual(w.owner('network', WP, {admin.OWNER: SITE}), SITE)
        self.assertEqual(w.owner('volume', DATA, {admin.OWNER: SITE}), SITE)
        with self.assertRaisesRegex(admin.Unsafe, 'Cannot verify ownership'):
            w.owner('volume', DATA, {})
        w.containers, w.networks, w.volumes = {}, {WP: {'Id': 'a' * 64}}, {}
        log = io.StringIO()
        with self.recovery_fixture(root, i), patch.object(admin, 'Service', return_value=Mock(path=None, links={})), \
                patch.object(admin, 'Websites', return_value=w) as discover, patch.object(admin, 'uninstall') as remove, \
                patch('builtins.input', side_effect=['n', 'UNINSTALL']), contextlib.redirect_stdout(log):
            self.assertTrue(admin.cleanup_menu(i, 'unknown.sh'))
        discover.assert_called_once_with(i, recovery=True)
        self.assertEqual(remove.call_args.kwargs['preview'], w.signature())
        self.assertIn(WP, log.getvalue())
        self.assertNotIn('?????', log.getvalue())

    def test_option_three_refuses_unknown_or_replaced_state(self):
        root, i = self.installation_fixture()
        shutil.rmtree(i.app)
        unknown = i.state / 'unrelated.txt'
        unknown.touch()
        with self.recovery_fixture(root, i) as paths:
            paths.write_bytes(f'HARBOR_PANEL_DIR={i.app}\nHARBOR_PANEL_STATE={i.state}\n'.encode())
            with self.assertRaisesRegex(admin.Unsafe, 'unrecognized contents'):
                i.recovery_trees()
            self.assertTrue(unknown.exists())
            unknown.unlink()
            marker = i.state / admin.MARKER
            marker.write_text(json.dumps({'paths': [str(p) for p in i.trees],
                                          'directories': [[10, 11], [10, 12]]}))
            with self.assertRaisesRegex(admin.Unsafe, 'Directory identity'):
                i.recovery_trees()
            info = i.state.stat()
            marker.write_text(json.dumps({'paths': [str(p) for p in i.trees],
                                          'directories': [[10, 11], [info.st_dev, info.st_ino]]}))
            self.assertEqual(i.recovery_trees(), [i.state])

    def test_downloaded_menu_cleanup_runs_with_no_installed_helper(self):
        sh = shutil.which('sh')
        if sh is None:
            self.skipTest('A POSIX shell is not available')
        menu = (DEPLOY / 'menu.sh').read_text(encoding='utf-8')
        root = self.fixture()
        helper = root / 'helper.py'
        helper.write_bytes((DEPLOY / 'menu_admin.py').read_bytes().replace(b'\r\n', b'\n'))
        self.assertIn(f"helper_sha='{hashlib.sha256(helper.read_bytes()).hexdigest()}'", menu)
        prompt = menu[menu.index('uninstall_panel()'):]
        fake = '''
APP_DIR=/not-a-real-harbor-test
STATE_DIR=/not-a-real-harbor-state
curl() {
    for destination do :; done
    if [ "$TAMPER" = yes ]; then printf tampered > "$destination"; else cp -- "$FIXTURE" "$destination"; fi
}
python3() { printf 'called cleanup: %s %s %s\\n' "$3" "$4" "$5"; }
'''
        for tamper in ('no', 'yes'):
            with self.subTest(tamper=tamper):
                env = dict(os.environ, FIXTURE=helper.as_posix(), TAMPER=tamper)
                result = subprocess.run([sh, '-c', fake + prompt], input=b'3\n0\n', capture_output=True, env=env)
                self.assertEqual(result.returncode, 0, result.stderr)
                if tamper == 'yes':
                    self.assertNotIn(b'called cleanup', result.stdout)
                    self.assertIn('清理组件版本不匹配'.encode(), result.stderr)
                else:
                    self.assertIn(b'called cleanup: cleanup /not-a-real-harbor-test /not-a-real-harbor-state',
                                  result.stdout)

    def test_active_or_failed_rollback_journal_blocks_uninstall(self):
        root, i = self.installation_fixture()
        path = i.state / 'host-settings.jobs.json'
        for status in ('queued', 'awaiting_confirmation', 'running', 'rollback_failed'):
            path.write_text(json.dumps([{'status': status}]), encoding='utf-8')
            with patch.object(admin, 'root_file'), self.assertRaisesRegex(admin.Unsafe, 'Resolve or roll back'):
                admin.host_jobs(i.state)
        path.write_text('[{"status":"rolled_back"}]', encoding='utf-8')
        with patch.object(admin, 'root_file'):
            admin.host_jobs(i.state)

    def test_shell_menu_dispatch_isolated_from_host(self):
        sh = shutil.which('sh')
        if sh is None:
            self.skipTest('A POSIX shell is not available')
        source = (DEPLOY / 'menu.sh').read_text(encoding='utf-8')
        start = source.index('set_shortcut() {')
        prompt = source[start:]
        fake = ('APP_DIR=/not-a-real-panel-test\nCALLER_PATH=caller-path\n'
                'menu_admin() { printf "mock <%s>\\n" "$@"; }\n')
        result = subprocess.run([sh, '-c', fake + prompt], input='5\n港口\n0\n'.encode(), capture_output=True)
        stdout = result.stdout.decode()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('5. 设置面板快捷', stdout)
        self.assertIn('mock <shortcut>\nmock <港口>\nmock <caller-path>', stdout)
