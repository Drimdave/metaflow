import io
import glob
import os
import random
import re
import string
import tempfile

from shutil import move

import click

from metaflow.exception import MetaflowException
from metaflow.parameters import Parameter


class InternalFile(object):
    def __init__(self, logger, is_text, encoding, path):
        self._logger = logger
        self._is_text = is_text
        self._encoding = encoding
        self._path = path
        self._size = 0

    @classmethod
    def is_file_handled(cls, path):
        if path.startswith("s3://"):
            return True, None
        try:
            with open(path, mode='r') as _:
                pass
        except OSError:
            return False, "Could not open file '%s'" % path
        else:
            return True, None

    def handle_s3_file(self):
        # We bring the file back in locally so we can then properly
        # include it like a local file; we currently do not support
        # S3 -> S3 directly.
        from metaflow import S3

        to_return = tempfile.NamedTemporaryFile(dir='/tmp', delete=False)
        to_return.close()
        self._logger('Fetching %s from S3 to temporary file %s' % (self._path, to_return.name))
        with S3() as s3:
            res = s3.get(self._path)
            move(res.path, to_return.name)
        return to_return.name

    def handle_local_file(self, override_path=None):
        if override_path:
            path = override_path
        else:
            path = self._path
        sz = self._size = os.path.getsize(path)
        unit = ['B', 'KB', 'MB', 'GB', 'TB']
        pos = 0
        while pos < len(unit) and sz >= 1024:
            sz = sz // 1024
            pos += 1
        if pos >= 3:
            extra = '(this may take a while)'
        else:
            extra = ''
        self._logger(
            'Including file %s of size %d%s %s' % (self._path, sz, unit[pos], extra))
        if self._is_text:
            return io.open(path, mode='rt', encoding=self._encoding).read()
        try:
            return io.open(path, mode='rb').read()
        except IOError:
            # If we get an error here, since we know that the file exists already,
            # it means that read failed which happens with Python 2.7 for large files
            raise MetaflowException('Cannot read file at %s -- this is likely because it is too '
                                    'large to be properly handled by Python 2.7' % self._path)

    def __call__(self):
        local_file = None
        if self._path.startswith("s3://"):
            local_file = self.handle_s3_file()
        try:
            return self.handle_local_file(local_file)
        finally:
            if local_file:
                os.unlink(local_file)

    def name(self):
        return self._path

    def size(self):
        return self._size


class MultipleFiles(object):
    def __init__(self, base_name, logger, is_text, encoding):
        self._base_name = base_name
        self._logger = logger
        self._is_text = is_text
        self._encoding = encoding
        self._files = {}

    def add_file(self, path):
        name = os.path.basename(path)
        # Sanitize the name to make it possible to use as a variable name
        name = re.sub('[^a-zA-Z0-9_]', '_', name)
        name = "%s_%s" % (self._base_name, name)
        ending = ''
        while name + ending in self._files:
            ending = ''.join([random.choice(string.digits) for _ in range(2)])
        name = name + ending
        self._files[name] = path

    def get_reference_dict(self):
        result = {name: info for name, info in self._files.items()}
        return result

    def __iter__(self):
        for name, file in self._files.items():
            f = InternalFile(self._logger, self._is_text, self._encoding, file)
            yield name, f(), f.size()


class FilePathClass(click.ParamType):
    name = 'FilePath'

    def __init__(self, is_text, encoding):
        self._is_text = is_text
        self._encoding = encoding

    def convert(self, value, param, ctx):
        value = os.path.expanduser(value)
        ok, err = InternalFile.is_file_handled(value)
        if not ok:
            self.fail(err)

        return InternalFile(ctx.obj.logger, self._is_text, self._encoding, value)

    def __str__(self):
        return repr(self)

    def __repr__(self):
        return 'FilePath'


class FileGlobClass(click.ParamType):
    name = 'FileGlob'

    def __init__(self, name, is_text, encoding, recursive):
        self._name = name
        self._is_text = is_text
        self._encoding = encoding
        self._recursive = recursive

    def convert(self, value, param, ctx):
        result = MultipleFiles(self._name, ctx.obj.logger, self._is_text, self._encoding)
        value = os.path.expanduser(value)
        for path in glob.glob(value, recursive=self._recursive):
            try:
                with open(path, mode='r') as _:
                    pass
            except OSError:
                pass  # Skip files that we can't open
            else:
                result.add_file(path)

        return result

    def __str__(self):
        return repr(self)

    def __repr__(self):
        return 'FileGlob'


class IncludeFile(Parameter):

    def __init__(
            self, name, required=False, is_text=True, encoding=None, help=None, default=None):
        super(IncludeFile, self).__init__(
            name, required=required, help=help, default=default,
            type=FilePathClass(is_text, encoding))


class IncludeMultipleFiles(Parameter):

    def __init__(
            self, name, required=False, is_text=True, encoding=None,
            recursive=False, help=None, default=None):
        super(IncludeMultipleFiles, self).__init__(
            name, required=required, help=help, default=default,
            type=FileGlobClass(name, is_text, encoding, recursive))
