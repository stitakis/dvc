import os
import yaml
import itertools
import subprocess
import schema

import dvc.dependency as dependency
import dvc.output as output
from dvc.exceptions import DvcException
from dvc.logger import Logger
from dvc.utils import dict_md5


class StageCmdFailedError(DvcException):
    def __init__(self, stage):
        msg = u'Stage \'{}\' cmd {} failed'.format(stage.relpath, stage.cmd)
        super(StageCmdFailedError, self).__init__(msg)


class StageFileFormatError(DvcException):
    def __init__(self):
        super(StageFileFormatError, self).__init__('Stage file format error')


class MissingDataSource(DvcException):
    def __init__(self, missing_files):
        assert len(missing_files) > 0

        source = 'source'
        if len(missing_files) > 1:
            source += 's'

        msg = u'missing data {}: {}'.format(source, ', '.join(missing_files))
        super(MissingDataSource, self).__init__(msg)


class Stage(object):
    STAGE_FILE = 'Dvcfile'
    STAGE_FILE_SUFFIX = '.dvc'

    PARAM_MD5 = 'md5'
    PARAM_CMD = 'cmd'
    PARAM_DEPS = 'deps'
    PARAM_OUTS = 'outs'

    SCHEMA = {
        schema.Optional(PARAM_MD5): schema.Or(str, None),
        schema.Optional(PARAM_CMD): schema.Or(str, None),
        schema.Optional(PARAM_DEPS): schema.Or(schema.And(list, schema.Schema([dependency.SCHEMA])), None),
        schema.Optional(PARAM_OUTS): schema.Or(schema.And(list, schema.Schema([output.SCHEMA])), None),
    }

    def __init__(self, project, path=None, cmd=None, cwd=None, deps=[], outs=[], md5=None):
        self.project = project
        self.path = path
        self.cmd = cmd
        self.cwd = cwd
        self.outs = outs
        self.deps = deps
        self.md5 = md5

    @property
    def relpath(self):
        return os.path.relpath(self.path)

    @property
    def is_data_source(self):
        return self.cmd is None

    @staticmethod
    def is_stage_file(path):
        if not os.path.isfile(path):
            return False

        if not path.endswith(Stage.STAGE_FILE_SUFFIX) and os.path.basename(path) != Stage.STAGE_FILE:
            return False

        return True

    def changed_md5(self):
        md5 = self.dumpd().get(self.PARAM_MD5, None)

        # backward compatibility
        if self.md5 == None:
            return False

        if self.md5 and md5 and self.md5 == md5:
            return False

        msg = "Dvc file '{}' md5 changed(expected '{}', actual '{}')"
        self.project.logger.debug(msg.format(self.relpath, self.md5, md5))
        return True

    @property
    def is_callback(self):
        return not self.is_data_source and len(self.deps) == 0

    def changed(self):
        ret = False

        if self.is_callback:
            ret = True

        for entry in itertools.chain(self.outs, self.deps):
            if entry.changed():
                ret = True

        if self.changed_md5():
            ret = True

        if ret:
            self.project.logger.debug(u'Dvc file \'{}\' changed'.format(self.relpath))
        else:
            self.project.logger.debug(u'Dvc file \'{}\' didn\'t change'.format(self.relpath))

        return ret

    def remove_outs(self, ignore_remove=False):
        for out in self.outs:
            out.remove(ignore_remove=ignore_remove)

    def remove(self):
        self.remove_outs(ignore_remove=True)
        os.unlink(self.path)

    def reproduce(self, force=False):
        if not self.changed() and not force:
            return None

        if self.cmd:
            # Removing outputs only if we actually have command to reproduce
            self.remove_outs(ignore_remove=False)

        self.run()

        return self

    @staticmethod
    def validate(d):
        try:
            schema.Schema(Stage.SCHEMA).validate(d)
        except schema.SchemaError as exc:
            Logger.debug(str(exc))
            raise StageFileFormatError()

    @staticmethod
    def loadd(project, d, path):
        Stage.validate(d)

        path = os.path.abspath(path)
        cwd = os.path.dirname(path)
        cmd = d.get(Stage.PARAM_CMD, None)
        md5 = d.get(Stage.PARAM_MD5, None)

        stage = Stage(project=project,
                      path=path,
                      cmd=cmd,
                      cwd=cwd,
                      md5=md5)

        stage.deps = dependency.loadd_from(stage, d.get(Stage.PARAM_DEPS, []))
        stage.outs = output.loadd_from(stage, d.get(Stage.PARAM_OUTS, []))

        return stage

    @staticmethod
    def loads(project=None,
              cmd=None,
              deps=[],
              outs=[],
              outs_no_cache=[],
              fname=None,
              cwd=os.curdir):
        cwd = os.path.abspath(cwd)
        path = os.path.join(cwd, fname)

        stage = Stage(project=project,
                      path=path,
                      cmd=cmd,
                      cwd=cwd)

        stage.outs = output.loads_from(stage, outs, use_cache=True)
        stage.outs += output.loads_from(stage, outs_no_cache, use_cache=False)
        stage.deps = dependency.loads_from(stage, deps)

        return stage

    @staticmethod
    def load(project, fname):
        with open(fname, 'r') as fd:
            return Stage.loadd(project, yaml.safe_load(fd), fname)

    def dumpd(self):
        deps = [x.dumpd() for x in self.deps]
        outs = [x.dumpd() for x in self.outs]

        ret = {}
        if self.cmd != None:
            ret[Stage.PARAM_CMD] = self.cmd

        if len(deps):
            ret[Stage.PARAM_DEPS] = deps

        if len(outs):
            ret[Stage.PARAM_OUTS] = outs

        ret[Stage.PARAM_MD5] = dict_md5(ret)

        return ret

    def dump(self, fname=None):
        if not fname:
            fname = self.path

        with open(fname, 'w') as fd:
            yaml.safe_dump(self.dumpd(), fd, default_flow_style=False)

    def save(self):
        for dep in self.deps:
            dep.save()

        for out in self.outs:
            out.save()

    def run(self):
        if not self.is_data_source:
            self.project.logger.info(u'Reproducing \'{}\':\n\t{}'.format(self.relpath, self.cmd))

            p = subprocess.Popen(self.cmd,
                                 cwd=self.cwd,
                                 shell=True,
                                 env=os.environ,
                                 executable=os.getenv('SHELL'))
            p.communicate()
            if p.returncode != 0:
               raise StageCmdFailedError(self)
 
            self.save()

            self.project.logger.debug(u'\'{}\' was reproduced'.format(self.relpath))
        else:
            self.project.logger.info(u'Verifying data sources in \'{}\''.format(self.relpath))
            self.check_missing_outputs()
            self.save()

    def check_missing_outputs(self):
        missing_outs = [out.rel_path for out in self.outs if not os.path.exists(out.rel_path)]
        if missing_outs:
            raise MissingDataSource(missing_outs)

    def checkout(self):
        for out in self.outs:
            out.checkout()

    def _status(self, entries, name):
        ret = {}

        for entry in entries:
            ret.update(entry.status())

        if ret:
            return {name: ret}

        return {}

    def status(self):
        ret = {}
        ret.update(self._status(self.deps, 'deps'))
        ret.update(self._status(self.outs, 'outs'))

        if ret or self.changed_md5() or self.is_callback:
            return {self.relpath: ret}

        return {}
