# This file is named twisted_t.py to avoid it being found by nosetests,
# which hangs on some Twisted test cases. Use trial <module>.
from twisted.trial import unittest
from twisted.internet import defer, interfaces, reactor, protocol, task
from twisted.application.service import Application
from zope.interface import implementer

from sio.sioworkersd import manager, db, taskmanager, server
from sio.sioworkersd.scheduler.fifo import FIFOScheduler
from sio.protocol import rpc
import shutil
import tempfile

# debug
def _print(x):
    print x
    return x


class TestWithDB(unittest.TestCase):
    """Abstract class for testing sioworkersd parts that need a database."""
    SAVED_TASKS = []

    def __init__(self, *args):
        super(TestWithDB, self).__init__(*args)
        self.app = None
        self.db = None
        self.workerm = None
        self.sched = None
        self.taskm = None

    def setUp(self):
        self.db_dir = tempfile.mkdtemp()
        self.db_path = self.db_dir + '/sio_tests.sqlite'

    def tearDown(self):
        if self.db:
            self.db.stopService()
        shutil.rmtree(self.db_dir)

    def _prepare_svc(self):
        self.app = Application('test')
        self.db = db.DBWrapper(self.db_path)
        self.db.setServiceParent(self.app)
        d = self.db.openDB()
        # HACK: we need to run openDB manually earlier to insert test values
        # but it can't be called a second time in startService
        self.db.openDB = lambda: None

        @defer.inlineCallbacks
        def db_callback(_):
            for tid, env in self.SAVED_TASKS:
                yield self.db.runOperation(
                    "insert into task (id, env) values (?, ?)",
                        (tid, env))
            self.workerm = manager.WorkerManager(self.db)
            self.workerm.setServiceParent(self.db)
            self.sched = FIFOScheduler(self.workerm)
            self.taskm = taskmanager.TaskManager(self.db, self.workerm,
                    self.sched)
            self.taskm.setServiceParent(self.db)
            yield self.db.startService()
        d.addCallback(db_callback)
        return d

class TaskManagerTest(TestWithDB):
    SAVED_TASKS = [
            ('asdf', '{"task_id": "asdf"}')
            ]

    def test_restore(self):
        d = self._prepare_svc()
        d.addCallback(lambda _:
                self.assertIn('asdf', self.taskm.inProgress))
        d.addCallback(lambda _:
                self.assertDictEqual(self.taskm.inProgress['asdf'].env,
                            {'task_id': 'asdf'}))
        return d


@implementer(interfaces.ITransport)
class MockTransport(object):
    def __init__(self):
        self.connected = True

    def loseConnection(self):
        self.connected = False


class TestWorker(server.WorkerServer):
    def __init__(self, clientInfo=None):
        server.WorkerServer.__init__(self)
        self.wm = None
        self.transport = MockTransport()
        self.running = []
        if not clientInfo:
            self.name = 'test_worker'
            self.clientInfo = {'name': self.name, 'concurrency': 2}
        else:
            self.name = clientInfo['name']
            self.clientInfo = clientInfo

    def call(self, method, *a, **kw):
        if method == 'run':
            env = a[0]
            if env['task_id'].startswith('ok'):
                env['foo'] = 'bar'
                return defer.succeed(env)
            elif env['task_id'] == 'fail':
                return defer.fail(rpc.RemoteError('test'))
            elif env['task_id'].startswith('hang'):
                return defer.Deferred()
        elif method == 'get_running':
            return self.running


