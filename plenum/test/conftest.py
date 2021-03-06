import importlib
import inspect
import itertools
import json
import logging
import os
import re
import warnings
from copy import copy
from functools import partial
from typing import Dict, Any

import gc
import pip
import pytest
from plenum.common.keygen_utils import initNodeKeysForBothStacks
from stp_core.crypto.util import randomSeed
from stp_core.network.port_dispenser import genHa
from stp_core.types import HA
from _pytest.recwarn import WarningsRecorder

from ledger.compact_merkle_tree import CompactMerkleTree
from ledger.ledger import Ledger
from ledger.serializers.compact_serializer import CompactSerializer
from plenum.common.config_util import getConfig
from stp_core.loop.eventually import eventually, eventuallyAll
from plenum.common.exceptions import BlowUp
from stp_core.common.log import getlogger
from stp_core.common.logging.handlers import TestingHandler
from stp_core.loop.looper import Looper, Prodable
from plenum.common.constants import TXN_TYPE, DATA, NODE, ALIAS, CLIENT_PORT, \
    CLIENT_IP, NODE_PORT, NYM, CLIENT_STACK_SUFFIX, PLUGIN_BASE_DIR_PATH
from plenum.common.txn_util import getTxnOrderedFields
from plenum.common.types import PLUGIN_TYPE_STATS_CONSUMER
from plenum.common.util import getNoInstances, getMaxFailures
from plenum.server.notifier_plugin_manager import PluginManager
from plenum.test.helper import randomOperation, \
    checkReqAck, checkLastClientReqForNode, checkSufficientRepliesRecvd, \
    checkViewNoForNodes, requestReturnedToNode, randomText, \
    mockGetInstalledDistributions, mockImportModule
from plenum.test.node_request.node_request_helper import checkPrePrepared, \
    checkPropagated, checkPrepared, checkCommitted
from plenum.test.plugin.helper import getPluginPath
from plenum.test.test_client import genTestClient, TestClient
from plenum.test.test_node import TestNode, TestNodeSet, Pool, \
    checkNodesConnected, ensureElectionsDone, genNodeReg

logger = getlogger()
config = getConfig()

UseZStack = config.UseZStack


@pytest.fixture(scope="session")
def warnfilters():
    def _():
        warnings.filterwarnings('ignore', category=DeprecationWarning, module='jsonpickle\.pickler', message='encodestring\(\) is a deprecated alias')
        warnings.filterwarnings('ignore', category=DeprecationWarning, module='jsonpickle\.unpickler', message='decodestring\(\) is a deprecated alias')
        warnings.filterwarnings('ignore', category=DeprecationWarning, module='plenum\.client\.client', message="The 'warn' method is deprecated")
        warnings.filterwarnings('ignore', category=DeprecationWarning, module='plenum\.common\.stacked', message="The 'warn' method is deprecated")
        warnings.filterwarnings('ignore', category=DeprecationWarning, module='plenum\.test\.test_testable', message='Please use assertEqual instead.')
        warnings.filterwarnings('ignore', category=DeprecationWarning, module='prompt_toolkit\.filters\.base', message='inspect\.getargspec\(\) is deprecated')
        warnings.filterwarnings('ignore', category=ResourceWarning, message='unclosed event loop')
        warnings.filterwarnings('ignore', category=ResourceWarning, message='unclosed file')
        warnings.filterwarnings('ignore', category=ResourceWarning, message='unclosed.*socket\.socket')
    return _


@pytest.yield_fixture(scope="session", autouse=True)
def warncheck(warnfilters):
    with WarningsRecorder() as record:
        warnfilters()
        yield
        gc.collect()
    to_prints = []

    def keyfunc(_):
        return _.category.__name__, _.filename, _.lineno

    _sorted = sorted(record, key=keyfunc)
    _grouped = itertools.groupby(_sorted, keyfunc)
    for k, g in _grouped:
        to_prints.append("\n"
                         "category: {}\n"
                         "filename: {}\n"
                         "  lineno: {}".format(*k))
        messages = itertools.groupby(g, lambda _: str(_.message))
        for k2, g2 in messages:
            count = sum(1 for _ in g2)
            count_str = ' ({} times)'.format(count) if count > 1 else ''
            to_prints.append("     msg: {}{}".format(k2, count_str))
    if to_prints:
        to_prints.insert(0, 'Warnings found:')
        pytest.fail('\n'.join(to_prints))


