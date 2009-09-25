##############################################################################
#
# Copyright (c) 2008 Zope Foundation and Contributors.
# All Rights Reserved.
#
# This software is subject to the provisions of the Zope Public License,
# Version 2.1 (ZPL).  A copy of the ZPL should accompany this distribution.
# THIS SOFTWARE IS PROVIDED "AS IS" AND ANY AND ALL EXPRESS OR IMPLIED
# WARRANTIES ARE DISCLAIMED, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF TITLE, MERCHANTABILITY, AGAINST INFRINGEMENT, AND FITNESS
# FOR A PARTICULAR PURPOSE.
#
##############################################################################
"""A foundation for RelStorage tests"""

from persistent import Persistent
from persistent.mapping import PersistentMapping
from relstorage.tests import fakecache
from ZODB.DB import DB
from ZODB.serialize import referencesf
from ZODB.tests import BasicStorage
from ZODB.tests import ConflictResolution
from ZODB.tests import MTStorage
from ZODB.tests import PackableStorage
from ZODB.tests import PersistentStorage
from ZODB.tests import ReadOnlyStorage
from ZODB.tests import StorageTestBase
from ZODB.tests import Synchronization
from ZODB.tests.MinPO import MinPO
from ZODB.tests.StorageTestBase import zodb_pickle
from ZODB.tests.StorageTestBase import zodb_unpickle
from ZODB.utils import p64
import time
import transaction


class RelStorageTestBase(StorageTestBase.StorageTestBase):

    def make_adapter(self):
        # abstract method
        raise NotImplementedError

    def open(self, **kwargs):
        from relstorage.storage import RelStorage
        adapter = self.make_adapter()
        self._storage = RelStorage(adapter, **kwargs)

    def setUp(self):
        self.open(create=1)
        self._storage.zap_all()

    def tearDown(self):
        transaction.abort()
        self._storage.close()
        self._storage.cleanup()


