import pytest

from stp_core.loop.eventually import eventually, slowFactor
from stp_core.common.log import getlogger
from stp_core.loop.looper import Looper
from plenum.server.node import Node
from plenum.test.delayers import delayerMsgTuple
from plenum.test.helper import sendMsgAndCheck, addNodeBack, assertExp, sendMsg, \
    checkMsg
from plenum.test.msgs import randomMsg
from plenum.test.test_node import TestNodeSet, checkNodesConnected, \
    ensureElectionsDone, prepareNodeSet

logger = getlogger()


@pytest.mark.skipif('sys.platform == "win32"', reason='SOV-457')
def testTestNodeDelay(tdir_for_func):
    nodeNames = {"testA", "testB"}
    with TestNodeSet(names=nodeNames, tmpdir=tdir_for_func) as nodes:
        nodeA = nodes.getNode("testA")
        nodeB = nodes.getNode("testB")

        with Looper(nodes) as looper:
            # for n in nodes:
            #     n.startKeySharing()

            logger.debug("connect")
            looper.run(checkNodesConnected(nodes))
            logger.debug("send one message, without delay")
            msg = randomMsg()
            looper.run(sendMsgAndCheck(nodes, nodeA, nodeB, msg, 2))
            logger.debug("set delay, then send another message and find that "
                          "it doesn't arrive")
            msg = randomMsg()

            nodeB.nodeIbStasher.delay(delayerMsgTuple(10 * slowFactor, type(msg), nodeA.name))

            sendMsg(nodes, nodeA, nodeB, msg)
            with pytest.raises(AssertionError):
                looper.run(eventually(checkMsg, msg, nodes, nodeB,
                                         retryWait=.1, timeout=6))
            logger.debug("but then find that it arrives after the delay "
                         "duration has passed")
            looper.run(eventually(checkMsg, msg, nodes, nodeB,
                                     retryWait=.1, timeout=6))
            logger.debug(
                    "reset the delay, and find another message comes quickly")
            nodeB.nodeIbStasher.resetDelays()
            msg = randomMsg()
            looper.run(sendMsgAndCheck(nodes, nodeA, nodeB, msg, 2))


def testSelfNominationDelay(tdir_for_func):
    nodeNames = ["testA", "testB", "testC", "testD"]
    with TestNodeSet(names=nodeNames, tmpdir=tdir_for_func) as nodeSet:
        with Looper(nodeSet) as looper:
            prepareNodeSet(looper, nodeSet)

            delay = 30
            # Add node A
            nodeA = addNodeBack(nodeSet, looper, nodeNames[0])
            nodeA.delaySelfNomination(delay)

            nodesBCD = []
            for name in nodeNames[1:]:
                # nodesBCD.append(nodeSet.addNode(name, i+1, AutoMode.never))
                nodesBCD.append(addNodeBack(nodeSet, looper, name))

            # Ensuring that NodeA is started before any other node to demonstrate
            # that it is delaying self nomination
            looper.run(
                    eventually(lambda: assertExp(nodeA.isReady()), retryWait=1,
                               timeout=5))

            # Elections should be done
            ensureElectionsDone(looper=looper, nodes=nodeSet, retryWait=1,
                                timeout=10)

            # node A should not have any primary replica
            looper.run(
                    eventually(lambda: assertExp(not nodeA.hasPrimary),
                               retryWait=1,
                               timeout=10))

            # Make sure that after at the most 30 seconds, nodeA's
            # `startElection` is called
            looper.run(eventually(lambda: assertExp(
                    len(nodeA.spylog.getAll(
                            Node.decidePrimaries.__name__)) > 0),
                                  retryWait=1, timeout=30))