@pytest.fixture(scope="session", autouse=True)
def setResourceLimits():
    import resource
    flimit = 65535
    plimit = 65535
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (flimit, flimit))
        resource.setrlimit(resource.RLIMIT_NPROC, (plimit, plimit))
    except Exception as ex:
        print('Could not set resource limits due to {}'.format(ex))


def getValueFromModule(request, name: str, default: Any = None):
    """
    Gets an attribute from the request's module if attribute is found
    else return the default value

    :param request:
    :param name: name of attribute to get from module
    :param default: value to return if attribute was not found
    :return: value of the attribute if attribute was found in module else the default value
    """
    if hasattr(request.module, name):
        value = getattr(request.module, name)
        logger.info("found {} in the module: {}".
                    format(name, value))
    else:
        value = default if default is not None else None
        logger.info("no {} found in the module, using the default: {}".
                    format(name, value))
    return value


basePath = os.path.dirname(os.path.abspath(__file__))
testPluginBaseDirPath = os.path.join(basePath, "plugin")

overriddenConfigValues = {
    "DefaultPluginPath": {
        PLUGIN_BASE_DIR_PATH: testPluginBaseDirPath,
        PLUGIN_TYPE_STATS_CONSUMER: "stats_consumer"
    },
    'UpdateGenesisPoolTxnFile': False,
    'EnsureLedgerDurability': False
}


@pytest.fixture(scope="module")
def allPluginsPath():
    return [getPluginPath('stats_consumer')]


@pytest.fixture(scope="module")
def keySharedNodes(startedNodes):
    # for n in startedNodes:
    #     n.startKeySharing()
    return startedNodes


@pytest.fixture(scope="module")
def startedNodes(nodeSet, looper):
    for n in nodeSet:
        n.start(looper.loop)
    return nodeSet


@pytest.fixture(scope="module")
def whitelist(request):
    return getValueFromModule(request, "whitelist", [])


@pytest.fixture(scope="module")
def concerningLogLevels(request):
    # TODO need to enable WARNING for all tests
    default = [  # logging.WARNING,
        logging.ERROR,
        logging.CRITICAL]
    return getValueFromModule(request, "concerningLogLevels", default)


@pytest.fixture(scope="function", autouse=True)
def logcapture(request, whitelist, concerningLogLevels):
    baseWhitelist = ['seconds to run once nicely',
                     'Executing %s took %.3f seconds',
                     'is already stopped',
                     'Error while running coroutine',
                     'not trying any more because',
                     # TODO: This is too specific, move it to the particular test
                     "Beta discarding message INSTANCE_CHANGE(viewNo='BAD') "
                     "because field viewNo has incorrect type: <class 'str'>",
                     'got exception while closing hash store',
                     # TODO: Remove these once the relevant bugs are fixed
                     '.+ failed to ping .+ at',
                     'discarding message (NOMINATE|PRIMARY)',
                     '.+ rid .+ has been removed',
                     'last try...'
                     ]
    wlfunc = inspect.isfunction(whitelist)

    def tester(record):
        isBenign = record.levelno not in concerningLogLevels
        # TODO is this sufficient to test if a log is from test or not?
        isTest = os.path.sep + 'test' in record.pathname

        if wlfunc:
            wl = whitelist()
        else:
            wl = whitelist

        whiteListedExceptions = baseWhitelist + wl

        # Converting the log message to its string representation, the log
        # message can be an arbitrary object
        msg = str(record.msg)
        isWhiteListed = bool([w for w in whiteListedExceptions
                              if re.search(w, msg)])

        if not (isBenign or isTest or isWhiteListed):
            # Stopping all loopers, so prodables like nodes, clients, etc stop.
            #  This helps in freeing ports
            for fv in request._fixture_values.values():
                if isinstance(fv, Looper):
                    fv.stopall()
                if isinstance(fv, Prodable):
                    fv.stop()
            raise BlowUp("{}: {} ".format(record.levelname, record.msg))

    ch = TestingHandler(tester)
    logging.getLogger().addHandler(ch)

    def cleanup():
        logging.getLogger().removeHandler(ch)

    request.addfinalizer(cleanup)
    config = getConfig(tdir)
    for k, v in overriddenConfigValues.items():
        setattr(config, k, v)


@pytest.yield_fixture(scope="module")
def nodeSet(request, tdir, nodeReg, allPluginsPath, patchPluginManager):
    primaryDecider = getValueFromModule(request, "PrimaryDecider", None)
    with TestNodeSet(nodeReg=nodeReg, tmpdir=tdir,
                     primaryDecider=primaryDecider,
                     pluginPaths=allPluginsPath) as ns:
        yield ns


