#!/usr/bin/env python
from __future__ import print_function, division

import os
import abc
import string
import functools
import subprocess
import getpass

from pymatgen.io.abinitio.launcher import ScriptEditor

__all__ = [
    "MpiRunner",
    "ShellAdapter",
    "SlurmAdapter",
    "PbsAdapter",
]

def is_string(obj):
    try:
        dummy = obj + ""
        return True
    except TypeError:
        return False


class Command(object):
    """
    From https://gist.github.com/kirpit/1306188

    Enables to run subprocess commands in a different thread with TIMEOUT option.

    Based on jcollado's solution:
    http://stackoverflow.com/questions/1191374/subprocess-with-timeout/4825933#4825933
    """
    command = None
    process = None
    status = None
    output, error = '', ''

    def __init__(self, command):
        if is_string(command):
            import shlex
            command = shlex.split(command)

        self.command = command

    def run(self, timeout=None, **kwargs):
        """ Run a command then return: (status, output, error). """
        def target(**kwargs):
            try:
                self.process = subprocess.Popen(self.command, **kwargs)
                self.output, self.error = self.process.communicate()
                self.status = self.process.returncode
            except:
                import traceback
                self.error = traceback.format_exc()
                self.status = -1
        # default stdout and stderr
        if 'stdout' not in kwargs:
            kwargs['stdout'] = subprocess.PIPE
        if 'stderr' not in kwargs:
            kwargs['stderr'] = subprocess.PIPE
        # thread
        import threading
        thread = threading.Thread(target=target, kwargs=kwargs)
        thread.start()
        thread.join(timeout)
        if thread.is_alive():
            self.process.terminate()
            thread.join()

        return self.status, self.output, self.error


class MpiRunner(object):
    """
    This object provides an abstraction for the mpirunner provided 
    by the different MPI libraries. It's main task is handling the
    different syntax and options supported by the different mpirunners.
    """
    def __init__(self, name, type=None, options=""):
        self.name = name
        self.type = None
        self.options = options

    def string_to_run(self, executable, mpi_ncpus, stdin=None, stdout=None, stderr=None):
        stdin = "< " + stdin if stdin is not None else ""
        stdout = "> " + stdout if stdout is not None else ""
        stderr = "2> " + stderr if stderr is not None else ""

        if self.has_mpirun:

            if self.type is None:
                #se.add_line('$MPIRUN -n $MPI_NCPUS $EXECUTABLE < $STDIN > $STDOUT 2> $STDERR')
                num_opt = "-n " + str(mpi_ncpus)
                cmd = " ".join([self.name, num_opt, executable, stdin, stdout, stderr])

            else:
                raise NotImplementedError("type %s is not supported!")

        else:
            #assert mpi_ncpus == 1
            cmd = " ".join([executable, stdin, stdout, stderr])

        return cmd

    @property
    def has_mpirun(self):
        return self.name is not None


class QScriptTemplate(string.Template):
    delimiter = '$$'


class QueueAdapterError(Exception):
    """Error class for exceptions raise by QueueAdapter."""


