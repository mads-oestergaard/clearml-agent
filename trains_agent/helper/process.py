from __future__ import unicode_literals, print_function

import abc
import logging
import os
import re
import subprocess
import sys
from contextlib import contextmanager
from copy import deepcopy
from distutils.spawn import find_executable
from itertools import chain, repeat, islice
from os.path import devnull
from typing import Union, Text, Sequence, Any, TypeVar, Callable

import psutil
from furl import furl
from future.builtins import super
from pathlib2 import Path

import six
from trains_agent.definitions import PROGRAM_NAME, CONFIG_FILE
from trains_agent.helper.base import bash_c, is_windows_platform, select_for_platform, chain_map

PathLike = Union[Text, Path]


def get_bash_output(cmd, strip=False, stderr=subprocess.STDOUT, stdin=False):
    try:
        output = (
            subprocess.check_output(
                bash_c().split() + [cmd],
                stderr=stderr,
                stdin=subprocess.PIPE if stdin else None,
            )
            .decode()
            .strip()
        )
    except subprocess.CalledProcessError:
        output = None
    return output if not strip or not output else output.strip()


def kill_all_child_processes(pid=None):
    # get current process if pid not provided
    include_parent = True
    if not pid:
        pid = os.getpid()
        include_parent = False
    print("\nLeaving process id {}".format(pid))
    try:
        parent = psutil.Process(pid)
    except psutil.Error:
        # could not find parent process id
        return
    for child in parent.children(recursive=True):
        child.kill()
    if include_parent:
        parent.kill()


def shutdown_docker_process(docker_cmd_ending):
    try:
        containers_running = get_bash_output(cmd='docker ps --no-trunc --format \"{{.ID}}: {{.Command}}\"')
        for docker_line in containers_running.split('\n'):
            parts = docker_line.split(':')
            if parts[-1].endswith(docker_cmd_ending):
                # we found our docker, stop it
                get_bash_output(cmd='docker stop -t 1 {}'.format(parts[0]))
                return
    except Exception:
        pass


def check_if_command_exists(cmd):
    return bool(find_executable(cmd))


def get_program_invocation():
    return [sys.executable, "-u", "-m", PROGRAM_NAME.replace('-', '_')]


Retval = TypeVar("Retval")


@six.add_metaclass(abc.ABCMeta)
class Executable(object):
    @abc.abstractmethod
    def call_subprocess(self, func, censor_password=False, *args, **kwargs):
        # type: (Callable[..., Retval]) -> Retval
        pass

    def get_output(self, *args, **kwargs):
        return (
            self.call_subprocess(subprocess.check_output, *args, **kwargs)
            .decode("utf8")
            .rstrip()
        )

    def check_call(self, *args, **kwargs):
        return self.call_subprocess(subprocess.check_call, *args, **kwargs)

    @staticmethod
    @contextmanager
    def normalize_exception(censor_password=False):
        try:
            yield
        except subprocess.CalledProcessError as e:
            if censor_password:
                e.cmd = [furl(word).remove(password=True).tostr() for word in e.cmd]

            if e.output and not isinstance(e.output, six.text_type):
                e.output = e.output.decode()
            raise

    @abc.abstractmethod
    def pretty(self):
        pass


class Argv(Executable):

    ARGV_SEPARATOR = " "

    def __init__(self, *argv, **kwargs):
        # type: (*PathLike, Any) -> ()
        """
        Object representing a series of strings used to invoke a process.
        """
        self.argv = argv
        self._log = kwargs.pop("log", None)
        if not self._log:
            self._log = logging.getLogger(__name__)
            self._log.propagate = False

    def serialize(self):
        """
        Returns a string of the shell command
        """
        return self.ARGV_SEPARATOR.join(map(quote, self))

    def call_subprocess(self, func, censor_password=False, *args, **kwargs):
        self._log.debug("running: %s: %s", func.__name__, list(self))
        with self.normalize_exception(censor_password):
            return func(list(self), *args, **kwargs)

    def call(self, *args, **kwargs):
        return self.call_subprocess(subprocess.call, *args, **kwargs)

    def get_argv(self):
        return self.argv

    def __repr__(self):
        return "<Argv{}>".format(self.argv)

    def __str__(self):
        return "Executing: {}".format(self.argv)

    def __iter__(self):
        return (six.text_type(word) for word in self.argv)

    def __getitem__(self, item):
        return self.argv[item]

    def __add__(self, other):
        try:
            iter(other)
        except TypeError:
            return NotImplemented
        return type(self)(*(self.argv + tuple(other)), log=self._log)

    def __radd__(self, other):
        try:
            iter(other)
        except TypeError:
            return NotImplemented
        return type(self)(*(tuple(other) + self.argv), log=self._log)

    pretty = serialize

    @staticmethod
    def conditional_flag(condition, flag, *flags):
        # type: (Any, PathLike, PathLike) -> Sequence[PathLike]
        """
        Translate a boolean to a flag command like arguments.
        :param condition: condition to translate to flag
        :param flag: flag to use if condition true (at least one)
        :param flags: additional flags to use if condition is true
        """
        return (flag,) + flags if condition else ()