@pytest.fixture(scope='module')
def tdir(tmpdir_factory):
    tempdir = tmpdir_factory.mktemp('').strpath
    logger.debug("module-level temporary directory: {}".format(tempdir))
    return tempdir

another_tdir = tdir


@pytest.fixture(scope='function')
def tdir_for_func(tmpdir_factory):
    tempdir = tmpdir_factory.mktemp('').strpath
    logging.debug("function-level temporary directory: {}".format(tempdir))
    return tempdir


@pytest.fixture(scope="module")
def nodeReg(request) -> Dict[str, HA]:
    nodeCount = getValueFromModule(request, "nodeCount", 4)
    return genNodeReg(count=nodeCount)


@pytest.yield_fixture(scope="module")
def unstartedLooper(nodeSet):
    with Looper(nodeSet, autoStart=False) as l:
        yield l


@pytest.fixture(scope="module")
def looper(unstartedLooper):
    unstartedLooper.autoStart = True
    unstartedLooper.startall()
    return unstartedLooper


@pytest.fixture(scope="module")
def pool(tmpdir_factory):
    return Pool(tmpdir_factory)


@pytest.fixture(scope="module")
def ready(looper, keySharedNodes):
    looper.run(checkNodesConnected(keySharedNodes))
    return keySharedNodes


@pytest.fixture(scope="module")
def up(looper, ready):
    ensureElectionsDone(looper=looper, nodes=ready, retryWait=1, timeout=30)


# noinspection PyIncorrectDocstring
@pytest.fixture(scope="module")
def ensureView(nodeSet, looper, up):
    """
    Ensure that all the nodes in the nodeSet are in the same view.
    """
    return looper.run(eventually(checkViewNoForNodes, nodeSet, timeout=3))


@pytest.fixture("module")
def delayedPerf(nodeSet):
    for node in nodeSet:
        node.delayCheckPerformance(20)


@pytest.fixture(scope="module")
def clientAndWallet1(looper, nodeSet, tdir, up):
    return genTestClient(nodeSet, tmpdir=tdir)


@pytest.fixture(scope="module")
def client1(clientAndWallet1, looper):
    client, _ = clientAndWallet1
    looper.add(client)
    looper.run(client.ensureConnectedToNodes())
    return client


@pytest.fixture(scope="module")
def wallet1(clientAndWallet1):
    _, wallet = clientAndWallet1
    return wallet


@pytest.fixture(scope="module")
def request1(wallet1):
    op = randomOperation()
    req = wallet1.signOp(op)
    return req


@pytest.fixture(scope="module")
def sent1(client1, request1):
    return client1.submitReqs(request1)[0]


@pytest.fixture(scope="module")
def reqAcked1(looper, nodeSet, client1, sent1, faultyNodes):
    coros = [partial(checkLastClientReqForNode, node, sent1)
             for node in nodeSet]
    looper.run(eventuallyAll(*coros,
                             totalTimeout=10,
                             acceptableFails=faultyNodes))

    coros2 = [partial(checkReqAck, client1, node, sent1.identifier, sent1.reqId)
              for node in nodeSet]
    looper.run(eventuallyAll(*coros2,
                             totalTimeout=5,
                             acceptableFails=faultyNodes))

    return sent1


@pytest.fixture(scope="module")
def noRetryReq(conf, tdir, request):
    oldRetryAck = conf.CLIENT_MAX_RETRY_ACK
    oldRetryReply = conf.CLIENT_MAX_RETRY_REPLY
    conf.baseDir = tdir
    conf.CLIENT_MAX_RETRY_ACK = 0
    conf.CLIENT_MAX_RETRY_REPLY = 0

    def reset():
        conf.CLIENT_MAX_RETRY_ACK = oldRetryAck
        conf.CLIENT_MAX_RETRY_REPLY = oldRetryReply

    request.addfinalizer(reset)
    return conf


@pytest.fixture(scope="module")
def faultyNodes(request):
    return getValueFromModule(request, "faultyNodes", 0)


@pytest.fixture(scope="module")
def propagated1(looper,
                nodeSet,
                up,
                reqAcked1,
                faultyNodes):
    checkPropagated(looper, nodeSet, reqAcked1, faultyNodes)
    return reqAcked1


@pytest.fixture(scope="module")
def preprepared1(looper, nodeSet, propagated1, faultyNodes):
    checkPrePrepared(looper,
                     nodeSet,
                     propagated1,
                     range(getNoInstances(len(nodeSet))),
                     faultyNodes)
    return propagated1


