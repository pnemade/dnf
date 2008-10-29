import os
import sys
import unittest

import settestpath
import logging
import yum.logginglevels as logginglevels

new_behavior = "NEW_BEHAVIOR" in os.environ.keys()

from yum import YumBase
from yum import transactioninfo
from yum import packages
from yum import packageSack
from yum.constants import TS_INSTALL_STATES, TS_REMOVE_STATES
from cli import YumBaseCli
import inspect
from rpmUtils import arch

#############################################################
### Helper classes ##########################################
#############################################################

# Dummy translation wrapper
def _(msg):
    return msg

class FakeConf(object):

    def __init__(self):
        self.installonlypkgs = ['kernel']
        self.exclude = []
        self.debuglevel = 0
        self.obsoletes = True
        self.exactarch = False
        self.exactarchlist = []
        self.installroot = '/'
        self.tsflags = []
        self.installonly_limit = 0
        self.skip_broken = False
        self.disable_excludes = []
        self.multilib_policy = 'best'
        self.persistdir = '/should-not-exist-bad-test!'

class FakeRepo(object):

    def __init__(self, id=None,sack=None):
        self.id = id
        self.sack = sack

class FakePackage(packages.YumAvailablePackage):

    def __init__(self, name, version='1.0', release='1', epoch='0', arch='noarch', repo=None):
        if repo is None:
            repo = FakeRepo()
            print "creating empty repo for %s-%s:%s-%s.%s " % (name, epoch,
                                                               version, release,
                                                               arch)
        packages.YumAvailablePackage.__init__(self, repo)

        self.name = name
        self.version = version
        self.ver = version
        self.release = release
        self.rel = release
        self.epoch = epoch
        self.arch = arch
        self.pkgtup = (self.name, self.arch, self.epoch, self.version, self.release)

        self.prco['provides'].append((name, 'EQ', (epoch, version, release)))

        # Just a unique integer
        self.id = self.__hash__()

    def addProvides(self, name, flag=None, evr=(None, None, None)):
        self.prco['provides'].append((name, flag, evr))
    def addRequires(self, name, flag=None, evr=(None, None, None)):
        self.prco['requires'].append((name, flag, evr))
    def addConflicts(self, name, flag=None, evr=(None, None, None)):
        self.prco['conflicts'].append((name, flag, evr))
    def addObsoletes(self, name, flag=None, evr=(None, None, None)):
        self.prco['obsoletes'].append((name, flag, evr))
    def addFile(self, name, ftype='file'):
        self.files[ftype].append(name)

class _Container(object):
    pass


class DepSolveProgressCallBack:
    """provides text output callback functions for Dependency Solver callback"""
    
    def __init__(self):
        """requires yum-cli log and errorlog functions as arguments"""
        self.verbose_logger = logging.getLogger("yum.verbose.cli")
        self.loops = 0
    
    def pkgAdded(self, pkgtup, mode):
        modedict = { 'i': _('installed'),
                     'u': _('updated'),
                     'o': _('obsoleted'),
                     'e': _('erased')}
        (n, a, e, v, r) = pkgtup
        modeterm = modedict[mode]
        self.verbose_logger.log(logginglevels.INFO_2,
            _('---> Package %s.%s %s:%s-%s set to be %s'), n, a, e, v, r,
            modeterm)
        
    def start(self):
        self.loops += 1
        
    def tscheck(self):
        self.verbose_logger.log(logginglevels.INFO_2, _('--> Running transaction check'))
        
    def restartLoop(self):
        self.loops += 1
        self.verbose_logger.log(logginglevels.INFO_2,
            _('--> Restarting Dependency Resolution with new changes.'))
        self.verbose_logger.debug('---> Loop Number: %d', self.loops)
    
    def end(self):
        self.verbose_logger.log(logginglevels.INFO_2,
            _('--> Finished Dependency Resolution'))

    
    def procReq(self, name, formatted_req):
        self.verbose_logger.log(logginglevels.INFO_2,
            _('--> Processing Dependency: %s for package: %s'), formatted_req,
            name)
        
    
    def unresolved(self, msg):
        self.verbose_logger.log(logginglevels.INFO_2, _('--> Unresolved Dependency: %s'),
            msg)

    
    def procConflict(self, name, confname):
        self.verbose_logger.log(logginglevels.INFO_2,
            _('--> Processing Conflict: %s conflicts %s'), name, confname)

    def transactionPopulation(self):
        self.verbose_logger.log(logginglevels.INFO_2, _('--> Populating transaction set '
            'with selected packages. Please wait.'))
    
    def downloadHeader(self, name):
        self.verbose_logger.log(logginglevels.INFO_2, _('---> Downloading header for %s '
            'to pack into transaction set.'), name)
      
#######################################################################
### Abstract super class for test cases ###############################
#######################################################################