class WorkerManagerTest(TestWithDB):
    def __init__(self, *args, **kwargs):
        super(WorkerManagerTest, self).__init__(*args, **kwargs)
        self.notifyCalled = False
        self.wm = None
        self.worker_proto = None

    def _notify_cb(self, _):
        self.notifyCalled = True

    def setUp2(self, _=None):
        self.wm = manager.WorkerManager(self.db)
        self.wm.notifyOnNewWorker(self._notify_cb)
        self.worker_proto = TestWorker()
        return self.wm.newWorker('unique1', self.worker_proto)

    def setUp(self):
        super(WorkerManagerTest, self).setUp()
        d = self._prepare_svc()
        d.addCallback(self.setUp2)
        return d

    def test_notify(self):
        self.assertTrue(self.notifyCalled)

    @defer.inlineCallbacks
    def test_run(self):
        yield self.assertIn('test_worker', self.wm.workers)
        ret = yield self.wm.runOnWorker('test_worker', {'task_id': 'ok'})
        yield self.assertIn('foo', ret)
        yield self.assertEqual('bar', ret['foo'])

    def test_fail(self):
        d = self.wm.runOnWorker('test_worker', {'task_id': 'fail'})
        d.addBoth(_print)
        return self.assertFailure(d, rpc.RemoteError)

    def test_exclusive(self):
        self.wm.runOnWorker('test_worker', {'task_id': 'hang1'})
        self.assertRaises(RuntimeError,
                self.wm.runOnWorker, 'test_worker', {'task_id': 'hang2'})

    def test_exclusive2(self):
        self.wm.runOnWorker('test_worker',
                {'task_id': 'hang1', 'exclusive': False})
        self.assertRaises(RuntimeError,
                self.wm.runOnWorker, 'test_worker', {'task_id': 'hang2'})

    def test_gone(self):
        d = self.wm.runOnWorker('test_worker', {'task_id': 'hang'})
        self.wm.workerLost(self.worker_proto)
        return self.assertFailure(d, manager.WorkerGone)

    def test_duplicate(self):
        w2 = TestWorker()
        d = self.wm.newWorker('unique2', w2)
        self.assertFalse(w2.transport.connected)
        return self.assertFailure(d, server.DuplicateWorker)

    def test_rejected(self):
        w2 = TestWorker()
        w2.running = ['asdf']
        w2.name = 'name2'
        d = self.wm.newWorker('unique2', w2)
        return self.assertFailure(d, server.WorkerRejected)

    def test_reject_incomplete_worker(self):
        w3 = TestWorker({'name': 'no_concurrency'})
        d = self.wm.newWorker('no_concurrency', w3)
        self.assertFailure(d, server.WorkerRejected)

        w4 = TestWorker({'name': 'unique4', 'concurrency': 'not a number'})
        d = self.wm.newWorker('unique4', w4)
        self.assertFailure(d, server.WorkerRejected)


class TestClient(rpc.WorkerRPC):
    def __init__(self, running):
        rpc.WorkerRPC.__init__(self, server=False)
        self.running = running

    def getHelloData(self):
        return {'name': 'test', 'concurrency': '1'}

    def cmd_get_running(self):
        return list(self.running)

    def do_run(self, env):
        if env['task_id'].startswith('hang'):
            return defer.Deferred()
        else:
            return defer.succeed(env)

    def cmd_run(self, env):
        self.running.add(env['task_id'])
        d = self.do_run(env)

        def _rm(x):
            self.running.remove(env['task_id'])
            return x
        d.addBoth(_rm)
        return d

class IntegrationTest(TestWithDB):
    def __init__(self, *args, **kwargs):
        super(IntegrationTest, self).__init__(*args, **kwargs)
        self.notifyCalled = False
        self.wm = None
        self.taskm = None
        self.port = None

    def setUp2(self, _=None):
        manager.TASK_TIMEOUT = 3
        self.wm = manager.WorkerManager(self.db)
        self.sched = FIFOScheduler(self.wm)
        self.taskm = taskmanager.TaskManager(self.db, self.wm, self.sched)

        factory = self.wm.makeFactory()
        self.port = reactor.listenTCP(0, factory, interface='127.0.0.1')
        self.addCleanup(self.port.stopListening)

    def setUp(self):
        super(IntegrationTest, self).setUp()
        d = self._prepare_svc()
        d.addCallback(self.setUp2)
        return d

    def _wrap_test(self, callback, *client_args):
        creator = protocol.ClientCreator(reactor, TestClient, *client_args)

        def cb(client):
            self.addCleanup(client.transport.loseConnection)
            # We have to wait for a few (local) network roundtrips, hence the
            # magic one-second delay.
            return task.deferLater(reactor, 1, callback, client)
        return creator.connectTCP('127.0.0.1', self.port.getHost().port).\
                addCallback(cb)

    def test_remote_run(self):
        def cb(client):
            self.assertIn('test', self.wm.workers)
            d = self.taskm.addTask({'task_id': 'asdf'})
            d.addCallback(lambda x: self.assertIn('task_id', x))
            return d
        return self._wrap_test(cb, set())

    def test_timeout(self):
        def cb2(_):
            self.assertEqual(self.wm.workers, {})

        def cb(client):
            d = self.taskm.addTask({'task_id': 'hang'})
            d = self.assertFailure(d, rpc.TimeoutError)
            d.addBoth(cb2)
            return d
        return self._wrap_test(cb, set())

    def test_gone(self):
        def cb3(client, d):
            self.assertFalse(d.called)
            self.assertDictEqual(self.wm.workers, {})
            self.assertListEqual(list(self.sched.queue), [('hang', True)])

        def cb2(client, d):
            client.transport.loseConnection()
            # Wait for the connection to drop
            return task.deferLater(reactor, 1, cb3, client, d)

        def cb(client):
            d = self.taskm.addTask({'task_id': 'hang'})
            # Allow the task to schedule
            return task.deferLater(reactor, 0, cb2, client, d)
        return self._wrap_test(cb, set())