@pytest.fixture(scope="module")
def prepared1(looper, nodeSet, client1, preprepared1, faultyNodes):
    checkPrepared(looper,
                  nodeSet,
                  preprepared1,
                  range(getNoInstances(len(nodeSet))),
                  faultyNodes)
    return preprepared1


@pytest.fixture(scope="module")
def committed1(looper, nodeSet, client1, prepared1, faultyNodes):
    checkCommitted(looper,
                   nodeSet,
                   prepared1,
                   range(getNoInstances(len(nodeSet))),
                   faultyNodes)
    return prepared1


@pytest.fixture(scope="module")
def replied1(looper, nodeSet, client1, committed1, wallet1, faultyNodes):
    def checkOrderedCount():
        instances = getNoInstances(len(nodeSet))
        resp = [requestReturnedToNode(node, wallet1.defaultId,
                                      committed1.reqId, instId) for
                node in nodeSet for instId in range(instances)]
        assert resp.count(True) >= (len(nodeSet) - faultyNodes)*instances

    looper.run(eventually(checkOrderedCount, retryWait=1, timeout=30))
    looper.run(eventually(
        checkSufficientRepliesRecvd,
        client1.inBox,
        committed1.reqId,
        getMaxFailures(len(nodeSet)),
        retryWait=2,
        timeout=30))
    return committed1


@pytest.yield_fixture(scope="module")
def looperWithoutNodeSet():
    with Looper(debug=True) as looper:
        yield looper


@pytest.fixture(scope="module")
def poolTxnNodeNames(index=""):
    return [n + index for n in ("Alpha", "Beta", "Gamma", "Delta")]


@pytest.fixture(scope="module")
def poolTxnClientNames():
    return "Alice", "Jason", "John", "Les"


@pytest.fixture(scope="module")
def poolTxnStewardNames():
    return "Steward1", "Steward2", "Steward3", "Steward4"


@pytest.fixture(scope="module")
def conf(tdir):
    return getConfig(tdir)


# TODO: This fixture is probably not needed now, as getConfig takes the
# `baseDir`. Confirm and remove
@pytest.fixture(scope="module")
def tconf(conf, tdir):
    conf.baseDir = tdir
    return conf


@pytest.fixture(scope="module")
def dirName():
    return os.path.dirname


@pytest.fixture(scope="module")
def nodeAndClientInfoFilePath(dirName):
    return os.path.join(dirName(__file__), "node_and_client_info.py")


@pytest.fixture(scope="module")
def poolTxnData(nodeAndClientInfoFilePath):
    with open(nodeAndClientInfoFilePath) as f:
        data = json.loads(f.read().strip())
        for txn in data["txns"]:
            if txn[TXN_TYPE] == NODE:
                txn[DATA][NODE_PORT] = genHa()[1]
                txn[DATA][CLIENT_PORT] = genHa()[1]
        return data


@pytest.fixture(scope="module")
def tdirWithPoolTxns(poolTxnData, tdir, tconf):
    import getpass
    logging.debug("current user when creating new pool txn file: {}".
                  format(getpass.getuser()))
    ledger = Ledger(CompactMerkleTree(),
                    dataDir=tdir,
                    fileName=tconf.poolTransactionsFile)
    for item in poolTxnData["txns"]:
        if item.get(TXN_TYPE) == NODE:
            ledger.add(item)
    ledger.stop()
    return tdir


@pytest.fixture(scope="module")
def domainTxnOrderedFields():
    return getTxnOrderedFields()


@pytest.fixture(scope="module")
def tdirWithDomainTxns(poolTxnData, tdir, tconf, domainTxnOrderedFields):
    ledger = Ledger(CompactMerkleTree(),
                    dataDir=tdir,
                    serializer=CompactSerializer(fields=domainTxnOrderedFields),
                    fileName=tconf.domainTransactionsFile)
    for item in poolTxnData["txns"]:
        if item.get(TXN_TYPE) == NYM:
            ledger.add(item)
    ledger.stop()
    return tdir


@pytest.fixture(scope="module")
def tdirWithNodeKeepInited(tdir, poolTxnData, poolTxnNodeNames):
    seeds = poolTxnData["seeds"]
    for nName in poolTxnNodeNames:
        seed = seeds[nName]
        initNodeKeysForBothStacks(nName, tdir, seed, override=True)


@pytest.fixture(scope="module")
def poolTxnClientData(poolTxnClientNames, poolTxnData):
    name = poolTxnClientNames[0]
    seed = poolTxnData["seeds"][name]
    return name, seed.encode()