class CommandSequence(Executable):

    JOIN_COMMAND_OPERATOR = "&&"

    def __init__(self, *commands, **kwargs):
        """
        Object representing a sequence of shell commands.
        :param commands: Command elements. Each CommandSequence will be treated as a single command-line argument.
        :type commands: Each command: [str] | Argv
        """
        self._log = kwargs.pop("log", None)
        if not self._log:
            self._log = logging.getLogger(__name__)
            self._log.propagate = False
        self.commands = []
        for c in commands:
            if isinstance(c, CommandSequence):
                self.commands.extend(deepcopy(c.commands))
            elif isinstance(c, Argv):
                self.commands.append(deepcopy(c))
            else:
                self.commands.append(Argv(*c, log=self._log))

    def get_argv(self, shell=False):
        """
        Get array of argv's.
        :param bool shell: if True, returns the argv of a process that will invoke a shell running the command sequence
        """
        if shell:
            return tuple(bash_c().split()) + (self.serialize(),)

        def safe_get_argv(obj):
            try:
                func = obj.get_argv
            except AttributeError:
                result = obj
            else:
                result = func()
            return tuple(map(str, result))

        return tuple(map(safe_get_argv, self.commands))

    def serialize(self):
        def intersperse(delimiter, seq):
            return islice(chain.from_iterable(zip(repeat(delimiter), seq)), 1, None)

        def normalize(command):
            return list(command) if is_windows_platform() else command.serialize()

        return ' '.join(list(intersperse(self.JOIN_COMMAND_OPERATOR, map(normalize, self.commands))))

    def call_subprocess(self, func, censor_password=False, *args, **kwargs):
        with self.normalize_exception(censor_password):
            return func(
                self.serialize(),
                *args,
                **chain_map(
                    dict(
                        executable=select_for_platform(linux="bash", windows=None),
                        shell=True,
                    ),
                    kwargs,
                )
            )

    def __repr__(self):
        tab = " " * 4
        return "<{}(\n{}{},\n)>".format(
            type(self).__name__, tab, (",\n" + tab).join(map(repr, self.commands))
        )

    def __iter__(self):
        return iter(self.commands)

    def __getitem__(self, item):
        return self.commands[item]

    def __setitem__(self, key, value):
        self.commands[key] = value

    def __add__(self, other):
        try:
            iter(other)
        except TypeError:
            return NotImplemented
        return type(self)(*(self.commands + tuple(other)))

    def pretty(self):
        serialized = self.serialize()
        if is_windows_platform():
            return " ".join(serialized)
        return serialized


class WorkerParams(object):
    def __init__(
        self,
        log_level="INFO",
        config_file=CONFIG_FILE,
        optimization=0,
        debug=False,
        trace=False,
    ):
        self.trace = trace
        self.log_level = log_level
        self.optimization = optimization
        self.config_file = config_file
        self.debug = debug

    def get_worker_flags(self):
        """
        Serialize a WorkerParams instance to a tuple of command-line flags
        :param WorkerParams self: parameters of worker
        :return: a tuple of global flags and "workers execute/daemon" flags
        """
        global_args = ("--config-file", str(self.config_file))
        if self.debug:
            global_args += ("--debug",)
        worker_args = tuple()
        if self.optimization:
            worker_args += self.get_optimization_flag()
        return global_args, worker_args

    def get_optimization_flag(self):
        return "-{}".format("O" * self.optimization)

    def get_argv_for_command(self, command):
        """
        Get argv for a particular worker command.
        """
        global_args, worker_args = self.get_worker_flags()
        command_line = (
            tuple(get_program_invocation())
            + global_args
            + (command, )
            + worker_args
        )
        return Argv(*command_line)


class DaemonParams(WorkerParams):
    def __init__(self, foreground=False, queues=(), *args, **kwargs):
        super(DaemonParams, self).__init__(*args, **kwargs)
        self.foreground = foreground
        self.queues = tuple(queues)

    def get_worker_flags(self):
        global_args, worker_args = super(DaemonParams, self).get_worker_flags()
        if self.foreground:
            worker_args += ("--foreground",)
        if self.queues:
            worker_args += ("--queue",) + self.queues
        return global_args, worker_args


DEVNULL = open(devnull, "w+")
SOURCE_COMMAND = select_for_platform(linux="source", windows="call")


class ExitStatus(object):
    success = 0
    failure = 1
    interrupted = 2


COMMAND_SUCCESS = 0


_find_unsafe = re.compile(r"[^\w@%+=:,./-]", getattr(re, "ASCII", 0)).search


def quote(s):
    """
    Backport of shlex.quote():
    Return a shell-escaped version of the string *s*.
    """
    if not s:
        return "''"
    if _find_unsafe(s) is None:
        return s

    # use single quotes, and put single quotes into double quotes
    # the string $'b is then quoted as '$'"'"'b'
    return "'" + s.replace("'", "'\"'\"'") + "'"