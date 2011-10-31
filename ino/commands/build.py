# -*- coding: utf-8; -*-

import re
import os.path
import inspect
import subprocess
import jinja2

from jinja2.runtime import StrictUndefined

import ino.filters

from ino.commands.base import Command
from ino.filters import colorize
from ino.utils import SpaceList, list_subdirs
from ino.exc import Abort


class Build(Command):
    """
    Build a project in the current directory and produce a ready-to-upload
    firmware file.

    The project is expected to have `src' subdirectroy where all its sources
    are located. This directory is scanned recursively to find
    *.[c|cpp|pde|ino] files. They are compiled and linked into resulting
    firmware hex-file.

    Also any external library dependencies are tracked automatically. If a
    source file includes any library found among standard Arduino libraries or
    a library placed in `lib' subdirectory of the project, the library gets
    build too.

    Build artifacts are placed in `.build' subdirectory of the project.
    """

    name = 'build'
    help_line = "Build firmware from the current directory project"

    def setup_arg_parser(self, parser):
        super(Build, self).setup_arg_parser(parser)
        self.e.add_board_model_arg(parser)
        self.e.add_arduino_dist_arg(parser)

    def discover(self):
        self.e.find_arduino_dir('arduino_core_dir', 
                                ['hardware', 'arduino', 'cores', 'arduino'], 
                                ['WProgram.h'], 
                                'Arduino core library')

        self.e.find_arduino_dir('arduino_libraries_dir', ['libraries'],
                                human_name='Arduino standard libraries')

        
        self.e.find_arduino_file('version.txt', ['lib'],
                                 human_name='Arduino lib version file (version.txt)')

        if 'arduino_lib_version' not in self.e:
            with open(self.e['version.txt']) as f:
                print 'Detecting Arduino software version ... ',
                self.e['arduino_lib_version'] = v = int(f.read().strip())
                print colorize(str(v), 'green')

        self.e.find_tool('cc', ['avr-gcc'], human_name='avr-gcc')
        self.e.find_tool('cxx', ['avr-g++'], human_name='avr-g++')
        self.e.find_tool('ar', ['avr-ar'], human_name='avr-ar')
        self.e.find_tool('objcopy', ['avr-objcopy'], human_name='avr-objcopy')

    def setup_flags(self, board):
        mcu = '-mmcu=' + board['build']['mcu']
        self.e['cflags'] = SpaceList([
            mcu,
            '-ffunction-sections',
            '-fdata-sections',
            '-g',
            '-Os', 
            '-w',
            '-DF_CPU=' + board['build']['f_cpu'],
            '-DARDUINO=' + str(self.e['arduino_lib_version']),
            '-I' + self.e['arduino_core_dir'],
        ])

        self.e['cxxflags'] = SpaceList(['-fno-exceptions'])
        self.e['elfflags'] = SpaceList(['-Os', '-Wl,--gc-sections', mcu])

        self.e['names'] = {
            'obj': '%s.o',
            'lib': 'lib%s.a',
        }

    def create_jinja(self):
        templates_dir = os.path.join(os.path.dirname(__file__), '..', 'make')
        self.jenv = jinja2.Environment(
            loader=jinja2.FileSystemLoader(templates_dir),
            undefined=StrictUndefined, # bark on Undefined render
            extensions=['jinja2.ext.do'])

        # inject @filters from ino.filters
        for name, f in inspect.getmembers(ino.filters, lambda x: getattr(x, 'filter', False)):
            self.jenv.filters[name] = f

        # inject globals
        self.jenv.globals['e'] = self.e
        self.jenv.globals['SpaceList'] = SpaceList

    def render_template(self, source, target, **ctx):
        template = self.jenv.get_template(source)
        contents = template.render(**ctx)
        out_path = os.path.join(self.e['build_dir'], target)
        with open(out_path, 'wt') as f:
            f.write(contents)

        return out_path

    def recursive_inc_lib_flags(self, libdirs):
        flags = SpaceList()
        for d in libdirs:
            flags.append('-I' + d)
            flags.extend('-I' + subd for subd in list_subdirs(d, recursive=True, exclude=['examples']))
        return flags

    def _scan_dependencies(self, dir, lib_dirs, inc_flags):
        output_filepath = os.path.join(self.e.build_dir, os.path.basename(dir), 'dependencies.d')
        makefile = self.render_template('Makefile.deps.jinja', 'Makefile.deps',
                                        inc_flags=inc_flags, src_dir=dir,
                                        output_filepath=output_filepath)

        subprocess.call(['make', '-f', makefile, 'all'])
        self.e['deps'].append(output_filepath)

        # search for dependencies on libraries
        # for this scan dependency file generated by make
        # with regexes to find entries that start with
        # libraries dirname
        re_template = ur'\s+(?P<libdir>{dirname}{slash}([^{slash}])+){slash}'
        re_compile = lambda dirname: re.compile(
            re_template.format(dirname=re.escape(dirname),
                               slash=re.escape(os.path.sep)))

        local_re = re_compile(self.e.lib_dir)
        dist_re = re_compile(self.e.arduino_libraries_dir)

        used_libs = set()
        with open(output_filepath) as f:
            for line in f:
                match = local_re.search(line) or dist_re.search(line)
                if match:
                    used_libs.add(match.group('libdir'))

        return used_libs

    def scan_dependencies(self):
        self.e['deps'] = SpaceList()

        lib_dirs = list_subdirs(self.e.lib_dir) + list_subdirs(self.e.arduino_libraries_dir)
        inc_flags = self.recursive_inc_lib_flags(lib_dirs)

        used_libs = self._scan_dependencies(self.e.src_dir, lib_dirs, inc_flags)
        scanned_libs = set()
        while scanned_libs != used_libs:
            for lib_dir in list(used_libs - scanned_libs):
                used_libs |= self._scan_dependencies(lib_dir, lib_dirs, inc_flags)
                scanned_libs.add(lib_dir)

        self.e['extra_libs'] = list(used_libs)
        self.e['cflags'].extend(self.recursive_inc_lib_flags(used_libs))

    def build(self):
        makefile = self.render_template('Makefile.jinja', 'Makefile')
        subprocess.call(['make', '-f', makefile, 'all'])

    def run(self, args):
        self.discover()
        self.setup_flags(args.board_model)
        self.create_jinja()
        self.scan_dependencies()
        self.build()
