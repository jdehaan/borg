from contextlib import contextmanager
import filecmp
import functools
import os

try:
    import posix
except ImportError:
    posix = None

import stat
import sys
import sysconfig
import tempfile
import time
import unittest

from ..xattr import get_all
from ..platform import get_flags
from ..helpers import umount
from ..helpers import EXIT_SUCCESS, EXIT_WARNING, EXIT_ERROR
from .. import platform

# Note: this is used by borg.selftest, do not use or import py.test functionality here.

from ..fuse_impl import llfuse, has_pyfuse3, has_llfuse

# Does this version of llfuse support ns precision?
have_fuse_mtime_ns = hasattr(llfuse.EntryAttributes, "st_mtime_ns") if llfuse else False

try:
    from pytest import raises
except:  # noqa
    raises = None

has_lchflags = hasattr(os, "lchflags") or sys.platform.startswith("linux")
try:
    with tempfile.NamedTemporaryFile() as file:
        platform.set_flags(file.name, stat.UF_NODUMP)
except OSError:
    has_lchflags = False

# The mtime get/set precision varies on different OS and Python versions
if posix and "HAVE_FUTIMENS" in getattr(posix, "_have_functions", []):
    st_mtime_ns_round = 0  # 1ns resolution
elif "HAVE_UTIMES" in sysconfig.get_config_vars():
    st_mtime_ns_round = -3  # 1us resolution
else:
    st_mtime_ns_round = -9  # 1s resolution

if sys.platform.startswith("netbsd"):
    st_mtime_ns_round = -4  # 10us - strange: only >1 microsecond resolution here?


def same_ts_ns(ts_ns1, ts_ns2):
    """compare 2 timestamps (both in nanoseconds) whether they are (roughly) equal"""
    diff_ts = int(abs(ts_ns1 - ts_ns2))
    diff_max = 10 ** (-st_mtime_ns_round)
    return diff_ts <= diff_max


@contextmanager
def unopened_tempfile():
    with tempfile.TemporaryDirectory() as tempdir:
        yield os.path.join(tempdir, "file")


@functools.lru_cache
def are_symlinks_supported():
    with unopened_tempfile() as filepath:
        try:
            os.symlink("somewhere", filepath)
            if os.stat(filepath, follow_symlinks=False) and os.readlink(filepath) == "somewhere":
                return True
        except OSError:
            pass
    return False


@functools.lru_cache
def are_hardlinks_supported():
    if not hasattr(os, "link"):
        # some pythons do not have os.link
        return False

    with unopened_tempfile() as file1path, unopened_tempfile() as file2path:
        open(file1path, "w").close()
        try:
            os.link(file1path, file2path)
            stat1 = os.stat(file1path)
            stat2 = os.stat(file2path)
            if stat1.st_nlink == stat2.st_nlink == 2 and stat1.st_ino == stat2.st_ino:
                return True
        except OSError:
            pass
    return False


@functools.lru_cache
def are_fifos_supported():
    with unopened_tempfile() as filepath:
        try:
            os.mkfifo(filepath)
            return True
        except OSError:
            pass
        except NotImplementedError:
            pass
        except AttributeError:
            pass
        return False


@functools.lru_cache
def is_utime_fully_supported():
    with unopened_tempfile() as filepath:
        # Some filesystems (such as SSHFS) don't support utime on symlinks
        if are_symlinks_supported():
            os.symlink("something", filepath)
        else:
            open(filepath, "w").close()
        try:
            os.utime(filepath, (1000, 2000), follow_symlinks=False)
            new_stats = os.stat(filepath, follow_symlinks=False)
            if new_stats.st_atime == 1000 and new_stats.st_mtime == 2000:
                return True
        except OSError:
            pass
        except NotImplementedError:
            pass
        return False


@functools.lru_cache
def is_birthtime_fully_supported():
    if not hasattr(os.stat_result, "st_birthtime"):
        return False
    with unopened_tempfile() as filepath:
        # Some filesystems (such as SSHFS) don't support utime on symlinks
        if are_symlinks_supported():
            os.symlink("something", filepath)
        else:
            open(filepath, "w").close()
        try:
            birthtime, mtime, atime = 946598400, 946684800, 946771200
            os.utime(filepath, (atime, birthtime), follow_symlinks=False)
            os.utime(filepath, (atime, mtime), follow_symlinks=False)
            new_stats = os.stat(filepath, follow_symlinks=False)
            if new_stats.st_birthtime == birthtime and new_stats.st_mtime == mtime and new_stats.st_atime == atime:
                return True
        except OSError:
            pass
        except NotImplementedError:
            pass
        return False