@pytest.fixture(scope="module")
def poolTxnStewardData(poolTxnStewardNames, poolTxnData):
    name = poolTxnStewardNames[0]
    seed = poolTxnData["seeds"][name]
    return name, seed.encode()


@pytest.fixture(scope="module")
def poolTxnClient(tdirWithPoolTxns, tdirWithDomainTxns, txnPoolNodeSet):
    return genTestClient(txnPoolNodeSet, tmpdir=tdirWithPoolTxns,
                         usePoolLedger=True)


@pytest.fixture(scope="module")
def testNodeClass(patchPluginManager):
    return TestNode


@pytest.fixture(scope="module")
def testClientClass():
    return TestClient


@pytest.yield_fixture(scope="module")
def txnPoolNodesLooper():
    with Looper(debug=True) as l:
        yield l


@pytest.fixture(scope="module")
def txnPoolNodeSet(patchPluginManager,
                   txnPoolNodesLooper,
                   tdirWithPoolTxns,
                   tdirWithDomainTxns,
                   tconf,
                   poolTxnNodeNames,
                   allPluginsPath,
                   tdirWithNodeKeepInited,
                   testNodeClass):
    nodes = []
    for nm in poolTxnNodeNames:
        node = testNodeClass(nm, basedirpath=tdirWithPoolTxns,
                             config=tconf, pluginPaths=allPluginsPath)
        txnPoolNodesLooper.add(node)
        nodes.append(node)
    txnPoolNodesLooper.run(checkNodesConnected(nodes))
    ensureElectionsDone(looper=txnPoolNodesLooper, nodes=nodes, retryWait=1,
                        timeout=20)
    return nodes


@pytest.fixture(scope="module")
def txnPoolCliNodeReg(poolTxnData):
    cliNodeReg = {}
    for txn in poolTxnData["txns"]:
        if txn[TXN_TYPE] == NODE:
            data = txn[DATA]
            cliNodeReg[data[ALIAS] + CLIENT_STACK_SUFFIX] = HA(data[CLIENT_IP],
                                                               data[CLIENT_PORT])
    return cliNodeReg


@pytest.fixture(scope="module")
def postingStatsEnabled(request):
    config = getConfig()
    config.SendMonitorStats = True

    # def reset():
    #    config.SendMonitorStats = False

    # request.addfinalizer(reset)


@pytest.fixture
def pluginManager(monkeypatch):
    pluginManager = PluginManager()
    monkeypatch.setattr(importlib, 'import_module', mockImportModule)
    packagesCnt = 3
    packages = [pluginManager.prefix + randomText(10)
                for _ in range(packagesCnt)]
    monkeypatch.setattr(pip.utils, 'get_installed_distributions',
                        partial(mockGetInstalledDistributions,
                                packages=packages))
    imported, found = pluginManager.importPlugins()
    assert imported == 3
    assert hasattr(pluginManager, 'prefix')
    assert hasattr(pluginManager, '_sendMessage')
    assert hasattr(pluginManager, '_findPlugins')
    yield pluginManager
    monkeypatch.undo()


@pytest.fixture(scope="module")
def patchPluginManager():
    pluginManager = PluginManager()
    pluginManager.plugins = []
    return pluginManager


@pytest.fixture
def pluginManagerWithImportedModules(pluginManager, monkeypatch):
    monkeypatch.setattr(pip.utils, 'get_installed_distributions',
                        partial(mockGetInstalledDistributions,
                                packages=[]))
    monkeypatch.setattr(importlib, 'import_module', mockImportModule)
    imported, found = pluginManager.importPlugins()
    assert imported == 0
    packagesCnt = 3
    packages = [pluginManager.prefix + randomText(10)
                for _ in range(packagesCnt)]
    monkeypatch.setattr(pip.utils, 'get_installed_distributions',
                        partial(mockGetInstalledDistributions,
                                packages=packages))
    imported, found = pluginManager.importPlugins()
    assert imported == 3
    yield pluginManager
    monkeypatch.undo()
    pluginManager.importPlugins()


@pytest.fixture
def testNode(pluginManager, tdir):
    name = randomText(20)
    nodeReg = genNodeReg(names=[name])
    ha, cliname, cliha = nodeReg[name]
    return TestNode(name=name, ha=ha, cliname=cliname, cliha=cliha,
                    nodeRegistry=copy(nodeReg), basedirpath=tdir,
                    primaryDecider=None, pluginPaths=None, seed=randomSeed())