class AbstractQueueAdapter(object):
    """
    The QueueAdapter is responsible for all interactions with a specific
    queue management system. This includes handling all details of queue
    script format as well as queue submission and management.

    This is the Abstract base class defining the methods that 
    must be implemented by the concrete classes.
    A user should extend this class with implementations that work on
    specific queue systems.
    """
    __metaclass__ = abc.ABCMeta

    Error = QueueAdapterError

    def __init__(self, qparams=None, setup=None, modules=None, shell_env=None, omp_env=None, 
                 pre_rocket=None, post_rocket=None, mpi_runner=None):
        """
        Args:
            setup:
                String or list of commands executed during the initial setup.
            modules:
                String or list of modules to load before running the application.
            shell_env:
                Dictionary with the shell environment variables to export
                before running the application.
            omp_env:
                Dictionary with the OpenMP variables.
            pre_rocket:
                String or list of commands executed before launching the calculation.
            post_rocket:
                String or list of commands executed once the calculation is completed.
            mpi_runner:
                Path to mpirun or `MpiRunner` instance. None if not used
        """
        # Make defensive copies so that we can change the values at runtime.
        self.qparams = qparams.copy() if qparams is not None else {}

        if is_string(setup):
            setup = [setup]
        self.setup = setup[:] if setup is not None else []

        self.omp_env = omp_env.copy() if omp_env is not None else {}

        if is_string(modules):
            modules = [modules]
        self.modules = modules[:] if modules is not None else []

        self.shell_env = shell_env.copy() if shell_env is not None else {}

        self.mpi_runner = mpi_runner
        if not isinstance(mpi_runner, MpiRunner):
            self.mpi_runner = MpiRunner(mpi_runner)

        if is_string(pre_rocket):
            pre_rocket = [pre_rocket]
        self.pre_rocket = pre_rocket[:] if pre_rocket is not None else []

        if is_string(post_rocket):
            post_rocket = [post_rocket]
        self.post_rocket = post_rocket[:] if post_rocket is not None else []

        # Parse the template so that we know the list of supported options.
        cls = self.__class__
        if hasattr(cls, "QTEMPLATE"): 
            # Consistency check.
            err_msg = ""
            for param in self.qparams:
                if param not in self.supported_qparams:
                    err_msg += "Unsupported QUEUE parameter name %s\n"  % param

            if err_msg:
                raise ValueError(err_msg)

    #def copy(self):
        #return self.__class__(qparams, setup=None, modules=None, shell_env=None, omp_env=None, pre_rocket=None, post_rocket=None, mpi_runner)

    @property
    def supported_qparams(self):
        """
        Dictionary with the supported parameters that can be passed to the queue manager.
        (obtained by parsing QTEMPLATE.
        """ 
        try:
            return self._supported_qparams

        except AttributeError:
            import re
            self._supported_qparams = re.findall("\$\$\{(\w+)\}", self.QTEMPLATE)
            return self._supported_qparams
    
    @property
    def has_mpirunner(self):
        """True if we are using a mpirunner"""
        return bool(self.mpi_runner)

    @property
    def has_omp(self):
        """True if we are using OpenMP threads"""
        return hasattr(self,"omp_env") and bool(getattr(self, "omp_env"))

    @property
    def tot_ncpus(self):
        """Total number of CPUs employed"""
        return self.mpi_ncpus * self.omp_ncpus 

    @property
    def omp_ncpus(self):
        """Number of OpenMP threads."""
        if self.has_omp:
            return self.omp_env["OMP_NUM_THREADS"]
        else:
            return 1

    @abc.abstractproperty
    def mpi_ncpus(self):
        """Number of CPUs used for MPI."""

    @abc.abstractmethod
    def set_mpi_ncpus(self, mpi_ncpus):
        """Set the number of CPUs used for MPI."""

    #@abc.abstractproperty
    #def queue_walltime(self):
    #    """Returns the walltime in seconds."""

    #@abc.abstractmethod
    #def set_queue_walltime(self):
    #    """Set the walltime in seconds."""

    #@abc.abstractproperty
    #def qout_path(self):
    #    """
    #    Absolute path to the output file produced by the queue manager. 
    #    None if not available.
    #    """

    #@abc.abstractproperty
    #def qerr_path(self):
    #    """
    #    Absolute Path to the error file produced by the queue manager. 
    #    None if not available.
    #    """

    def _make_qheader(self, job_name):
        """Return a string with the options that are passed to the resource manager."""
        a = QScriptTemplate(self.QTEMPLATE)

        # set substitution dict for replacements into the template and clean null values
        subs_dict = {k: v for k,v in self.qparams.items() if v is not None}  

        # Set job_name and the names for the stderr and stdout of the 
        # queue manager (note the use of the extensions .qout and .qerr
        # so that we can easily locate this file.
        subs_dict['job_name'] = job_name 

        # might contain unused parameters as leftover $$.
        unclean_template = a.safe_substitute(subs_dict)  

        # Remove lines with leftover $$.
        clean_template = []
        for line in unclean_template.split('\n'):
            if '$$' not in line:
                clean_template.append(line)

        return '\n'.join(clean_template)

    def get_script_str(self, job_name, launch_dir, executable, stdin=None, stdout=None, stderr=None):
        """
        returns a (multi-line) String representing the queue script, e.g. PBS script.
        Uses the template_file along with internal parameters to create the script.

        :param launch_dir: (str) The directory the job will be launched in
        """
        qheader = self._make_qheader(job_name)

        se = ScriptEditor()

        if self.setup:
            se.add_comment("Setup section")
            se.add_lines(self.setup)

        if self.modules:
            se.add_comment("Load Modules")
            se.add_line("module purge")
            se.load_modules(self.modules)

        if self.has_omp:
            se.add_comment("OpenMp Environment")
            se.declare_vars(self.omp_env)

        if self.shell_env:
            se.add_comment("Shell Environment")
            se.declare_vars(self.shell_env)

        # Cd to launch_dir
        #print(launch_dir)
        se.add_line("cd " + launch_dir)

        if self.pre_rocket:
            se.add_comment("Commands before execution")
            se.add_lines(self.pre_rocket)

        mpi_ncpus = self.mpi_ncpus

        line = self.mpi_runner.string_to_run(executable, mpi_ncpus, stdin=stdin, stdout=stdout, stderr=stderr)
        se.add_line(line)

        if self.post_rocket:
            se.add_comment("Commands after execution")
            se.add_lines(self.post_rocket)

        shell_text = se.get_script_str()

        return qheader + shell_text

    @abc.abstractmethod
    def submit_to_queue(self, script_file):
        """
        submits the job to the queue, probably using subprocess or shutil

        :param script_file: (str) name of the script file to use (String)
        """

    @abc.abstractmethod
    def get_njobs_in_queue(self, username=None):
        """
        returns the number of jobs in the queue, probably using subprocess or shutil to
        call a command like 'qstat'. returns None when the number of jobs cannot be determined.

        :param username: (str) the username of the jobs to count (default is to autodetect)
        """