def no_selinux(x):
    # selinux fails our FUSE tests, thus ignore selinux xattrs
    SELINUX_KEY = b"security.selinux"
    if isinstance(x, dict):
        return {k: v for k, v in x.items() if k != SELINUX_KEY}
    if isinstance(x, list):
        return [k for k in x if k != SELINUX_KEY]


class BaseTestCase(unittest.TestCase):
    """ """

    assert_in = unittest.TestCase.assertIn
    assert_not_in = unittest.TestCase.assertNotIn
    assert_equal = unittest.TestCase.assertEqual
    assert_not_equal = unittest.TestCase.assertNotEqual

    if raises:
        assert_raises = staticmethod(raises)
    else:
        assert_raises = unittest.TestCase.assertRaises  # type: ignore

    @contextmanager
    def assert_creates_file(self, path):
        assert not os.path.exists(path), f"{path} should not exist"
        yield
        assert os.path.exists(path), f"{path} should exist"

    def assert_dirs_equal(self, dir1, dir2, **kwargs):
        diff = filecmp.dircmp(dir1, dir2)
        self._assert_dirs_equal_cmp(diff, **kwargs)

    def _assert_dirs_equal_cmp(self, diff, ignore_flags=False, ignore_xattrs=False, ignore_ns=False):
        self.assert_equal(diff.left_only, [])
        self.assert_equal(diff.right_only, [])
        self.assert_equal(diff.diff_files, [])
        self.assert_equal(diff.funny_files, [])
        for filename in diff.common:
            path1 = os.path.join(diff.left, filename)
            path2 = os.path.join(diff.right, filename)
            s1 = os.stat(path1, follow_symlinks=False)
            s2 = os.stat(path2, follow_symlinks=False)
            # Assume path2 is on FUSE if st_dev is different
            fuse = s1.st_dev != s2.st_dev
            attrs = ["st_uid", "st_gid", "st_rdev"]
            if not fuse or not os.path.isdir(path1):
                # dir nlink is always 1 on our FUSE filesystem
                attrs.append("st_nlink")
            d1 = [filename] + [getattr(s1, a) for a in attrs]
            d2 = [filename] + [getattr(s2, a) for a in attrs]
            d1.insert(1, oct(s1.st_mode))
            d2.insert(1, oct(s2.st_mode))
            if not ignore_flags:
                d1.append(get_flags(path1, s1))
                d2.append(get_flags(path2, s2))
            # ignore st_rdev if file is not a block/char device, fixes #203
            if not stat.S_ISCHR(s1.st_mode) and not stat.S_ISBLK(s1.st_mode):
                d1[4] = None
            if not stat.S_ISCHR(s2.st_mode) and not stat.S_ISBLK(s2.st_mode):
                d2[4] = None
            # If utime isn't fully supported, borg can't set mtime.
            # Therefore, we shouldn't test it in that case.
            if is_utime_fully_supported():
                # Older versions of llfuse do not support ns precision properly
                if ignore_ns:
                    d1.append(int(s1.st_mtime_ns / 1e9))
                    d2.append(int(s2.st_mtime_ns / 1e9))
                elif fuse and not have_fuse_mtime_ns:
                    d1.append(round(s1.st_mtime_ns, -4))
                    d2.append(round(s2.st_mtime_ns, -4))
                else:
                    d1.append(round(s1.st_mtime_ns, st_mtime_ns_round))
                    d2.append(round(s2.st_mtime_ns, st_mtime_ns_round))
            if not ignore_xattrs:
                d1.append(no_selinux(get_all(path1, follow_symlinks=False)))
                d2.append(no_selinux(get_all(path2, follow_symlinks=False)))
            self.assert_equal(d1, d2)
        for sub_diff in diff.subdirs.values():
            self._assert_dirs_equal_cmp(
                sub_diff, ignore_flags=ignore_flags, ignore_xattrs=ignore_xattrs, ignore_ns=ignore_ns
            )

    @contextmanager
    def fuse_mount(self, location, mountpoint=None, *options, fork=True, os_fork=False, **kwargs):
        # For a successful mount, `fork = True` is required for
        # the borg mount daemon to work properly or the tests
        # will just freeze. Therefore, if argument `fork` is not
        # specified, the default value is `True`, regardless of
        # `FORK_DEFAULT`. However, leaving the possibility to run
        # the command with `fork = False` is still necessary for
        # testing for mount failures, for example attempting to
        # mount a read-only repo.
        #    `os_fork = True` is needed for testing (the absence of)
        # a race condition of the Lock during lock migration when
        # borg mount (local repo) is daemonizing (#4953). This is another
        # example where we need `fork = False`, because the test case
        # needs an OS fork, not a spawning of the fuse mount.
        # `fork = False` is implied if `os_fork = True`.
        if mountpoint is None:
            mountpoint = tempfile.mkdtemp()
        else:
            os.mkdir(mountpoint)
        args = [f"--repo={location}", "mount", mountpoint] + list(options)
        if os_fork:
            # Do not spawn, but actually (OS) fork.
            if os.fork() == 0:
                # The child process.
                # Decouple from parent and fork again.
                # Otherwise, it becomes a zombie and pretends to be alive.
                os.setsid()
                if os.fork() > 0:
                    os._exit(0)
                # The grandchild process.
                try:
                    self.cmd(*args, fork=False, **kwargs)  # borg mount not spawning.
                finally:
                    # This should never be reached, since it daemonizes,
                    # and the grandchild process exits before cmd() returns.
                    # However, just in case...
                    print("Fatal: borg mount did not daemonize properly. Force exiting.", file=sys.stderr, flush=True)
                    os._exit(0)
        else:
            self.cmd(*args, fork=fork, **kwargs)
            if kwargs.get("exit_code", EXIT_SUCCESS) == EXIT_ERROR:
                # If argument `exit_code = EXIT_ERROR`, then this call
                # is testing the behavior of an unsuccessful mount and
                # we must not continue, as there is no mount to work
                # with. The test itself has already failed or succeeded
                # with the call to `self.cmd`, above.
                yield
                return
        self.wait_for_mountstate(mountpoint, mounted=True)
        yield
        umount(mountpoint)
        self.wait_for_mountstate(mountpoint, mounted=False)
        os.rmdir(mountpoint)
        # Give the daemon some time to exit
        time.sleep(0.2)

    def wait_for_mountstate(self, mountpoint, *, mounted, timeout=5):
        """Wait until a path meets specified mount point status"""
        timeout += time.time()
        while timeout > time.time():
            if os.path.ismount(mountpoint) == mounted:
                return
            time.sleep(0.1)
        message = "Waiting for {} of {}".format("mount" if mounted else "umount", mountpoint)
        raise TimeoutError(message)

    @contextmanager
    def read_only(self, path):
        """Some paths need to be made read-only for testing

        If the tests are executed inside a fakeroot environment, the
        changes from chmod won't affect the real permissions of that
        folder. This issue is circumvented by temporarily disabling
        fakeroot with `LD_PRELOAD=`.

        Using chmod to remove write permissions is not enough if the
        tests are running with root privileges. Instead, the folder is
        rendered immutable with chattr or chflags, respectively.
        """
        if sys.platform.startswith("linux"):
            cmd_immutable = 'chattr +i "%s"' % path
            cmd_mutable = 'chattr -i "%s"' % path
        elif sys.platform.startswith(("darwin", "freebsd", "netbsd", "openbsd")):
            cmd_immutable = 'chflags uchg "%s"' % path
            cmd_mutable = 'chflags nouchg "%s"' % path
        elif sys.platform.startswith("sunos"):  # openindiana
            cmd_immutable = 'chmod S+vimmutable "%s"' % path
            cmd_mutable = 'chmod S-vimmutable "%s"' % path
        else:
            message = "Testing read-only repos is not supported on platform %s" % sys.platform
            self.skipTest(message)
        try:
            os.system('LD_PRELOAD= chmod -R ugo-w "%s"' % path)
            os.system(cmd_immutable)
            yield
        finally:
            # Restore permissions to ensure clean-up doesn't fail
            os.system(cmd_mutable)
            os.system('LD_PRELOAD= chmod -R ugo+w "%s"' % path)


class changedir:
    def __init__(self, dir):
        self.dir = dir

    def __enter__(self):
        self.old = os.getcwd()
        os.chdir(self.dir)

    def __exit__(self, *args, **kw):
        os.chdir(self.old)


class environment_variable:
    def __init__(self, **values):
        self.values = values
        self.old_values = {}

    def __enter__(self):
        for k, v in self.values.items():
            self.old_values[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def __exit__(self, *args, **kw):
        for k, v in self.old_values.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class FakeInputs:
    """Simulate multiple user inputs, can be used as input() replacement"""

    def __init__(self, inputs):
        self.inputs = inputs

    def __call__(self, prompt=None):
        if prompt is not None:
            print(prompt, end="")
        try:
            return self.inputs.pop(0)
        except IndexError:
            raise EOFError from None
