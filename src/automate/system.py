# -*- coding: utf-8 -*-
# (c) 2015 Tuomas Airaksinen
#
# This file is part of Automate.
#
# Automate is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Automate is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Automate.  If not, see <http://www.gnu.org/licenses/>.
#
# ------------------------------------------------------------------
#
# If you like Automate, please take a look at this page:
# http://evankelista.net/automate/

from collections import defaultdict

from raven.handlers.logging import SentryHandler

import threading
import operator
import os
import logging
import pickle
import pkg_resources
import argparse

import raven

from traits.api import (CStr, Instance, CBool, CList, Property, CInt, CUnicode, Event, CSet, Str, cached_property,
                        on_trait_change)

from .common import (SystemBase, ExitException, has_baseclass, Object)
from .namespace import Namespace
from .service import AbstractService, AbstractUserService, AbstractSystemService
from .statusobject import AbstractSensor, AbstractActuator
from .systemobject import SystemObject
from .worker import StatusWorkerThread
from .callable import AbstractCallable
from . import __version__

import typing

if typing.TYPE_CHECKING:
    from typing import Dict, List, Any

STATEFILE_VERSION = 1

import sys

if sys.version_info >= (3, 0):
    TimerClass = threading.Timer
else:
    TimerClass = threading._Timer

def get_autoload_services():
    import automate.services
    return (i for i in list(automate.services.__dict__.values()) if has_baseclass(i, AbstractService) and i.autoload)


def get_service_by_name(name):
    import automate.services
    return getattr(automate.services, name)


