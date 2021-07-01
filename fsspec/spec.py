import io
import logging
import os
import threading
import warnings
import weakref
from distutils.version import LooseVersion
from errno import ESPIPE
from glob import has_magic
from hashlib import sha256

from .callbacks import as_callback, branch
from .config import apply_config, conf
from .dircache import DirCache
from .transaction import Transaction
from .utils import (
    get_package_version_without_import,
    other_paths,
    read_block,
    stringify_path,
    tokenize,
)

logger = logging.getLogger("fsspec")


def make_instance(cls, args, kwargs):
    return cls(*args, **kwargs)


class _Cached(type):
    """
    Metaclass for caching file system instances.

    Notes
    -----
    Instances are cached according to

    * The values of the class attributes listed in `_extra_tokenize_attributes`
    * The arguments passed to ``__init__``.

    This creates an additional reference to the filesystem, which prevents the
    filesystem from being garbage collected when all *user* references go away.
    A call to the :meth:`AbstractFileSystem.clear_instance_cache` must *also*
    be made for a filesystem instance to be garbage collected.
    """

    def __init__(cls, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Note: we intentionally create a reference here, to avoid garbage
        # collecting instances when all other references are gone. To really
        # delete a FileSystem, the cache must be cleared.
        if conf.get("weakref_instance_cache"):  # pragma: no cover
            # debug option for analysing fork/spawn conditions
            cls._cache = weakref.WeakValueDictionary()
        else:
            cls._cache = {}
        cls._pid = os.getpid()

    def __call__(cls, *args, **kwargs):
        kwargs = apply_config(cls, kwargs)
        extra_tokens = tuple(
            getattr(cls, attr, None) for attr in cls._extra_tokenize_attributes
        )
        token = tokenize(
            cls, cls._pid, threading.get_ident(), *args, *extra_tokens, **kwargs
        )
        skip = kwargs.pop("skip_instance_cache", False)
        if os.getpid() != cls._pid:
            cls._cache.clear()
            cls._pid = os.getpid()
        if not skip and cls.cachable and token in cls._cache:
            return cls._cache[token]
        else:
            obj = super().__call__(*args, **kwargs)
            # Setting _fs_token here causes some static linters to complain.
            obj._fs_token_ = token
            obj.storage_args = args
            obj.storage_options = kwargs
            if obj.async_impl:
                from .asyn import mirror_sync_methods

                mirror_sync_methods(obj)

            if cls.cachable and not skip:
                cls._cache[token] = obj
            return obj


pa_version = get_package_version_without_import("pyarrow")
if pa_version and LooseVersion(pa_version) < LooseVersion("2.0"):
    try:
        import pyarrow as pa

        up = pa.filesystem.DaskFileSystem
    except ImportError:  # pragma: no cover
        # pyarrow exists but doesn't import for some reason
        up = object
else:  # pragma: no cover
    up = object


class AbstractFileSystem(up, metaclass=_Cached):
    """
    An abstract super-class for pythonic file-systems

    Implementations are expected to be compatible with or, better, subclass
    from here.
    """

    cachable = True  # this class can be cached, instances reused
    _cached = False
    blocksize = 2 ** 22
    sep = "/"
    protocol = "abstract"
    async_impl = False
    root_marker = ""  # For some FSs, may require leading '/' or other character

    #: Extra *class attributes* that should be considered when hashing.
    _extra_tokenize_attributes = ()

    def __init__(self, *args, **storage_options):
        """Create and configure file-system instance

        Instances may be cachable, so if similar enough arguments are seen
        a new instance is not required. The token attribute exists to allow
        implementations to cache instances if they wish.

        A reasonable default should be provided if there are no arguments.

        Subclasses should call this method.

        Parameters
        ----------
        use_listings_cache, listings_expiry_time, max_paths:
            passed to ``DirCache``, if the implementation supports
            directory listing caching. Pass use_listings_cache=False
            to disable such caching.
        skip_instance_cache: bool
            If this is a cachable implementation, pass True here to force
            creating a new instance even if a matching instance exists, and prevent
            storing this instance.
        asynchronous: bool
        loop: asyncio-compatible IOLoop or None
        """
        if self._cached:
            # reusing instance, don't change
            return
        self._cached = True
        self._intrans = False
        self._transaction = None
        self._invalidated_caches_in_transaction = []
        self.dircache = DirCache(**storage_options)

        if storage_options.pop("add_docs", None):
            warnings.warn("add_docs is no longer supported.", FutureWarning)

        if storage_options.pop("add_aliases", None):
            warnings.warn("add_aliases has been removed.", FutureWarning)
        # This is set in _Cached
        self._fs_token_ = None

    @property
    def _fs_token(self):
        return self._fs_token_

    def __dask_tokenize__(self):
        return self._fs_token

    def __hash__(self):
        return int(self._fs_token, 16)

    def __eq__(self, other):
        return isinstance(other, type(self)) and self._fs_token == other._fs_token

    def __reduce__(self):
        return make_instance, (type(self), self.storage_args, self.storage_options)

    @classmethod
    def _strip_protocol(cls, path):
        """Turn path from fully-qualified to file-system-specific

        May require FS-specific handling, e.g., for relative paths or links.
        """
        if isinstance(path, list):
            return [cls._strip_protocol(p) for p in path]
        path = stringify_path(path)
        protos = (cls.protocol,) if isinstance(cls.protocol, str) else cls.protocol
        for protocol in protos:
            if path.startswith(protocol + "://"):
                path = path[len(protocol) + 3 :]
            elif path.startswith(protocol + "::"):
                path = path[len(protocol) + 2 :]
        path = path.rstrip("/")
        # use of root_marker to make minimum required path, e.g., "/"
        return path or cls.root_marker

    @staticmethod
    def _get_kwargs_from_urls(path):
        """If kwargs can be encoded in the paths, extract them here

        This should happen before instantiation of the class; incoming paths
        then should be amended to strip the options in methods.

        Examples may look like an sftp path "sftp://user@host:/my/path", where
        the user and host should become kwargs and later get stripped.
        """
        # by default, nothing happens
        return {}

    @classmethod
    def current(cls):
        """Return the most recently created FileSystem

        If no instance has been created, then create one with defaults
        """
        if not len(cls._cache):
            return cls()
        else:
            return list(cls._cache.values())[-1]

    @property
    def transaction(self):
        """A context within which files are committed together upon exit

        Requires the file class to implement `.commit()` and `.discard()`
        for the normal and exception cases.
        """
        if self._transaction is None:
            self._transaction = Transaction(self)
        return self._transaction

    def start_transaction(self):
        """Begin write transaction for deferring files, non-context version"""
        self._intrans = True
        self._transaction = Transaction(self)
        return self.transaction

    def end_transaction(self):
        """Finish write transaction, non-context version"""
        self.transaction.complete()
        self._transaction = None
        # The invalid cache must be cleared after the transcation is completed.
        for path in self._invalidated_caches_in_transaction:
            self.invalidate_cache(path)
        self._invalidated_caches_in_transaction.clear()

    def invalidate_cache(self, path=None):
        """
        Discard any cached directory information

        Parameters
        ----------
        path: string or None
            If None, clear all listings cached else listings at or under given
            path.
        """
        # Not necessary to implement invalidation mechanism, may have no cache.
        # But if have, you should call this method of parent class from your
        # subclass to ensure expiring caches after transacations correctly.
        # See the implementaion of FTPFileSystem in ftp.py
        if self._intrans:
            self._invalidated_caches_in_transaction.append(path)

    def mkdir(self, path, create_parents=True, **kwargs):
        """
        Create directory entry at path

        For systems that don't have true directories, may create an for
        this instance only and not touch the real filesystem

        Parameters
        ----------
        path: str
            location
        create_parents: bool
            if True, this is equivalent to ``makedirs``
        kwargs:
            may be permissions, etc.
        """
        pass  # not necessary to implement, may not have directories

    def makedirs(self, path, exist_ok=False):
        """Recursively make directories

        Creates directory at path and any intervening required directories.
        Raises exception if, for instance, the path already exists but is a
        file.

        Parameters
        ----------
        path: str
            leaf directory name
        exist_ok: bool (False)
            If True, will error if the target already exists
        """
        pass  # not necessary to implement, may not have directories

    def rmdir(self, path):
        """Remove a directory, if empty"""
        pass  # not necessary to implement, may not have directories

    def ls(self, path, detail=True, **kwargs):
        """List objects at path.

        This should include subdirectories and files at that location. The
        difference between a file and a directory must be clear when details
        are requested.

        The specific keys, or perhaps a FileInfo class, or similar, is TBD,
        but must be consistent across implementations.
        Must include:

        - full path to the entry (without protocol)
        - size of the entry, in bytes. If the value cannot be determined, will
          be ``None``.
        - type of entry, "file", "directory" or other

        Additional information
        may be present, aproriate to the file-system, e.g., generation,
        checksum, etc.

        May use refresh=True|False to allow use of self._ls_from_cache to
        check for a saved listing and avoid calling the backend. This would be
        common where listing may be expensive.

        Parameters
        ----------
        path: str
        detail: bool
            if True, gives a list of dictionaries, where each is the same as
            the result of ``info(path)``. If False, gives a list of paths
            (str).
        kwargs: may have additional backend-specific options, such as version
            information

        Returns
        -------
        List of strings if detail is False, or list of directory information
        dicts if detail is True.
        """
        raise NotImplementedError

    def _ls_from_cache(self, path):
        """Check cache for listing

        Returns listing, if found (may me empty list for a directly that exists
        but contains nothing), None if not in cache.
        """
        parent = self._parent(path)
        if path.rstrip("/") in self.dircache:
            return self.dircache[path.rstrip("/")]
        try:
            files = [
                f
                for f in self.dircache[parent]
                if f["name"] == path
                or (f["name"] == path.rstrip("/") and f["type"] == "directory")
            ]
            if len(files) == 0:
                # parent dir was listed but did not contain this file
                raise FileNotFoundError(path)
            return files
        except KeyError:
            pass

    def walk(self, path, maxdepth=None, **kwargs):
        """Return all files belows path

        List all files, recursing into subdirectories; output is iterator-style,
        like ``os.walk()``. For a simple list of files, ``find()`` is available.

        Note that the "files" outputted will include anything that is not
        a directory, such as links.

        Parameters
        ----------
        path: str
            Root to recurse into
        maxdepth: int
            Maximum recursion depth. None means limitless, but not recommended
            on link-based file-systems.
        kwargs: passed to ``ls``
        """
        path = self._strip_protocol(path)
        full_dirs = {}
        dirs = {}
        files = {}

        detail = kwargs.pop("detail", False)
        try:
            listing = self.ls(path, detail=True, **kwargs)
        except (FileNotFoundError, IOError):
            if detail:
                return path, {}, {}
            return path, [], []

        for info in listing:
            # each info name must be at least [path]/part , but here
            # we check also for names like [path]/part/
            pathname = info["name"].rstrip("/")
            name = pathname.rsplit("/", 1)[-1]
            if info["type"] == "directory" and pathname != path:
                # do not include "self" path
                full_dirs[pathname] = info
                dirs[name] = info
            elif pathname == path:
                # file-like with same name as give path
                files[""] = info
            else:
                files[name] = info

        if detail:
            yield path, dirs, files
        else:
            yield path, list(dirs), list(files)

        if maxdepth is not None:
            maxdepth -= 1
            if maxdepth < 1:
                return

        for d in full_dirs:
            yield from self.walk(d, maxdepth=maxdepth, detail=detail, **kwargs)

    def find(self, path, maxdepth=None, withdirs=False, **kwargs):
        """List all files below path.

        Like posix ``find`` command without conditions

        Parameters
        ----------
        path : str
        maxdepth: int or None
            If not None, the maximum number of levels to descend
        withdirs: bool
            Whether to include directory paths in the output. This is True
            when used by glob, but users usually only want files.
        kwargs are passed to ``ls``.
        """
        # TODO: allow equivalent of -name parameter
        path = self._strip_protocol(path)
        out = dict()
        detail = kwargs.pop("detail", False)
        for _, dirs, files in self.walk(path, maxdepth, detail=True, **kwargs):
            if withdirs:
                files.update(dirs)
            out.update({info["name"]: info for name, info in files.items()})
        if not out and self.isfile(path):
            # walk works on directories, but find should also return [path]
            # when path happens to be a file
            out[path] = {}
        names = sorted(out)
        if not detail:
            return names
        else:
            return {name: out[name] for name in names}

    def du(self, path, total=True, maxdepth=None, **kwargs):
        """Space used by files within a path

        Parameters
        ----------
        path: str
        total: bool
            whether to sum all the file sizes
        maxdepth: int or None
            maximum number of directory levels to descend, None for unlimited.
        kwargs: passed to ``ls``

        Returns
        -------
        Dict of {fn: size} if total=False, or int otherwise, where numbers
        refer to bytes used.
        """
        sizes = {}
        for f in self.find(path, maxdepth=maxdepth, **kwargs):
            info = self.info(f)
            sizes[info["name"]] = info["size"]
        if total:
            return sum(sizes.values())
        else:
            return sizes

    def glob(self, path, **kwargs):
        """
        Find files by glob-matching.

        If the path ends with '/' and does not contain "*", it is essentially
        the same as ``ls(path)``, returning only files.

        We support ``"**"``,
        ``"?"`` and ``"[..]"``. We do not support ^ for pattern negation.

        Search path names that contain embedded characters special to this
        implementation of glob may not produce expected results;
        e.g., 'foo/bar/*starredfilename*'.

        kwargs are passed to ``ls``.
        """
        import re

        ends = path.endswith("/")
        path = self._strip_protocol(path)
        indstar = path.find("*") if path.find("*") >= 0 else len(path)
        indques = path.find("?") if path.find("?") >= 0 else len(path)
        indbrace = path.find("[") if path.find("[") >= 0 else len(path)

        ind = min(indstar, indques, indbrace)

        detail = kwargs.pop("detail", False)

        if not has_magic(path):
            root = path
            depth = 1
            if ends:
                path += "/*"
            elif self.exists(path):
                if not detail:
                    return [path]
                else:
                    return {path: self.info(path)}
            else:
                if not detail:
                    return []  # glob of non-existent returns empty
                else:
                    return {}
        elif "/" in path[:ind]:
            ind2 = path[:ind].rindex("/")
            root = path[: ind2 + 1]
            depth = None if "**" in path else path[ind2 + 1 :].count("/") + 1
        else:
            root = ""
            depth = None if "**" in path else path[ind + 1 :].count("/") + 1

        allpaths = self.find(root, maxdepth=depth, withdirs=True, detail=True, **kwargs)
        # Escape characters special to python regex, leaving our supported
        # special characters in place.
        # See https://www.gnu.org/software/bash/manual/html_node/Pattern-Matching.html
        # for shell globbing details.
        pattern = (
            "^"
            + (
                path.replace("\\", r"\\")
                .replace(".", r"\.")
                .replace("+", r"\+")
                .replace("//", "/")
                .replace("(", r"\(")
                .replace(")", r"\)")
                .replace("|", r"\|")
                .replace("^", r"\^")
                .replace("$", r"\$")
                .replace("{", r"\{")
                .replace("}", r"\}")
                .rstrip("/")
                .replace("?", ".")
            )
            + "$"
        )
        pattern = re.sub("[*]{2}", "=PLACEHOLDER=", pattern)
        pattern = re.sub("[*]", "[^/]*", pattern)
        pattern = re.compile(pattern.replace("=PLACEHOLDER=", ".*"))
        out = {
            p: allpaths[p]
            for p in sorted(allpaths)
            if pattern.match(p.replace("//", "/").rstrip("/"))
        }
        if detail:
            return out
        else:
            return list(out)

    def exists(self, path, **kwargs):
        """Is there a file at the given path"""
        try:
            self.info(path, **kwargs)
            return True
        except:  # noqa: E722
            # any exception allowed bar FileNotFoundError?
            return False

    def info(self, path, **kwargs):
        """Give details of entry at path

        Returns a single dictionary, with exactly the same information as ``ls``
        would with ``detail=True``.

        The default implementation should calls ls and could be overridden by a
        shortcut. kwargs are passed on to ```ls()``.

        Some file systems might not be able to measure the file's size, in
        which case, the returned dict will include ``'size': None``.

        Returns
        -------
        dict with keys: name (full path in the FS), size (in bytes), type (file,
        directory, or something else) and other FS-specific keys.
        """
        path = self._strip_protocol(path)
        out = self.ls(self._parent(path), detail=True, **kwargs)
        out = [o for o in out if o["name"].rstrip("/") == path]
        if out:
            return out[0]
        out = self.ls(path, detail=True, **kwargs)
        path = path.rstrip("/")
        out1 = [o for o in out if o["name"].rstrip("/") == path]
        if len(out1) == 1:
            if "size" not in out1[0]:
                out1[0]["size"] = None
            return out1[0]
        elif len(out1) > 1 or out:
            return {"name": path, "size": 0, "type": "directory"}
        else:
            raise FileNotFoundError(path)

    def checksum(self, path):
        """Unique value for current version of file

        If the checksum is the same from one moment to another, the contents
        are guaranteed to be the same. If the checksum changes, the contents
        *might* have changed.

        This should normally be overridden; default will probably capture
        creation/modification timestamp (which would be good) or maybe
        access timestamp (which would be bad)
        """
        return int(tokenize(self.info(path)), 16)

    def size(self, path):
        """Size in bytes of file"""
        return self.info(path).get("size", None)

    def isdir(self, path):
        """Is this entry directory-like?"""
        try:
            return self.info(path)["type"] == "directory"
        except IOError:
            return False

    def isfile(self, path):
        """Is this entry file-like?"""
        try:
            return self.info(path)["type"] == "file"
        except:  # noqa: E722
            return False

    def cat_file(self, path, start=None, end=None, **kwargs):
        """Get the content of a file

        Parameters
        ----------
        path: URL of file on this filesystems
        start, end: int
            Bytes limits of the read. If negative, backwards from end,
            like usual python slices. Either can be None for start or
            end of file, respectively
        kwargs: passed to ``open()``.
        """
        # explicitly set buffering off?
        with self.open(path, "rb", **kwargs) as f:
            if start is not None:
                if start >= 0:
                    f.seek(start)
                else:
                    f.seek(max(0, f.size + start))
            if end is not None:
                if end < 0:
                    end = f.size + end
                return f.read(end - f.tell())
            return f.read()

    def pipe_file(self, path, value, **kwargs):
        """Set the bytes of given file"""
        with self.open(path, "wb") as f:
            f.write(value)

    def pipe(self, path, value=None, **kwargs):
        """Put value into path

        (counterpart to ``cat``)
        Parameters
        ----------
        path: string or dict(str, bytes)
            If a string, a single remote location to put ``value`` bytes; if a dict,
            a mapping of {path: bytesvalue}.
        value: bytes, optional
            If using a single path, these are the bytes to put there. Ignored if
            ``path`` is a dict
        """
        if isinstance(path, str):
            self.pipe_file(self._strip_protocol(path), value, **kwargs)
        elif isinstance(path, dict):
            for k, v in path.items():
                self.pipe_file(self._strip_protocol(k), v, **kwargs)
        else:
            raise ValueError("path must be str or dict")

    def cat(self, path, recursive=False, on_error="raise", **kwargs):
        """Fetch (potentially multiple) paths' contents

        Returns a dict of {path: contents} if there are multiple paths
        or the path has been otherwise expanded

        on_error : "raise", "omit", "return"
            If raise, an underlying exception will be raised (converted to KeyError
            if the type is in self.missing_exceptions); if omit, keys with exception
            will simply not be included in the output; if "return", all keys are
            included in the output, but the value will be bytes or an exception
            instance.
        """
        paths = self.expand_path(path, recursive=recursive)
        if (
            len(paths) > 1
            or isinstance(path, list)
            or paths[0] != self._strip_protocol(path)
        ):
            out = {}
            for path in paths:
                try:
                    out[path] = self.cat_file(path, **kwargs)
                except Exception as e:
                    if on_error == "raise":
                        raise
                    if on_error == "return":
                        out[path] = e
            return out
        else:
            return self.cat_file(paths[0], **kwargs)

    def get_file(self, rpath, lpath, **kwargs):
        """Copy single remote file to local"""
        if self.isdir(rpath):
            os.makedirs(lpath, exist_ok=True)
            return None

        callback = as_callback(kwargs.pop("callback", None))
        with self.open(rpath, "rb", **kwargs) as f1:
            callback.call("set_size", getattr(f1, "size", None))
            with open(lpath, "wb") as f2:
                data = True
                while data:
                    data = f1.read(self.blocksize)
                    segment_len = f2.write(data)
                    callback.call("relative_update", segment_len)

    def get(self, rpath, lpath, recursive=False, **kwargs):
        """Copy file(s) to local.

        Copies a specific file or tree of files (if recursive=True). If lpath
        ends with a "/", it will be assumed to be a directory, and target files
        will go within. Can submit a list of paths, which may be glob-patterns
        and will be expanded.

        Calls get_file for each source.
        """
        from .implementations.local import make_path_posix

        callback = as_callback(kwargs.pop("callback", None))
        if isinstance(lpath, str):
            lpath = make_path_posix(lpath)
        rpaths = self.expand_path(rpath, recursive=recursive)
        lpaths = other_paths(rpaths, lpath)

        callback.lazy_call("set_size", len, lpaths)
        for lpath, rpath in callback.wrap(zip(lpaths, rpaths)):
            branch(callback, rpath, lpath, kwargs)
            self.get_file(rpath, lpath, **kwargs)

    def put_file(self, lpath, rpath, **kwargs):
        """Copy single file to remote"""
        if os.path.isdir(lpath):
            self.makedirs(rpath, exist_ok=True)
            return None

        callback = as_callback(kwargs.pop("callback", None))
        with open(lpath, "rb") as f1:
            callback.call("set_size", f1.seek(0, 2))
            f1.seek(0)

            self.mkdirs(os.path.dirname(rpath), exist_ok=True)
            with self.open(rpath, "wb", **kwargs) as f2:
                data = True
                while data:
                    data = f1.read(self.blocksize)
                    segment_len = f2.write(data)
                    callback.call("relative_update", segment_len)

    def put(self, lpath, rpath, recursive=False, **kwargs):
        """Copy file(s) from local.

        Copies a specific file or tree of files (if recursive=True). If rpath
        ends with a "/", it will be assumed to be a directory, and target files
        will go within.

        Calls put_file for each source.
        """
        from .implementations.local import LocalFileSystem, make_path_posix

        callback = as_callback(kwargs.pop("callback", None))
        rpath = (
            self._strip_protocol(rpath)
            if isinstance(rpath, str)
            else [self._strip_protocol(p) for p in rpath]
        )
        if isinstance(lpath, str):
            lpath = make_path_posix(lpath)
        fs = LocalFileSystem()
        lpaths = fs.expand_path(lpath, recursive=recursive)
        rpaths = other_paths(lpaths, rpath)

        callback.lazy_call("set_size", len, rpaths)
        for lpath, rpath in callback.wrap(zip(lpaths, rpaths)):
            branch(callback, lpath, rpath, kwargs)
            self.put_file(lpath, rpath, **kwargs)

    def head(self, path, size=1024):
        """ Get the first ``size`` bytes from file """
        with self.open(path, "rb") as f:
            return f.read(size)

    def tail(self, path, size=1024):
        """ Get the last ``size`` bytes from file """
        with self.open(path, "rb") as f:
            f.seek(max(-size, -f.size), 2)
            return f.read()

    def cp_file(self, path1, path2, **kwargs):
        raise NotImplementedError

    def copy(self, path1, path2, recursive=False, on_error=None, **kwargs):
        """Copy within two locations in the filesystem

        on_error : "raise", "ignore"
            If raise, any not-found exceptions will be raised; if ignore any
            not-found exceptions will cause the path to be skipped; defaults to
            raise unless recursive is true, where the default is ignore
        """
        if on_error is None and recursive:
            on_error = "ignore"
        elif on_error is None:
            on_error = "raise"

        paths = self.expand_path(path1, recursive=recursive)
        path2 = other_paths(paths, path2)
        for p1, p2 in zip(paths, path2):
            try:
                self.cp_file(p1, p2, **kwargs)
            except FileNotFoundError:
                if on_error == "raise":
                    raise

    def expand_path(self, path, recursive=False, maxdepth=None):
        """Turn one or more globs or directories into a list of all matching paths
        to files or directories."""
        if isinstance(path, str):
            out = self.expand_path([path], recursive, maxdepth)
        else:
            # reduce depth on each recursion level unless None or 0
            maxdepth = maxdepth if not maxdepth else maxdepth - 1
            out = set()
            path = [self._strip_protocol(p) for p in path]
            for p in path:
                if has_magic(p):
                    bit = set(self.glob(p))
                    out |= bit
                    if recursive:
                        out |= set(
                            self.expand_path(
                                list(bit), recursive=recursive, maxdepth=maxdepth
                            )
                        )
                    continue
                elif recursive:
                    rec = set(self.find(p, maxdepth=maxdepth, withdirs=True))
                    out |= rec
                if p not in out and (recursive is False or self.exists(p)):
                    # should only check once, for the root
                    out.add(p)
        if not out:
            raise FileNotFoundError(path)
        return list(sorted(out))

    def mv(self, path1, path2, recursive=False, maxdepth=None, **kwargs):
        """ Move file(s) from one location to another """
        self.copy(path1, path2, recursive=recursive, maxdepth=maxdepth)
        self.rm(path1, recursive=recursive)

    def rm_file(self, path):
        """Delete a file"""
        self._rm(path)

    def _rm(self, path):
        """Delete one file"""
        # this is the old name for the method, prefer rm_file
        raise NotImplementedError

    def rm(self, path, recursive=False, maxdepth=None):
        """Delete files.

        Parameters
        ----------
        path: str or list of str
            File(s) to delete.
        recursive: bool
            If file(s) are directories, recursively delete contents and then
            also remove the directory
        maxdepth: int or None
            Depth to pass to walk for finding files to delete, if recursive.
            If None, there will be no limit and infinite recursion may be
            possible.
        """
        path = self.expand_path(path, recursive=recursive, maxdepth=maxdepth)
        for p in reversed(path):
            self.rm_file(p)

    @classmethod
    def _parent(cls, path):
        path = cls._strip_protocol(path.rstrip("/"))
        if "/" in path:
            parent = path.rsplit("/", 1)[0].lstrip(cls.root_marker)
            return cls.root_marker + parent
        else:
            return cls.root_marker

    def _open(
        self,
        path,
        mode="rb",
        block_size=None,
        autocommit=True,
        cache_options=None,
        **kwargs,
    ):
        """Return raw bytes-mode file-like from the file-system"""
        return AbstractBufferedFile(
            self,
            path,
            mode,
            block_size,
            autocommit,
            cache_options=cache_options,
            **kwargs,
        )

    def open(self, path, mode="rb", block_size=None, cache_options=None, **kwargs):
        """
        Return a file-like object from the filesystem

        The resultant instance must function correctly in a context ``with``
        block.

        Parameters
        ----------
        path: str
            Target file
        mode: str like 'rb', 'w'
            See builtin ``open()``
        block_size: int
            Some indication of buffering - this is a value in bytes
        cache_options : dict, optional
            Extra arguments to pass through to the cache.
        encoding, errors, newline: passed on to TextIOWrapper for text mode
        """
        import io

        path = self._strip_protocol(path)
        if "b" not in mode:
            mode = mode.replace("t", "") + "b"

            text_kwargs = {
                k: kwargs.pop(k)
                for k in ["encoding", "errors", "newline"]
                if k in kwargs
            }
            return io.TextIOWrapper(
                self.open(path, mode, block_size, **kwargs), **text_kwargs
            )
        else:
            ac = kwargs.pop("autocommit", not self._intrans)
            f = self._open(
                path,
                mode=mode,
                block_size=block_size,
                autocommit=ac,
                cache_options=cache_options,
                **kwargs,
            )
            if not ac and "r" not in mode:
                self.transaction.files.append(f)
            return f

    def touch(self, path, truncate=True, **kwargs):
        """Create empty file, or update timestamp

        Parameters
        ----------
        path: str
            file location
        truncate: bool
            If True, always set file size to 0; if False, update timestamp and
            leave file unchanged, if backend allows this
        """
        if truncate or not self.exists(path):
            with self.open(path, "wb", **kwargs):
                pass
        else:
            raise NotImplementedError  # update timestamp, if possible

    def ukey(self, path):
        """Hash of file properties, to tell if it has changed"""
        return sha256(str(self.info(path)).encode()).hexdigest()

    def read_block(self, fn, offset, length, delimiter=None):
        """Read a block of bytes from

        Starting at ``offset`` of the file, read ``length`` bytes.  If
        ``delimiter`` is set then we ensure that the read starts and stops at
        delimiter boundaries that follow the locations ``offset`` and ``offset
        + length``.  If ``offset`` is zero then we start at zero.  The
        bytestring returned WILL include the end delimiter string.

        If offset+length is beyond the eof, reads to eof.

        Parameters
        ----------
        fn: string
            Path to filename
        offset: int
            Byte offset to start read
        length: int
            Number of bytes to read
        delimiter: bytes (optional)
            Ensure reading starts and stops at delimiter bytestring

        Examples
        --------
        >>> fs.read_block('data/file.csv', 0, 13)  # doctest: +SKIP
        b'Alice, 100\\nBo'
        >>> fs.read_block('data/file.csv', 0, 13, delimiter=b'\\n')  # doctest: +SKIP
        b'Alice, 100\\nBob, 200\\n'

        Use ``length=None`` to read to the end of the file.
        >>> fs.read_block('data/file.csv', 0, None, delimiter=b'\\n')  # doctest: +SKIP
        b'Alice, 100\\nBob, 200\\nCharlie, 300'

        See Also
        --------
        utils.read_block
        """
        with self.open(fn, "rb") as f:
            size = f.size
            if length is None:
                length = size
            if size is not None and offset + length > size:
                length = size - offset
            return read_block(f, offset, length, delimiter)

    def to_json(self):
        """
        JSON representation of this filesystem instance

        Returns
        -------
        str: JSON structure with keys cls (the python location of this class),
            protocol (text name of this class's protocol, first one in case of
            multiple), args (positional args, usually empty), and all other
            kwargs as their own keys.
        """
        import json

        cls = type(self)
        cls = ".".join((cls.__module__, cls.__name__))
        proto = (
            self.protocol[0]
            if isinstance(self.protocol, (tuple, list))
            else self.protocol
        )
        return json.dumps(
            dict(
                **{"cls": cls, "protocol": proto, "args": self.storage_args},
                **self.storage_options,
            )
        )

    @staticmethod
    def from_json(blob):
        """
        Recreate a filesystem instance from JSON representation

        See ``.to_json()`` for the expected structure of the input

        Parameters
        ----------
        blob: str

        Returns
        -------
        file system instance, not necessarily of this particular class.
        """
        import json

        from .registry import _import_class, get_filesystem_class

        dic = json.loads(blob)
        protocol = dic.pop("protocol")
        try:
            cls = _import_class(dic.pop("cls"))
        except (ImportError, ValueError, RuntimeError, KeyError):
            cls = get_filesystem_class(protocol)
        return cls(*dic.pop("args", ()), **dic)

    def _get_pyarrow_filesystem(self):
        """
        Make a version of the FS instance which will be acceptable to pyarrow
        """
        # all instances already also derive from pyarrow
        return self

    def get_mapper(self, root, check=False, create=False):
        """Create key/value store based on this file-system

        Makes a MutibleMapping interface to the FS at the given root path.
        See ``fsspec.mapping.FSMap`` for further details.
        """
        from .mapping import FSMap

        return FSMap(root, self, check, create)

    @classmethod
    def clear_instance_cache(cls):
        """
        Clear the cache of filesystem instances.

        Notes
        -----
        Unless overridden by setting the ``cachable`` class attribute to False,
        the filesystem class stores a reference to newly created instances. This
        prevents Python's normal rules around garbage collection from working,
        since the instances refcount will not drop to zero until
        ``clear_instance_cache`` is called.
        """
        cls._cache.clear()

    def created(self, path):
        """Return the created timestamp of a file as a datetime.datetime"""
        raise NotImplementedError

    def modified(self, path):
        """Return the modified timestamp of a file as a datetime.datetime"""
        raise NotImplementedError

    # ------------------------------------------------------------------------
    # Aliases

    def makedir(self, path, create_parents=True, **kwargs):
        """Alias of :ref:`FilesystemSpec.mkdir`."""
        return self.mkdir(path, create_parents=create_parents, **kwargs)

    def mkdirs(self, path, exist_ok=False):
        """Alias of :ref:`FilesystemSpec.makedirs`."""
        return self.makedirs(path, exist_ok=exist_ok)

    def listdir(self, path, detail=True, **kwargs):
        """Alias of :ref:`FilesystemSpec.ls`."""
        return self.ls(path, detail=detail, **kwargs)

    def cp(self, path1, path2, **kwargs):
        """Alias of :ref:`FilesystemSpec.copy`."""
        return self.copy(path1, path2, **kwargs)

    def move(self, path1, path2, **kwargs):
        """Alias of :ref:`FilesystemSpec.mv`."""
        return self.mv(path1, path2, **kwargs)

    def stat(self, path, **kwargs):
        """Alias of :ref:`FilesystemSpec.info`."""
        return self.info(path, **kwargs)

    def disk_usage(self, path, total=True, maxdepth=None, **kwargs):
        """Alias of :ref:`FilesystemSpec.du`."""
        return self.du(path, total=total, maxdepth=maxdepth, **kwargs)

    def rename(self, path1, path2, **kwargs):
        """Alias of :ref:`FilesystemSpec.mv`."""
        return self.mv(path1, path2, **kwargs)

    def delete(self, path, recursive=False, maxdepth=None):
        """Alias of :ref:`FilesystemSpec.rm`."""
        return self.rm(path, recursive=recursive, maxdepth=maxdepth)

    def upload(self, lpath, rpath, recursive=False, **kwargs):
        """Alias of :ref:`FilesystemSpec.put`."""
        return self.put(lpath, rpath, recursive=recursive, **kwargs)

    def download(self, rpath, lpath, recursive=False, **kwargs):
        """Alias of :ref:`FilesystemSpec.get`."""
        return self.get(rpath, lpath, recursive=recursive, **kwargs)

    def sign(self, path, expiration=100, **kwargs):
        """Create a signed URL representing the given path

        Some implementations allow temporary URLs to be generated, as a
        way of delegating credentials.

        Parameters
        ----------
        path : str
             The path on the filesystem
        expiration : int
            Number of seconds to enable the URL for (if supported)

        Returns
        -------
        URL : str
            The signed URL

        Raises
        ------
        NotImplementedError : if method is not implemented for a fileystem
        """
        raise NotImplementedError("Sign is not implemented for this filesystem")

    def _isfilestore(self):
        # Originally inherited from pyarrow DaskFileSystem. Keeping this
        # here for backwards compatibility as long as pyarrow uses its
        # legacy ffspec-compatible filesystems and thus accepts fsspec
        # filesystems as well
        return False


class AbstractBufferedFile(io.IOBase):
    """Convenient class to derive from to provide buffering

    In the case that the backend does not provide a pythonic file-like object
    already, this class contains much of the logic to build one. The only
    methods that need to be overridden are ``_upload_chunk``,
    ``_initate_upload`` and ``_fetch_range``.
    """

    DEFAULT_BLOCK_SIZE = 5 * 2 ** 20

    def __init__(
        self,
        fs,
        path,
        mode="rb",
        block_size="default",
        autocommit=True,
        cache_type="readahead",
        cache_options=None,
        **kwargs,
    ):
        """
        Template for files with buffered reading and writing

        Parameters
        ----------
        fs: instance of FileSystem
        path: str
            location in file-system
        mode: str
            Normal file modes. Currently only 'wb', 'ab' or 'rb'. Some file
            systems may be read-only, and some may not support append.
        block_size: int
            Buffer size for reading or writing, 'default' for class default
        autocommit: bool
            Whether to write to final destination; may only impact what
            happens when file is being closed.
        cache_type: {"readahead", "none", "mmap", "bytes"}, default "readahead"
            Caching policy in read mode. See the definitions in ``core``.
        cache_options : dict
            Additional options passed to the constructor for the cache specified
            by `cache_type`.
        kwargs:
            Gets stored as self.kwargs
        """
        from .core import caches

        self.path = path
        self.fs = fs
        self.mode = mode
        self.blocksize = (
            self.DEFAULT_BLOCK_SIZE if block_size in ["default", None] else block_size
        )
        self.loc = 0
        self.autocommit = autocommit
        self.end = None
        self.start = None
        self.closed = False

        if cache_options is None:
            cache_options = {}

        if "trim" in kwargs:
            warnings.warn(
                "Passing 'trim' to control the cache behavior has been deprecated. "
                "Specify it within the 'cache_options' argument instead.",
                FutureWarning,
            )
            cache_options["trim"] = kwargs.pop("trim")

        self.kwargs = kwargs

        if mode not in {"ab", "rb", "wb"}:
            raise NotImplementedError("File mode not supported")
        if mode == "rb":
            if not hasattr(self, "details"):
                self.details = fs.info(path)
            self.size = self.details["size"]
            self.cache = caches[cache_type](
                self.blocksize, self._fetch_range, self.size, **cache_options
            )
        else:
            self.buffer = io.BytesIO()
            self.offset = None
            self.forced = False
            self.location = None

    @property
    def closed(self):
        # get around this attr being read-only in IOBase
        # use getattr here, since this can be called during del
        return getattr(self, "_closed", True)

    @closed.setter
    def closed(self, c):
        self._closed = c

    def __hash__(self):
        if "w" in self.mode:
            return id(self)
        else:
            return int(tokenize(self.details), 16)

    def __eq__(self, other):
        """Files are equal if they have the same checksum, only in read mode"""
        return self.mode == "rb" and other.mode == "rb" and hash(self) == hash(other)

    def commit(self):
        """Move from temp to final destination"""

    def discard(self):
        """Throw away temporary file"""

    def info(self):
        """ File information about this path """
        if "r" in self.mode:
            return self.details
        else:
            raise ValueError("Info not available while writing")

    def tell(self):
        """ Current file location """
        return self.loc

    def seek(self, loc, whence=0):
        """Set current file location

        Parameters
        ----------
        loc: int
            byte location
        whence: {0, 1, 2}
            from start of file, current location or end of file, resp.
        """
        loc = int(loc)
        if not self.mode == "rb":
            raise OSError(ESPIPE, "Seek only available in read mode")
        if whence == 0:
            nloc = loc
        elif whence == 1:
            nloc = self.loc + loc
        elif whence == 2:
            nloc = self.size + loc
        else:
            raise ValueError("invalid whence (%s, should be 0, 1 or 2)" % whence)
        if nloc < 0:
            raise ValueError("Seek before start of file")
        self.loc = nloc
        return self.loc

    def write(self, data):
        """
        Write data to buffer.

        Buffer only sent on flush() or if buffer is greater than
        or equal to blocksize.

        Parameters
        ----------
        data: bytes
            Set of bytes to be written.
        """
        if self.mode not in {"wb", "ab"}:
            raise ValueError("File not in write mode")
        if self.closed:
            raise ValueError("I/O operation on closed file.")
        if self.forced:
            raise ValueError("This file has been force-flushed, can only close")
        out = self.buffer.write(data)
        self.loc += out
        if self.buffer.tell() >= self.blocksize:
            self.flush()
        return out

    def flush(self, force=False):
        """
        Write buffered data to backend store.

        Writes the current buffer, if it is larger than the block-size, or if
        the file is being closed.

        Parameters
        ----------
        force: bool
            When closing, write the last block even if it is smaller than
            blocks are allowed to be. Disallows further writing to this file.
        """

        if self.closed:
            raise ValueError("Flush on closed file")
        if force and self.forced:
            raise ValueError("Force flush cannot be called more than once")
        if force:
            self.forced = True

        if self.mode not in {"wb", "ab"}:
            # no-op to flush on read-mode
            return

        if not force and self.buffer.tell() < self.blocksize:
            # Defer write on small block
            return

        if self.offset is None:
            # Initialize a multipart upload
            self.offset = 0
            try:
                self._initiate_upload()
            except:  # noqa: E722
                self.closed = True
                raise

        if self._upload_chunk(final=force) is not False:
            self.offset += self.buffer.seek(0, 2)
            self.buffer = io.BytesIO()

    def _upload_chunk(self, final=False):
        """Write one part of a multi-block file upload

        Parameters
        ==========
        final: bool
            This is the last block, so should complete file, if
            self.autocommit is True.
        """
        # may not yet have been initialized, may need to call _initialize_upload

    def _initiate_upload(self):
        """ Create remote file/upload """
        pass

    def _fetch_range(self, start, end):
        """Get the specified set of bytes from remote"""
        raise NotImplementedError

    def read(self, length=-1):
        """
        Return data from cache, or fetch pieces as necessary

        Parameters
        ----------
        length: int (-1)
            Number of bytes to read; if <0, all remaining bytes.
        """
        length = -1 if length is None else int(length)
        if self.mode != "rb":
            raise ValueError("File not in read mode")
        if length < 0:
            length = self.size - self.loc
        if self.closed:
            raise ValueError("I/O operation on closed file.")
        logger.debug("%s read: %i - %i" % (self, self.loc, self.loc + length))
        if length == 0:
            # don't even bother calling fetch
            return b""
        out = self.cache._fetch(self.loc, self.loc + length)
        self.loc += len(out)
        return out

    def readinto(self, b):
        """mirrors builtin file's readinto method

        https://docs.python.org/3/library/io.html#io.RawIOBase.readinto
        """
        out = memoryview(b).cast("B")
        data = self.read(out.nbytes)
        out[: len(data)] = data
        return len(data)

    def readuntil(self, char=b"\n", blocks=None):
        """Return data between current position and first occurrence of char

        char is included in the output, except if the end of the tile is
        encountered first.

        Parameters
        ----------
        char: bytes
            Thing to find
        blocks: None or int
            How much to read in each go. Defaults to file blocksize - which may
            mean a new read on every call.
        """
        out = []
        while True:
            start = self.tell()
            part = self.read(blocks or self.blocksize)
            if len(part) == 0:
                break
            found = part.find(char)
            if found > -1:
                out.append(part[: found + len(char)])
                self.seek(start + found + len(char))
                break
            out.append(part)
        return b"".join(out)

    def readline(self):
        """Read until first occurrence of newline character

        Note that, because of character encoding, this is not necessarily a
        true line ending.
        """
        return self.readuntil(b"\n")

    def __next__(self):
        out = self.readline()
        if out:
            return out
        raise StopIteration

    def __iter__(self):
        return self

    def readlines(self):
        """Return all data, split by the newline character"""
        data = self.read()
        lines = data.split(b"\n")
        out = [l + b"\n" for l in lines[:-1]]
        if data.endswith(b"\n"):
            return out
        else:
            return out + [lines[-1]]
        # return list(self)  ???

    def readinto1(self, b):
        return self.readinto(b)

    def close(self):
        """Close file

        Finalizes writes, discards cache
        """
        if getattr(self, "_unclosable", False):
            return
        if self.closed:
            return
        if self.mode == "rb":
            self.cache = None
        else:
            if not self.forced:
                self.flush(force=True)

            if self.fs is not None:
                self.fs.invalidate_cache(self.path)
                self.fs.invalidate_cache(self.fs._parent(self.path))

        self.closed = True

    def readable(self):
        """Whether opened for reading"""
        return self.mode == "rb" and not self.closed

    def seekable(self):
        """Whether is seekable (only in read mode)"""
        return self.readable()

    def writable(self):
        """Whether opened for writing"""
        return self.mode in {"wb", "ab"} and not self.closed

    def __del__(self):
        if not self.closed:
            self.close()

    def __str__(self):
        return "<File-like object %s, %s>" % (type(self.fs).__name__, self.path)

    __repr__ = __str__

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