class _DepsolveTestsBase(unittest.TestCase):

    res = {0 : 'empty', 2 : 'ok', 1 : 'err'}
    canonArch = "x86_64"

    def __init__(self, methodName='runTest'):
        unittest.TestCase.__init__(self, methodName)
        self.pkgs = _Container()
        self.buildPkgs(self.pkgs)

    def setUp(self):
        self._canonArch = arch.canonArch
        arch.canonArch = self.canonArch
    def tearDown(self):
        arch.canonArch = self._canonArch

    @staticmethod
    def buildPkgs(pkgs, *args):
        """Overload this staticmethod to create pkpgs that are used in several
        test cases. It gets called from __init__ with self.pkgs as first parameter.
        It is a staticmethod so you can call .buildPkgs() from other Tests to share
        buildPkg code (inheritance doesn't work here, because we don't want to
        inherit the test cases, too).
        """
        pass

    def assertResult(self, pkgs, optional_pkgs=[]):
        """Check if "system" contains the given pkgs. pkgs must be present,
        optional_pkgs may be. Any other pkgs result in an error. Pkgs are
        present if they are in the rpmdb and are not REMOVEd or they
        are INSTALLed.
        """
        errors = ["Unexpected result after depsolving: \n\n"]
        pkgs = set(pkgs)
        optional_pkgs = set(optional_pkgs)
        installed = set()

        for pkg in self.rpmdb:
            # got removed
            if self.tsInfo.getMembersWithState(pkg.pkgtup, TS_REMOVE_STATES):
                if pkg in pkgs:
                    errors.append("Package %s was removed!\n" % pkg)
            else: # still installed
                if pkg not in pkgs and pkg not in optional_pkgs:
                    errors.append("Package %s was not removed!\n" % pkg)
            installed.add(pkg)

        for txmbr in self.tsInfo.getMembersWithState(output_states=TS_INSTALL_STATES):
            installed.add(txmbr.po)
            if txmbr.po not in pkgs and txmbr.po not in optional_pkgs:
                errors.append("Package %s was installed!\n" % txmbr.po)
        for pkg in pkgs - installed:
            errors.append("Package %s was not installed!\n" % pkg)

        if len(errors) > 1:
            errors.append("\nTest case was:\n\n")
            errors.extend(inspect.getsource(inspect.stack()[1][0].f_code))
            errors.append("\n")
            self.fail("".join(errors))


#######################################################################
### Derive Tests from these classes or unittest.TestCase ##############
#######################################################################

class DepsolveTests(_DepsolveTestsBase):
    """Run depsolver on an manually  set up transaction.
    You can add pkgs to self.rpmdb or self.tsInfo. See
    yum/transactioninfo.py for details.
    A typical test case looks like:

    def testInstallPackageRequireInstalled(self):
        po = FakePackage('zsh', '1', '1', None, 'i386')
        po.addRequires('zip', 'EQ', (None, '1.3', '2'))
        self.tsInfo.addInstall(po)

        ipo = FakePackage('zip', '1.3', '2', None, 'i386')
        self.rpmdb.addPackage(ipo)

        result, msg = self.resolveCode()
        self.assertEquals('ok', result, msg)
        self.assertResult((po, ipo))
    """

    def setUp(self):
        """ Called at the start of each test. """
        _DepsolveTestsBase.setUp(self)
        self.tsInfo = transactioninfo.TransactionData()
        self.rpmdb  = packageSack.PackageSack()
        self.xsack  = packageSack.PackageSack()
        self.repo   = FakeRepo("installed")
        # XXX this side-affect is hacky:
        self.tsInfo.setDatabases(self.rpmdb, self.xsack)

    def resetTsInfo(self):
        self.tsInfo = transactioninfo.TransactionData()
        

    def resolveCode(self):
        solver = YumBase()
        solver.conf = FakeConf()
        solver.tsInfo = solver._tsInfo = self.tsInfo
        solver.rpmdb = self.rpmdb
        solver.pkgSack = self.xsack

        for po in self.rpmdb:
            po.repoid = po.repo.id = "installed"
        for po in self.xsack:
            po.repoid = po.repo.id = "TestRepository"
        for txmbr in self.tsInfo:
            if txmbr.ts_state in ('u', 'i'):
                txmbr.po.repoid = txmbr.po.repo.id = "TestRepository"
            else:
                txmbr.po.repoid = txmbr.po.repo.id = "installed"

        result, msg = solver.resolveDeps()
        return (self.res[result], msg)

class OperationsTests(_DepsolveTestsBase):
    """Run a yum command (install, update, remove, ...) in a given set of installed
    and available pkgs. Typical test case looks like:

    def testUpdate(self):
        p = self.pkgs
        res, msg = self.runOperation(['update'], [p.installed], [p.update])
        self.assert_(res=='ok', msg)
        self.assertResult((p.update,))

    To avoid creating the same pkgs over and over again overload the staticmethod
    buildPkgs. It gets called from __init__ with self.pkgs as first parameter.
    As it is a static method you can call .buildPkgs() from other Tests to share
    buildPkg code.
    """

    def runOperation(self, args, installed=[], available=[]):
        """Sets up and runs the depsolver. args[0] must be a valid yum command
        ("install", "update", ...). It might be followed by pkg names as on the
        yum command line. The pkg objects in installed are added to self.rpmdb and
        those in available to self.xsack which is the repository to resolve
        requirements from.
        """
        depsolver = YumBaseCli()
        self.rpmdb = depsolver.rpmdb = packageSack.PackageSack()
        self.xsack = depsolver._pkgSack  = packageSack.PackageSack()
        self.repo = depsolver.repo = FakeRepo("installed")
        depsolver.conf = FakeConf()
        # We are running nosetest, so we want to see some yum output
        # if a testcase if failing
        depsolver.doLoggingSetup(9,9)
        self.depsolver = depsolver

        for po in installed:
            po.repoid = po.repo.id = "installed"
            self.depsolver.rpmdb.addPackage(po)
        for po in available:
            po.repoid = po.repo.id = "TestRepository"
            self.depsolver._pkgSack.addPackage(po)

        self.depsolver.basecmd = args[0]
        self.depsolver.extcmds = args[1:]
        res, msg = self.depsolver.doCommands()
        self.tsInfo = depsolver.tsInfo
        if res!=2:
            return res, msg
        res, msg = self.depsolver.buildTransaction()
        return self.res[res], msg