class GenericRelStorageTests(
    RelStorageTestBase,
    BasicStorage.BasicStorage,
    PackableStorage.PackableStorage,
    Synchronization.SynchronizedStorage,
    ConflictResolution.ConflictResolvingStorage,
    PersistentStorage.PersistentStorage,
    MTStorage.MTStorage,
    ReadOnlyStorage.ReadOnlyStorage,
    ):

    def checkDropAndPrepare(self):
        self._storage._adapter.schema.drop_all()
        self._storage._adapter.schema.prepare()

    def checkCrossConnectionInvalidation(self):
        # Verify connections see updated state at txn boundaries
        db = DB(self._storage)
        try:
            c1 = db.open()
            r1 = c1.root()
            r1['myobj'] = 'yes'
            c2 = db.open()
            r2 = c2.root()
            self.assert_('myobj' not in r2)

            storage = c1._storage
            t = transaction.Transaction()
            t.description = 'invalidation test'
            storage.tpc_begin(t)
            c1.commit(t)
            storage.tpc_vote(t)
            storage.tpc_finish(t)

            self.assert_('myobj' not in r2)
            c2.sync()
            self.assert_('myobj' in r2)
            self.assert_(r2['myobj'] == 'yes')
        finally:
            db.close()

    def checkCrossConnectionIsolation(self):
        # Verify MVCC isolates connections
        db = DB(self._storage)
        try:
            c1 = db.open()
            r1 = c1.root()
            r1['alpha'] = PersistentMapping()
            r1['gamma'] = PersistentMapping()
            transaction.commit()

            # Open a second connection but don't load root['alpha'] yet
            c2 = db.open()
            r2 = c2.root()

            r1['alpha']['beta'] = 'yes'

            storage = c1._storage
            t = transaction.Transaction()
            t.description = 'isolation test 1'
            storage.tpc_begin(t)
            c1.commit(t)
            storage.tpc_vote(t)
            storage.tpc_finish(t)

            # The second connection will now load root['alpha'], but due to
            # MVCC, it should continue to see the old state.
            self.assert_(r2['alpha']._p_changed is None)  # A ghost
            self.assert_(not r2['alpha'])
            self.assert_(r2['alpha']._p_changed == 0)

            # make root['alpha'] visible to the second connection
            c2.sync()

            # Now it should be in sync
            self.assert_(r2['alpha']._p_changed is None)  # A ghost
            self.assert_(r2['alpha'])
            self.assert_(r2['alpha']._p_changed == 0)
            self.assert_(r2['alpha']['beta'] == 'yes')

            # Repeat the test with root['gamma']
            r1['gamma']['delta'] = 'yes'

            storage = c1._storage
            t = transaction.Transaction()
            t.description = 'isolation test 2'
            storage.tpc_begin(t)
            c1.commit(t)
            storage.tpc_vote(t)
            storage.tpc_finish(t)

            # The second connection will now load root[3], but due to MVCC,
            # it should continue to see the old state.
            self.assert_(r2['gamma']._p_changed is None)  # A ghost
            self.assert_(not r2['gamma'])
            self.assert_(r2['gamma']._p_changed == 0)

            # make root[3] visible to the second connection
            c2.sync()

            # Now it should be in sync
            self.assert_(r2['gamma']._p_changed is None)  # A ghost
            self.assert_(r2['gamma'])
            self.assert_(r2['gamma']._p_changed == 0)
            self.assert_(r2['gamma']['delta'] == 'yes')
        finally:
            db.close()

    def checkResolveConflictBetweenConnections(self):
        # Verify that conflict resolution works between storage instances
        # bound to connections.
        obj = ConflictResolution.PCounter()
        obj.inc()

        oid = self._storage.new_oid()

        revid1 = self._dostoreNP(oid, data=zodb_pickle(obj))

        storage1 = self._storage.bind_connection(None)
        storage1.load(oid, '')
        storage2 = self._storage.bind_connection(None)
        storage2.load(oid, '')

        obj.inc()
        obj.inc()
        # The effect of committing two transactions with the same
        # pickle is to commit two different transactions relative to
        # revid1 that add two to _value.
        root_storage = self._storage
        try:
            self._storage = storage1
            revid2 = self._dostoreNP(oid, revid=revid1, data=zodb_pickle(obj))
            self._storage = storage2
            revid3 = self._dostoreNP(oid, revid=revid1, data=zodb_pickle(obj))

            data, serialno = self._storage.load(oid, '')
            inst = zodb_unpickle(data)
            self.assertEqual(inst._value, 5)
        finally:
            self._storage = root_storage

    def check16KObject(self):
        # Store 16 * 1024 bytes in an object, then retrieve it
        data = 'a 16 byte string' * 1024
        oid = self._storage.new_oid()
        self._dostoreNP(oid, data=data)
        got, serialno = self._storage.load(oid, '')
        self.assertEqual(len(got), len(data))
        self.assertEqual(got, data)

    def checkPreventOIDOverlap(self):
        # Store an object with a particular OID, then verify that
        # OID is not reused.
        data = 'mydata'
        oid1 = '\0' * 7 + '\x0f'
        self._dostoreNP(oid1, data=data)
        oid2 = self._storage.new_oid()
        self.assert_(oid1 < oid2, 'old OID %r should be less than new OID %r'
            % (oid1, oid2))

    def check16MObject(self):
        # Store 16 * 1024 * 1024 bytes in an object, then retrieve it
        data = 'a 16 byte string' * (1024 * 1024)
        oid = self._storage.new_oid()
        self._dostoreNP(oid, data=data)
        got, serialno = self._storage.load(oid, '')
        self.assertEqual(len(got), len(data))
        self.assertEqual(got, data)

    def checkLoadFromCache(self):
        # Store an object, cache it, then retrieve it from the cache
        self._storage._options.cache_servers = 'x:1 y:2'
        self._storage._options.cache_module_name = fakecache.__name__

        db = DB(self._storage)
        try:
            c1 = db.open()
            self.assertEqual(c1._storage._cache_client.servers, ['x:1', 'y:2'])
            fakecache.data.clear()
            r1 = c1.root()
            # the root tid and state should now be cached
            self.assertEqual(len(fakecache.data), 2)
            r1['alpha'] = PersistentMapping()
            self.assertFalse('commit_count' in fakecache.data)
            transaction.commit()
            self.assertTrue('commit_count' in fakecache.data)
            self.assertEqual(len(fakecache.data), 3)
            oid = r1['alpha']._p_oid
            self.assertEqual(len(fakecache.data), 3)

            got, serial = c1._storage.load(oid, '')
            # another tid and state should now be cached
            self.assertEqual(len(fakecache.data), 5)

            # load the object via loadSerial()
            got2 = c1._storage.loadSerial(oid, serial)
            self.assertEqual(got, got2)

            # try to load an object that doesn't exist
            self.assertRaises(KeyError, c1._storage.load, 'bad.oid.', '')
        finally:
            db.close()

    def checkMultipleStores(self):
        # Verify a connection can commit multiple transactions
        db = DB(self._storage)
        try:
            c1 = db.open()
            r1 = c1.root()
            r1['alpha'] = 1
            transaction.commit()
            r1['alpha'] = 2
            transaction.commit()
        finally:
            db.close()

    def checkLongTransactionDescription(self):
        # Don't trip over long transaction descriptions
        db = DB(self._storage)
        try:
            c = db.open()
            r = c.root()
            r['key'] = 1
            transaction.get().note('A long description. ' * 1000)
            transaction.commit()
        finally:
            db.close()

    def checkAutoReconnect(self):
        # Verify auto-reconnect
        db = DB(self._storage)
        try:
            c1 = db.open()
            r = c1.root()
            r['alpha'] = 1
            transaction.commit()
            c1.close()

            c1._storage._load_conn.close()
            c1._storage._store_conn.close()

            c2 = db.open()
            self.assert_(c2 is c1)
            r = c2.root()
            self.assertEqual(r['alpha'], 1)
            r['beta'] = 2
            transaction.commit()
            c2.close()
        finally:
            db.close()

    def checkPollInterval(self, using_cache=False):
        # Verify the poll_interval parameter causes RelStorage to
        # delay invalidation polling.
        self._storage._options.poll_interval = 3600
        db = DB(self._storage)
        try:
            c1 = db.open()
            r1 = c1.root()
            r1['alpha'] = 1
            transaction.commit()

            c2 = db.open()
            r2 = c2.root()
            self.assertEqual(r2['alpha'], 1)

            r1['alpha'] = 2
            # commit c1 without triggering c2.afterCompletion().
            storage = c1._storage
            t = transaction.Transaction()
            storage.tpc_begin(t)
            c1.commit(t)
            storage.tpc_vote(t)
            storage.tpc_finish(t)

            # flush invalidations to c2, but the poll timer has not
            # yet expired, so the change to r2 should not be seen yet.
            self.assertTrue(c2._storage._poll_at > 0)
            if using_cache:
                # The cache reveals that a poll is needed even though
                # the poll timeout has not expired.
                self.assertTrue(c2._storage.need_poll())
                c2._flush_invalidations()
                r2 = c2.root()
                self.assertEqual(r2['alpha'], 2)
                self.assertFalse(c2._storage.need_poll())
            else:
                self.assertFalse(c2._storage.need_poll())
                c2._flush_invalidations()
                r2 = c2.root()
                self.assertEqual(r2['alpha'], 1)

            # expire the poll timer and verify c2 sees the change
            c2._storage._poll_at -= 3601
            c2._flush_invalidations()
            r2 = c2.root()
            self.assertEqual(r2['alpha'], 2)

            transaction.abort()
            c2.close()
            c1.close()

        finally:
            db.close()

    def checkPollIntervalWithCache(self):
        self._storage._options.cache_servers = 'x:1'
        self._storage._options.cache_module_name = fakecache.__name__
        fakecache.data.clear()
        self.checkPollInterval(using_cache=True)

    def checkDoubleCommitter(self):
        # Verify we can store an object that gets committed twice in
        # a single transaction.
        db = DB(self._storage)
        try:
            conn = db.open()
            try:
                conn.root()['dc'] = DoubleCommitter()
                transaction.commit()
                conn2 = db.open()
                self.assertEquals(conn2.root()['dc'].new_attribute, 1)
                conn2.close()
            finally:
                transaction.abort()
                conn.close()
        finally:
            db.close()

    def checkPackDutyCycle(self):
        # Exercise the code in the pack algorithm that releases the
        # commit lock for a time to allow concurrent transactions to commit.
        self._storage._options.pack_batch_timeout = 0  # pause after every txn

        slept = []
        def sim_sleep(seconds):
            slept.append(seconds)

        db = DB(self._storage)
        try:
            # add some data to be packed
            c = db.open()
            r = c.root()
            r['alpha'] = PersistentMapping()
            transaction.commit()
            del r['alpha']
            transaction.commit()

            # Pack
            now = packtime = time.time()
            while packtime <= now:
                packtime = time.time()
            self._storage.pack(packtime, referencesf, sleep=sim_sleep)

            self.assertTrue(len(slept) > 0)
        finally:
            db.close()

    def checkPackBrokenPickle(self):
        # Verify the pack stops with the right exception if it encounters
        # a broken pickle.
        from cPickle import UnpicklingError
        self._dostoreNP(self._storage.new_oid(), data='brokenpickle')
        self.assertRaises(UnpicklingError, self._storage.pack,
            time.time() + 10000, referencesf)


class DoubleCommitter(Persistent):
    """A crazy persistent class that changes self in __getstate__"""
    def __getstate__(self):
        if not hasattr(self, 'new_attribute'):
            self.new_attribute = 1
        return Persistent.__getstate__(self)