####################
# Concrete classes #
####################

class ShellAdapter(AbstractQueueAdapter):

    QTEMPLATE = """\
#!/bin/bash

export MPI_NCPUS=$${MPI_NCPUS}
"""

    @property
    def mpi_ncpus(self):
        """Number of CPUs used for MPI."""
        return self.qparams.get("MPI_NCPUS", 1)
                                                    
    def set_mpi_ncpus(self, mpi_ncpus):
        """Set the number of CPUs used for MPI."""
        self.qparams["MPI_NCPUS"] = mpi_ncpus

    def submit_to_queue(self, script_file):
        process = subprocess.Popen(("/bin/bash", script_file), stderr=subprocess.PIPE)
        return process

    def get_njobs_in_queue(self, username=None):
        return None


class SlurmAdapter(AbstractQueueAdapter):

    QTEMPLATE = """
#!/bin/bash

#SBATCH --ntasks=$${ntasks}
#SBATCH --ntasks-per-node=$${ntasks_per_node}
#SBATCH --cpus-per-task=$${cpus_per_task}
#SBATCH --time=$${time}
#SBATCH --partition=$${partition}
#SBATCH --account=$${account}
#SBATCH --job-name=$${job_name}
#SBATCH --output=$${job_name}.qout
#SBATCH --error=$${job_name}.qerr
"""

    @property
    def mpi_ncpus(self):
        """Number of CPUs used for MPI."""
        return self.qparams.get("ntasks", 1)

    def set_mpi_ncpus(self, mpi_ncpus):
        """Set the number of CPUs used for MPI."""
        self.qparams["ntasks"] = mpi_ncpus

    def submit_to_queue(self, script_file):
        if not os.path.exists(script_file):
            raise self.Error('Cannot find script file located at: {}'.format(script_file))

        # submit the job
        try:
            cmd = ['sbatch', script_file]
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            p.wait()

            # grab the returncode. SLURM returns 0 if the job was successful
            if p.returncode == 0:
                try:
                    # output should of the form '2561553.sdb' or '352353.jessup' - just grab the first part for job id
                    job_id = int(p.stdout.read().split()[3])
                    sprint('Job submission was successful and job_id is {}'.format(job_id))
                    return job_id

                except:
                    # probably error parsing job code
                    log_exception(slurm_logger, 'Could not parse job id following slurm...')

            else:
                # some qsub error, e.g. maybe wrong queue specified, don't have permission to submit, etc...
                err_msg = ("Error in job submission with SLURM file {f} and cmd {c}\n".format(f=script_file, c=cmd) + 
                           "The error response reads: {}".format(p.stderr.read()))
                raise self.Error(err_msg)

        except:
            # random error, e.g. no qsub on machine!
            raise self.Error('Running sbatch caused an error...')

    def get_njobs_in_queue(self, username=None):
        if username is None:
            username = getpass.getuser()

        cmd = ['squeue', '-o "%u"', '-u', username]
        p = subprocess.Popen(cmd, shell=False, stdout=subprocess.PIPE)
        p.wait()

        # parse the result
        if p.returncode == 0:
            # lines should have this form
            # username
            # count lines that include the username in it

            outs = p.stdout.readlines()
            njobs = len([line.split() for line in outs if username in line])
            print('The number of jobs currently in the queue is: {}'.format(njobs))
            return njobs

        # there's a problem talking to squeue server?
        slurm_logger = self.get_qlogger('qadapter.slurm')
        msgs = ['Error trying to get the number of jobs in the queue using squeue service',
                'The error response reads: {}'.format(p.stderr.read())]
        log_fancy(slurm_logger, 'error', msgs)

        return None