class System(SystemBase):
    #: Name of the system (shown in WEB UI for example)
    name = CStr

    #: Allow referencing objects by their names in Callables. If disabled, you can still refer to objects by names
    #: by Object('name')
    allow_name_referencing = CBool(True)

    #: Filename to where to dump the system state
    filename = Str

    # LOGGING
    ###########

    #: Name of the file where logs are stored
    logfile = CUnicode

    #: Reference to logger instance (read-only)
    logger = Instance(logging.Logger)

    #: Sentry: Raven DSN configuration (see http://sentry.io)
    raven_dsn = Str

    #: Raven client (is created automatically if raven_dsn is set and this is left empty)
    raven_client = Instance(raven.Client, transient=True)

    #: Format string of the log handler that writes to stdout
    log_format = Str('%(asctime)s %(log_color)s%(name)s%(reset)s %(message)s')

    #: Format string of the log handler that writes to logfile
    logfile_format = Str('%(process)d:%(threadName)s:%(name)s:%(asctime)s:%(levelname)s:%(message)s')

    #: Log level of System logger
    log_level = CInt(logging.INFO, transient=True)

    @on_trait_change('log_level', post_init=True)
    def log_level_changed(self, new):
        self.logger.setLevel(new)

    # SERVICES
    ###########

    #: Add here services that you want to be added automatically. This is meant to be re-defined in subclass.
    default_services = CList(trait=Str)

    #: List of services that are loaded in the initialization of the System.
    services = CList(trait=Instance(AbstractService))

    #: List of servicenames that are desired to be avoided (even if normally autoloaded).
    exclude_services = CSet(trait=Str)

    #: Reference to the worker thread (read-only)
    worker_thread = Instance(StatusWorkerThread, transient=True)

    #: System namespace (read-only)
    namespace = Instance(Namespace)

    # Set of all SystemObjects within the system. This is where SystemObjects are ultimately stored
    # in the System initialization. (read-only)
    objects = CSet(trait=SystemObject)

    #: Property giving objects sorted alphabetically (read-only)
    objects_sorted = Property(depends_on='objects')

    @cached_property
    def _get_objects_sorted(self):
        return sorted(list(self.objects), key=operator.attrgetter('_order'))

    #: Read-only property giving all sensors of the system
    sensors = Property(depends_on='objects[]')

    @cached_property
    def _get_sensors(self):
        return {i for i in self.objects_sorted if isinstance(i, AbstractSensor)}

    #: Read-only property giving all actuator of the system
    actuators = Property(depends_on='objects[]')

    @cached_property
    def _get_actuators(self):
        return {i for i in self.objects_sorted if isinstance(i, AbstractActuator)}

    #: Read-only property giving all objects that have program features in use
    programs = Property(depends_on='objects[]')

    @cached_property
    def _get_programs(self):
        from .program import Program, DefaultProgram
        return {i for i in self.objects_sorted if isinstance(i, (Program, DefaultProgram))}

    #: Read-only property giving all :class:`~program.Program` objects
    ordinary_programs = Property(depends_on='programs[]')

    @cached_property
    def _get_ordinary_programs(self):
        from . import program
        return {i for i in self.programs if isinstance(i, program.Program)}

    #: Start worker thread automatically after system is initialized
    worker_autostart = CBool(True)

    #: Trigger which is triggered after initialization is ready (used by Services)
    post_init_trigger = Event

    #: Trigger which is triggered before quiting (used by Services)
    pre_exit_trigger = Event

    #: Read-only property that gives list of all object tags
    all_tags = Property(depends_on='objects.tags[]')

    #: Number of state backup files
    num_state_backups = CInt(5)

    @cached_property
    def _get_all_tags(self):
        newset = set([])
        for i in self.system.objects:
            for j in i.tags:
                if j:
                    newset.add(j)
        return newset

    #: Enable experimental two-phase queue handling technique (not recommended)
    two_phase_queue = CBool(False)

    @classmethod
    def load_or_create(cls, filename=None, no_input=False, create_new=False, **kwargs):
        """
            Load system from a dump, if dump file exists, or create a new system if it does not exist.
        """
        parser = argparse.ArgumentParser()
        parser.add_argument('--no_input', action='store_true')
        parser.add_argument('--create_new', action='store_true')
        args = parser.parse_args()

        if args.no_input:
            print('Parameter --no_input was given')
            no_input = True
        if args.create_new:
            print('Parameter --create_new was given')
            create_new = True
            no_input = True

        def savefile_more_recent():
            time_savefile = os.path.getmtime(filename)
            time_program = os.path.getmtime(sys.argv[0])
            return time_savefile > time_program

        def load_pickle():
            with open(filename, 'rb') as of:
                statefile_version, data = pickle.load(of)

            if statefile_version != STATEFILE_VERSION:
                raise RuntimeError(f'Wrong statefile version, please remove state file {filename}')
            return data

        def load():
            print('Loading %s' % filename)
            obj_list, config = load_pickle()
            system = System(load_state=obj_list, filename=filename, **kwargs)

            return system

        def create():
            print('Creating new system')
            config = None
            if filename:
                try:
                    obj_list, config = load_pickle()
                except FileNotFoundError:
                    config = None
            return cls(filename=filename, load_config=config, **kwargs)

        if filename and os.path.isfile(filename):
            if savefile_more_recent() and not create_new:
                return load()
            else:
                if no_input:
                    print('Program file more recent. Loading that instead.')
                    return create()
                while True:
                    answer = input('Program file more recent. Do you want to load it? (y/n) ')
                    if answer == 'y':
                        return create()
                    elif answer == 'n':
                        return load()
        else:
            return create()

    def save_state(self):
        """
            Save state of the system to a dump file :attr:`System.filename`
        """
        if not self.filename:
            self.logger.error('Filename not specified. Could not save state')
            return
        self.logger.debug('Saving system state to %s', self.filename)
        for i in reversed(range(self.num_state_backups)):
            fname = self.filename if i == 0 else '%s.%d' % (self.filename, i)
            new_fname = '%s.%d' % (self.filename, i+1)
            try:
                os.rename(fname, new_fname)
            except FileNotFoundError:
                pass

        with open(self.filename, 'wb') as file, self.worker_thread.queue.mutex:
            obj_list = list(self.objects)
            config = {obj.name: obj.status for obj in obj_list
                      if getattr(obj, 'user_editable', False)}
            data = obj_list, config
            pickle.dump((STATEFILE_VERSION, data), file, pickle.HIGHEST_PROTOCOL)

    @property
    def cmd_namespace(self):
        """
            A read-only property that gives the namespace of the system for evaluating commands.
        """
        import automate
        ns = dict(list(automate.__dict__.items()) + list(self.namespace.items()))
        return ns

    def __getattr__(self, item):
        if self.namespace and item in self.namespace:
            return self.namespace[item]
        raise AttributeError

    def get_unique_name(self, obj, name='', name_from_system=''):
        """
            Give unique name for an Sensor/Program/Actuator object
        """
        ns = self.namespace
        newname = name
        if not newname:
            newname = name_from_system

        if not newname:
            newname = u"Nameless_" + obj.__class__.__name__

        if not newname in ns:
            return newname

        counter = 0
        while True:
            newname1 = u"%s_%.2d" % (newname, counter)
            if not newname1 in ns:
                return newname1
            counter += 1

    @property
    def services_by_name(self):
        """
            A property that gives a dictionary that contains services as values and their names as keys.
        """
        srvs = defaultdict(list)
        for i in self.services:
            srvs[i.__class__.__name__].append(i)
        return srvs

    @property
    def service_names(self):
        """
            A property that gives the names of services as a list
        """
        return set(self.services_by_name.keys())

    def flush(self):
        """
            Flush the worker queue. Usefull in unit tests.
        """
        self.worker_thread.flush()

    def name_to_system_object(self, name):
        """
            Give SystemObject instance corresponding to the name
        """
        if isinstance(name, str):
            if self.allow_name_referencing:
                name = name
            else:
                raise NameError('System.allow_name_referencing is set to False, cannot convert string to name')
        elif isinstance(name, Object):
            name = str(name)
        return self.namespace.get(name, None)

    def eval_in_system_namespace(self, exec_str):
        """
            Get Callable for specified string (for GUI-based editing)
        """
        ns = self.cmd_namespace
        try:
            return eval(exec_str, ns)
        except Exception as e:
            self.logger.warning('Could not execute %s, gave error %s', exec_str, e)
            return None

    def register_service_functions(self, *funcs):
        """
            Register function in the system namespace. Called by Services.
        """
        for func in funcs:
            self.namespace[func.__name__] = func

    def register_service(self, service):
        """
            Register service into the system. Called by Services.
        """
        if service not in self.services:
            self.services.append(service)

    def request_service(self, type, id=0):
        """
            Used by Sensors/Actuators/other services that need to use other services for their
            operations.
        """
        srvs = self.services_by_name.get(type)
        if not srvs:
            return

        ser = srvs[id]

        if not ser.system:
            ser.setup_system(self, id=id)
        return ser

    def cleanup(self):
        """
            Clean up before quitting
        """

        self.pre_exit_trigger = True

        self.logger.info("Shutting down %s, please wait a moment.", self.name)
        for t in threading.enumerate():
            if isinstance(t, TimerClass):
                t.cancel()
        self.logger.debug('Timers cancelled')

        for i in self.objects:
            i.cleanup()

        self.logger.debug('Sensors etc cleanups done')

        for ser in (i for i in self.services if isinstance(i, AbstractUserService)):
            ser.cleanup_system()
        self.logger.debug('User services cleaned up')
        if self.worker_thread.is_alive():
            self.worker_thread.stop()
        self.logger.debug('Worker thread really stopped')

        for ser in (i for i in self.services if isinstance(i, AbstractSystemService)):
            ser.cleanup_system()
        self.logger.debug('System services cleaned up')
        threads = list(t.name for t in threading.enumerate() if t.is_alive() and not t.daemon)
        if threads:
            self.logger.info('After cleanup, we have still the following threads '
                             'running: %s', ', '.join(threads))

    def cmd_exec(self, cmd):
        """
            Execute commands in automate namespace
        """

        if not cmd:
            return
        ns = self.cmd_namespace
        import copy
        rval = True
        nscopy = copy.copy(ns)
        try:
            r = eval(cmd, ns)
            if isinstance(r, SystemObject) and not r.system:
                r.setup_system(self)
            if callable(r):
                r = r()
                cmd += "()"
            self.logger.info("Eval: %s", cmd)
            self.logger.info("Result: %s", r)
        except SyntaxError:
            r = {}
            try:
                exec (cmd, ns)
                self.logger.info("Exec: %s", cmd)
            except ExitException:
                raise
            except Exception as e:
                self.logger.info("Failed to exec cmd %s: %s.", cmd, e)
                rval = False
            for key, value in list(ns.items()):
                if key not in nscopy or not value is nscopy[key]:
                    if key in self.namespace:
                        del self.namespace[key]
                    self.namespace[key] = value
                    r[key] = value
            self.logger.info("Set items in namespace: %s", r)
        except ExitException:
            raise
        except Exception as e:
            self.logger.info("Failed to eval cmd %s: %s", cmd, e)
            return False

        return rval

    def __init__(self, load_state: 'List[SystemObject]'=None, load_config: 'Dict[str, Any]'=None,
                 **traits):
        super().__init__(**traits)
        if not self.name:
            self.name = self.__class__.__name__
            if self.name == 'System':
                self.name = os.path.split(sys.argv[0])[-1].replace('.py', '')

        # Initialize Sentry / raven client, if is configured
        if not self.raven_client and self.raven_dsn:
            self.raven_client = raven.Client(self.raven_dsn, release=__version__,
                                             tags={'automate-system': self.name})

        self._initialize_logging()
        self.worker_thread = StatusWorkerThread(name="Status worker thread", system=self)
        self.logger.info('Initializing services')
        self._initialize_services()
        self.logger.info('Initializing namespace')
        self._initialize_namespace(load_state)

        if load_config:
            self.logger.info('Loading config')
            for obj_name, status in load_config.items():
                if hasattr(self, obj_name):
                    getattr(self, obj_name).status = status

        self.logger.info('Initialize user services')
        self._setup_user_services()

        if self.worker_autostart:
            self.logger.info('Starting worker thread')
            self.worker_thread.start()

        self.post_init_trigger = True

    def _initialize_logging(self):
        root_logger = logging.getLogger('automate')
        self.logger = root_logger.getChild(self.name)

        # Check if root level logging has been set up externally.

        if len(root_logger.handlers) > 0:
            root_logger.info('Logging has been configured already, '
                             'skipping logging configuration')
            return

        root_logger.propagate = False
        root_logger.setLevel(self.log_level)
        self.logger.setLevel(self.log_level)

        if self.raven_client:
            sentry_handler = SentryHandler(client=self.raven_client, level=logging.ERROR)
            root_logger.addHandler(sentry_handler)

        if self.logfile:
            formatter = logging.Formatter(fmt=self.logfile_format)
            log_handler = logging.FileHandler(self.logfile)
            log_handler.setFormatter(formatter)
            root_logger.addHandler(log_handler)

        stream_handler = logging.StreamHandler()

        from colorlog import ColoredFormatter, default_log_colors
        colors = default_log_colors.copy()
        colors['DEBUG'] = 'purple'

        stream_handler.setFormatter(ColoredFormatter(self.log_format, datefmt='%H:%M:%S', log_colors=colors))
        root_logger.addHandler(stream_handler)

        self.logger.info('Logging setup ready')

    def _initialize_namespace(self, load_state=None):
        self.namespace = Namespace(system=self)
        self.namespace.set_system(load_state)

        self.logger.info('Setup loggers per object')
        for name, obj in self.namespace.items():
            if isinstance(obj, SystemObject):
                ctype = obj.__class__.__name__
                obj.logger = self.logger.getChild('%s.%s' % (ctype, name))

    def _initialize_services(self):
        # Add default_services, if not already
        for servname in self.default_services:
            if servname not in self.service_names | self.exclude_services:
                self.services.append(get_service_by_name(servname)())

        # Add autorun services if not already
        for servclass in get_autoload_services():
            if servclass.__name__ not in self.service_names | self.exclude_services:
                self.services.append(servclass())

    def _setup_user_services(self):
        for ser in (i for i in self.services if isinstance(i, AbstractUserService)):
            self.logger.info('...%s', ser.__class__.__name__)
            ser.setup_system(self)


# Load extensions

from . import services, sensors, actuators, callables
print('Loading extensions')
for entry_point in pkg_resources.iter_entry_points('automate.extension'):
    print('Trying to load extension %s' % entry_point)
    try:
        ext_classes = entry_point.load(require=False)
    except ImportError:
        print('Loading extension %s failed. Perhaps missing requirements? Skipping.' % entry_point)
        continue
    for ext_class in ext_classes:
        print('... %s' % ext_class.__name__)
        if issubclass(ext_class, AbstractService):
            setattr(services, ext_class.__name__, ext_class)
        elif issubclass(ext_class, AbstractSensor):
            setattr(sensors, ext_class.__name__, ext_class)
        elif issubclass(ext_class, AbstractActuator):
            setattr(actuators, ext_class.__name__, ext_class)
        elif issubclass(ext_class, AbstractCallable):
            setattr(callables, ext_class.__name__, ext_class)