class PbsAdapter(AbstractQueueAdapter):

    QTEMPLATE = """
#!/bin/bash

#PBS -A $${account}
#PBS -l walltime=$${walltime}
#PBS -q $${queue}
#PBS -l mppwidth=$${mppwidth}
#PBS -l nodes=$${nodes}:ppn=$${ppn}
#PBS -N $${job_name}
#PBS -o $${job_name}.qout
#PBS -e $${job_name}.qerr
"""

    #@property
    #def mpi_ncpus(self):
    #    """Number of CPUs used for MPI."""
    #    return self.qparams.get("nodes", 1) * self.qparams.get("ppn", 1)
                                                    
    #def set_mpi_ncpus(self, mpi_ncpus):
    #    """Set the number of CPUs used for MPI."""
    #    if "ppn" not in self.qparams:
    #       self.qparams["ppn"] = 1
    #    ppnode = self.qparams.get("ppn")
    #    self.qparams["nodes"] = mpi_ncpus // ppnode 

    def submit_to_queue(self, script_file):

        if not os.path.exists(script_file):
            raise self.Error('Cannot find script file located at: {}'.format(script_file))

        pbs_logger = self.get_qlogger('qadapter.pbs')

        # submit the job
        try:
            cmd = ['qsub', script_file]
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            p.wait()

            # grab the returncode. PBS returns 0 if the job was successful
            if p.returncode == 0:
                try:
                    # output should of the form '2561553.sdb' or '352353.jessup' - just grab the first part for job id
                    job_id = int(p.stdout.read().split('.')[0])
                    pbs_logger.info('Job submission was successful and job_id is {}'.format(job_id))
                    return job_id
                except:
                    # probably error parsing job code
                    raise self.Error("Could not parse job id following qsub...")

            else:
                # some qsub error, e.g. maybe wrong queue specified, don't have permission to submit, etc...
                msgs = [
                    'Error in job submission with PBS file {f} and cmd {c}'.format(f=script_file, c=cmd),
                    'The error response reads: {}'.format(p.stderr.read())]
                log_fancy(pbs_logger, 'error', msgs)

        except:
            # random error, e.g. no qsub on machine!
            raise self.Error("Running qsub caused an error...")

    def get_njobs_in_queue(self, username=None):
        # initialize username
        if username is None:
            username = getpass.getuser()

        # run qstat
        qstat = Command(['qstat', '-a', '-u', username])
        p = qstat.run(timeout=5)

        # parse the result
        if p[0] == 0:
            # lines should have this form
            # '1339044.sdb          username  queuename    2012-02-29-16-43  20460   --   --    --  00:20 C 00:09'
            # count lines that include the username in it

            # TODO: only count running or queued jobs. or rather, *don't* count jobs that are 'C'.
            outs = p[1].split('\n')
            njobs = len([line.split() for line in outs if username in line])
            pbs_logger.info('The number of jobs currently in the queue is: {}'.format(njobs))
            return njobs

        # there's a problem talking to qstat server?
        pbs_logger = self.get_qlogger('qadapter.pbs')
        msgs = ['Error trying to get the number of jobs in the queue using qstat service',
                'The error response reads: {}'.format(p[2])]
        log_fancy(pbs_logger, 'error', msgs)

        return None


def qadapter_class(qtype):
    return {
        "shell": ShellAdapter,
        "slurm": SlurmAdapter,
        "pbs": PbsAdapter,

    }[qtype.lower()]


if __name__ == "__main__":

    slurm = SlurmAdapter(
        qparams=dict(
            ntasks=12,
            partition="hmem",
            account='nobody@nowhere.org',
            time="119:59:59",
            #ntasks_per_node=None,
            #cpus_per_task=None,
            #ntasks=None,
            #time=None,
            #partition=None,
            #account=None,
        ),
        setup = ["echo 'This is the list of commands executed during the initial setup'", "ssh user@node01"],
        # List of modules to load before running the application.
        modules = ['intel-compilers/12.0.4.191', 'MPI/Intel/mvapich2/1.6', 'FFTW/3.3'],
        # Dictionary with the shell environment variables to export before running the application.
        shell_env = dict(FOO=1, PATH="/home/gmatteo/bin:$PATH"),
        # OpenMP variables.
        omp_env = dict(OMP_NUM_THREADS=1),
        pre_rocket = ["echo 'List of command executed before launching the calculation'" ],
        post_rocket = ["echo 'List of command executed once the calculation is completed'" ],
        mpi_runner= "mpirun",
    )

    shell = ShellAdapter(
        qparams={},
        setup=["echo 'List of commands executed during the initial setup'", "ssh user@node01"],
        # List of modules to load before running the application.
        modules= ['intel-compilers/12.0.4.191', 'MPI/Intel/mvapich2/1.6', 'FFTW/3.3'],
        # Dictionary with the shell environment variables to export
        # before running the application.
        shell_env = dict(MPI_NCPUS=1, BAR=None, PATH="/home/gmatteo/bin:$PATH"),
        # OpenMP variables.
        omp_env = dict(OMP_NUM_THREADS=1),
        pre_rocket = ["echo 'List of command executed before launching the calculation'" ],
        post_rocket = ["echo 'List of command executed once the calculation is completed'" ],
        mpi_runner = "mpirun",
        )

    for qad in [shell, slurm]:

        for num in [2, 3]:
            qad.set_mpi_ncpus(num)

            script = qad.get_script_str(job_name="myjob", launch_dir="hello", 
                executable="abinit", stdin="STDIN", stdout="STDOUT", stderr="STDERR")

            print(script)

            #script_file = "job_file.sh"
            #with open(script_file, "w") as fh:
            #    fh.write(script)

            #process = qad.submit_to_queue(script_file)

            #import cPickle as pickle
            #with open("test.pickle", "w") as fh:
            #    #pickle.dump(qad, fh, protocol=0)
            #    pickle.dump(cls, fh, protocol=0)
