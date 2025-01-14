import decimal
import gc
import itertools
import multiprocessing
import weakref

import sqlalchemy as sa
from sqlalchemy import ForeignKey
from sqlalchemy import Integer
from sqlalchemy import MetaData
from sqlalchemy import select
from sqlalchemy import String
from sqlalchemy import testing
from sqlalchemy import Unicode
from sqlalchemy import util
from sqlalchemy.orm import aliased
from sqlalchemy.orm import clear_mappers
from sqlalchemy.orm import create_session
from sqlalchemy.orm import mapper
from sqlalchemy.orm import relationship
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker
from sqlalchemy.orm import subqueryload
from sqlalchemy.orm.mapper import _mapper_registry
from sqlalchemy.orm.session import _sessions
from sqlalchemy.processors import to_decimal_processor_factory
from sqlalchemy.processors import to_unicode_processor_factory
from sqlalchemy.sql import column
from sqlalchemy.testing import engines
from sqlalchemy.testing import eq_
from sqlalchemy.testing import fixtures
from sqlalchemy.testing.schema import Column
from sqlalchemy.testing.schema import Table
from sqlalchemy.testing.util import gc_collect


class A(fixtures.ComparableEntity):
    pass


class B(fixtures.ComparableEntity):
    pass


class ASub(A):
    pass


def profile_memory(
    maxtimes=250, assert_no_sessions=True, get_num_objects=None
):
    def decorate(func):
        # run the test N times.  if length of gc.get_objects()
        # keeps growing, assert false

        def get_objects_skipping_sqlite_issue():
            # pysqlite keeps adding weakref objects which only
            # get reset after 220 iterations.  We'd like to keep these
            # tests under 50 iterations and ideally about ten, so
            # just filter them out so that we get a "flatline" more quickly.

            if testing.against("sqlite+pysqlite"):
                return [
                    o
                    for o in gc.get_objects()
                    if not isinstance(o, weakref.ref)
                ]
            else:
                return gc.get_objects()

        def profile(queue, func_args):
            # give testing.db a brand new pool and don't
            # touch the existing pool, since closing a socket
            # in the subprocess can affect the parent
            testing.db.pool = testing.db.pool.recreate()

            gc_collect()
            samples = []
            max_ = 0
            max_grew_for = 0
            success = False
            until_maxtimes = 0
            while True:
                if until_maxtimes >= maxtimes // 5:
                    break
                for x in range(5):
                    func(*func_args)
                    gc_collect()
                    samples.append(
                        get_num_objects()
                        if get_num_objects is not None
                        else len(get_objects_skipping_sqlite_issue())
                    )

                if assert_no_sessions:
                    assert len(_sessions) == 0

                # queue.put(('samples', samples))

                latest_max = max(samples[-5:])
                if latest_max > max_:
                    queue.put(
                        (
                            "status",
                            "Max grew from %s to %s, max has "
                            "grown for %s samples"
                            % (max_, latest_max, max_grew_for),
                        )
                    )
                    max_ = latest_max
                    max_grew_for += 1
                    until_maxtimes += 1
                    continue
                else:
                    queue.put(
                        (
                            "status",
                            "Max remained at %s, %s more attempts left"
                            % (max_, max_grew_for),
                        )
                    )
                    max_grew_for -= 1
                    if max_grew_for == 0:
                        success = True
                        break

            if not success:
                queue.put(
                    (
                        "result",
                        False,
                        "Ran for a total of %d times, memory kept "
                        "growing: %r" % (maxtimes, samples),
                    )
                )

            else:
                queue.put(("result", True, "success"))

        def run_in_process(*func_args):
            queue = multiprocessing.Queue()
            proc = multiprocessing.Process(
                target=profile, args=(queue, func_args)
            )
            proc.start()
            while True:
                row = queue.get()
                typ = row[0]
                if typ == "samples":
                    print("sample gc sizes:", row[1])
                elif typ == "status":
                    print(row[1])
                elif typ == "result":
                    break
                else:
                    assert False, "can't parse row"
            proc.join()
            assert row[1], row[2]

        return run_in_process

    return decorate


def assert_no_mappers():
    clear_mappers()
    gc_collect()
    assert len(_mapper_registry) == 0


class EnsureZeroed(fixtures.ORMTest):
    def setup(self):
        _sessions.clear()
        _mapper_registry.clear()
        self.engine = engines.testing_engine(options={"use_reaper": False})


class MemUsageTest(EnsureZeroed):
    __tags__ = ("memory_intensive",)
    __requires__ = ("cpython",)

    def test_type_compile(self):
        from sqlalchemy.dialects.sqlite.base import dialect as SQLiteDialect

        cast = sa.cast(column("x"), sa.Integer)

        @profile_memory()
        def go():
            dialect = SQLiteDialect()
            cast.compile(dialect=dialect)

        go()

    @testing.requires.cextensions
    def test_DecimalResultProcessor_init(self):
        @profile_memory()
        def go():
            to_decimal_processor_factory({}, 10)

        go()

    @testing.requires.cextensions
    def test_DecimalResultProcessor_process(self):
        @profile_memory()
        def go():
            to_decimal_processor_factory(decimal.Decimal, 10)(1.2)

        go()

    @testing.requires.cextensions
    def test_UnicodeResultProcessor_init(self):
        @profile_memory()
        def go():
            to_unicode_processor_factory("utf8")

        go()

    def test_ad_hoc_types(self):
        """test storage of bind processors, result processors
        in dialect-wide registry."""

        from sqlalchemy.dialects import mysql, postgresql, sqlite
        from sqlalchemy import types

        eng = engines.testing_engine()
        for args in (
            (types.Integer,),
            (types.String,),
            (types.PickleType,),
            (types.Enum, "a", "b", "c"),
            (sqlite.DATETIME,),
            (postgresql.ENUM, "a", "b", "c"),
            (types.Interval,),
            (postgresql.INTERVAL,),
            (mysql.VARCHAR,),
        ):

            @profile_memory()
            def go():
                type_ = args[0](*args[1:])
                bp = type_._cached_bind_processor(eng.dialect)
                rp = type_._cached_result_processor(eng.dialect, 0)
                bp, rp  # strong reference

            go()

        assert not eng.dialect._type_memos

    @testing.fails()
    def test_fixture_failure(self):
        class Foo(object):
            pass

        stuff = []

        @profile_memory(maxtimes=20)
        def go():
            stuff.extend(Foo() for i in range(100))

        go()


class MemUsageWBackendTest(EnsureZeroed):

    __tags__ = ("memory_intensive",)
    __requires__ = "cpython", "memory_process_intensive"
    __sparse_backend__ = True

    # ensure a pure growing test trips the assertion
    @testing.fails_if(lambda: True)
    def test_fixture(self):
        class Foo(object):
            pass

        x = []

        @profile_memory(maxtimes=10)
        def go():
            x[-1:] = [Foo(), Foo(), Foo(), Foo(), Foo(), Foo()]

        go()

    def test_session(self):
        metadata = MetaData(self.engine)

        table1 = Table(
            "mytable",
            metadata,
            Column(
                "col1",
                Integer,
                primary_key=True,
                test_needs_autoincrement=True,
            ),
            Column("col2", String(30)),
        )

        table2 = Table(
            "mytable2",
            metadata,
            Column(
                "col1",
                Integer,
                primary_key=True,
                test_needs_autoincrement=True,
            ),
            Column("col2", String(30)),
            Column("col3", Integer, ForeignKey("mytable.col1")),
        )

        metadata.create_all()

        m1 = mapper(
            A,
            table1,
            properties={
                "bs": relationship(
                    B, cascade="all, delete", order_by=table2.c.col1
                )
            },
        )
        m2 = mapper(B, table2)

        @profile_memory()
        def go():
            sess = create_session()
            a1 = A(col2="a1")
            a2 = A(col2="a2")
            a3 = A(col2="a3")
            a1.bs.append(B(col2="b1"))
            a1.bs.append(B(col2="b2"))
            a3.bs.append(B(col2="b3"))
            for x in [a1, a2, a3]:
                sess.add(x)
            sess.flush()
            sess.expunge_all()

            alist = sess.query(A).order_by(A.col1).all()
            eq_(
                [
                    A(col2="a1", bs=[B(col2="b1"), B(col2="b2")]),
                    A(col2="a2", bs=[]),
                    A(col2="a3", bs=[B(col2="b3")]),
                ],
                alist,
            )

            for a in alist:
                sess.delete(a)
            sess.flush()

        go()

        metadata.drop_all()
        del m1, m2
        assert_no_mappers()

    def test_sessionmaker(self):
        @profile_memory()
        def go():
            sessmaker = sessionmaker(bind=self.engine)
            sess = sessmaker()
            r = sess.execute(select([1]))
            r.close()
            sess.close()
            del sess
            del sessmaker

        go()

    @testing.emits_warning("Compiled statement cache for mapper.*")
    @testing.emits_warning("Compiled statement cache for lazy loader.*")
    @testing.crashes("sqlite", ":memory: connection not suitable here")
    def test_orm_many_engines(self):
        metadata = MetaData(self.engine)

        table1 = Table(
            "mytable",
            metadata,
            Column(
                "col1",
                Integer,
                primary_key=True,
                test_needs_autoincrement=True,
            ),
            Column("col2", String(30)),
        )

        table2 = Table(
            "mytable2",
            metadata,
            Column(
                "col1",
                Integer,
                primary_key=True,
                test_needs_autoincrement=True,
            ),
            Column("col2", String(30)),
            Column("col3", Integer, ForeignKey("mytable.col1")),
        )

        metadata.create_all()

        m1 = mapper(
            A,
            table1,
            properties={
                "bs": relationship(
                    B, cascade="all, delete", order_by=table2.c.col1
                )
            },
            _compiled_cache_size=50,
        )
        m2 = mapper(B, table2, _compiled_cache_size=50)

        @profile_memory()
        def go():
            engine = engines.testing_engine(
                options={
                    "logging_name": "FOO",
                    "pool_logging_name": "BAR",
                    "use_reaper": False,
                }
            )
            sess = create_session(bind=engine)

            a1 = A(col2="a1")
            a2 = A(col2="a2")
            a3 = A(col2="a3")
            a1.bs.append(B(col2="b1"))
            a1.bs.append(B(col2="b2"))
            a3.bs.append(B(col2="b3"))
            for x in [a1, a2, a3]:
                sess.add(x)
            sess.flush()
            sess.expunge_all()

            alist = sess.query(A).order_by(A.col1).all()
            eq_(
                [
                    A(col2="a1", bs=[B(col2="b1"), B(col2="b2")]),
                    A(col2="a2", bs=[]),
                    A(col2="a3", bs=[B(col2="b3")]),
                ],
                alist,
            )

            for a in alist:
                sess.delete(a)
            sess.flush()
            sess.close()
            engine.dispose()

        go()

        metadata.drop_all()
        del m1, m2
        assert_no_mappers()

    @testing.emits_warning("Compiled statement cache for.*")
    def test_many_updates(self):
        metadata = MetaData(self.engine)

        wide_table = Table(
            "t",
            metadata,
            Column(
                "id", Integer, primary_key=True, test_needs_autoincrement=True
            ),
            *[Column("col%d" % i, Integer) for i in range(10)]
        )

        class Wide(object):
            pass

        mapper(Wide, wide_table, _compiled_cache_size=10)

        metadata.create_all()
        session = create_session()
        w1 = Wide()
        session.add(w1)
        session.flush()
        session.close()
        del session
        counter = [1]

        @profile_memory()
        def go():
            session = create_session()
            w1 = session.query(Wide).first()
            x = counter[0]
            dec = 10
            while dec > 0:
                # trying to count in binary here,
                # works enough to trip the test case
                if pow(2, dec) < x:
                    setattr(w1, "col%d" % dec, counter[0])
                    x -= pow(2, dec)
                dec -= 1
            session.flush()
            session.close()
            counter[0] += 1

        try:
            go()
        finally:
            metadata.drop_all()

    @testing.requires.savepoints
    @testing.provide_metadata
    def test_savepoints(self):
        metadata = self.metadata

        some_table = Table(
            "t",
            metadata,
            Column(
                "id", Integer, primary_key=True, test_needs_autoincrement=True
            ),
        )

        class SomeClass(object):
            pass

        mapper(SomeClass, some_table)

        metadata.create_all()

        session = Session(testing.db)

        target_strings = (
            session.connection().dialect.identifier_preparer._strings
        )

        session.close()

        @profile_memory(
            assert_no_sessions=False,
            get_num_objects=lambda: len(target_strings),
        )
        def go():
            session = Session(testing.db)
            with session.transaction:

                sc = SomeClass()
                session.add(sc)
                with session.begin_nested():
                    session.query(SomeClass).first()

        go()

    @testing.crashes("mysql+cymysql", "blocking")
    def test_unicode_warnings(self):
        metadata = MetaData(self.engine)
        table1 = Table(
            "mytable",
            metadata,
            Column(
                "col1",
                Integer,
                primary_key=True,
                test_needs_autoincrement=True,
            ),
            Column("col2", Unicode(30)),
        )
        metadata.create_all()
        i = [1]

        # the times here is cranked way up so that we can see
        # pysqlite clearing out its internal buffer and allow
        # the test to pass
        @testing.emits_warning()
        @profile_memory()
        def go():

            # execute with a non-unicode object. a warning is emitted,
            # this warning shouldn't clog up memory.

            self.engine.execute(
                table1.select().where(table1.c.col2 == "foo%d" % i[0])
            )
            i[0] += 1

        try:
            go()
        finally:
            metadata.drop_all()

    def test_warnings_util(self):
        counter = itertools.count()
        import warnings

        warnings.filterwarnings("ignore", "memusage warning.*")

        @profile_memory()
        def go():
            util.warn_limited(
                "memusage warning, param1: %s, param2: %s",
                (next(counter), next(counter)),
            )

        go()

    def test_mapper_reset(self):
        metadata = MetaData(self.engine)

        table1 = Table(
            "mytable",
            metadata,
            Column(
                "col1",
                Integer,
                primary_key=True,
                test_needs_autoincrement=True,
            ),
            Column("col2", String(30)),
        )

        table2 = Table(
            "mytable2",
            metadata,
            Column(
                "col1",
                Integer,
                primary_key=True,
                test_needs_autoincrement=True,
            ),
            Column("col2", String(30)),
            Column("col3", Integer, ForeignKey("mytable.col1")),
        )

        @profile_memory()
        def go():
            mapper(
                A,
                table1,
                properties={"bs": relationship(B, order_by=table2.c.col1)},
            )
            mapper(B, table2)

            sess = create_session()
            a1 = A(col2="a1")
            a2 = A(col2="a2")
            a3 = A(col2="a3")
            a1.bs.append(B(col2="b1"))
            a1.bs.append(B(col2="b2"))
            a3.bs.append(B(col2="b3"))
            for x in [a1, a2, a3]:
                sess.add(x)
            sess.flush()
            sess.expunge_all()

            alist = sess.query(A).order_by(A.col1).all()
            eq_(
                [
                    A(col2="a1", bs=[B(col2="b1"), B(col2="b2")]),
                    A(col2="a2", bs=[]),
                    A(col2="a3", bs=[B(col2="b3")]),
                ],
                alist,
            )

            for a in alist:
                sess.delete(a)
            sess.flush()
            sess.close()
            clear_mappers()

        metadata.create_all()
        try:
            go()
        finally:
            metadata.drop_all()
        assert_no_mappers()

    def test_alias_pathing(self):
        metadata = MetaData(self.engine)

        a = Table(
            "a",
            metadata,
            Column(
                "id", Integer, primary_key=True, test_needs_autoincrement=True
            ),
            Column("bid", Integer, ForeignKey("b.id")),
            Column("type", String(30)),
        )

        asub = Table(
            "asub",
            metadata,
            Column("id", Integer, ForeignKey("a.id"), primary_key=True),
            Column("data", String(30)),
        )

        b = Table(
            "b",
            metadata,
            Column(
                "id", Integer, primary_key=True, test_needs_autoincrement=True
            ),
        )
        mapper(A, a, polymorphic_identity="a", polymorphic_on=a.c.type)
        mapper(ASub, asub, inherits=A, polymorphic_identity="asub")
        mapper(B, b, properties={"as_": relationship(A)})

        metadata.create_all()
        sess = Session()
        a1 = ASub(data="a1")
        a2 = ASub(data="a2")
        a3 = ASub(data="a3")
        b1 = B(as_=[a1, a2, a3])
        sess.add(b1)
        sess.commit()
        del sess

        # sqlite has a slow enough growth here
        # that we have to run it more times to see the
        # "dip" again
        @profile_memory(maxtimes=120)
        def go():
            sess = Session()
            sess.query(B).options(subqueryload(B.as_.of_type(ASub))).all()
            sess.close()

        try:
            go()
        finally:
            metadata.drop_all()
        clear_mappers()

    def test_path_registry(self):
        metadata = MetaData()
        a = Table(
            "a",
            metadata,
            Column("id", Integer, primary_key=True),
            Column("foo", Integer),
            Column("bar", Integer),
        )
        b = Table(
            "b",
            metadata,
            Column("id", Integer, primary_key=True),
            Column("a_id", ForeignKey("a.id")),
        )
        m1 = mapper(A, a, properties={"bs": relationship(B)})
        mapper(B, b)

        @profile_memory()
        def go():
            ma = sa.inspect(aliased(A))
            m1._path_registry[m1.attrs.bs][ma][m1.attrs.bar]

        go()
        clear_mappers()

    def test_with_inheritance(self):
        metadata = MetaData(self.engine)

        table1 = Table(
            "mytable",
            metadata,
            Column(
                "col1",
                Integer,
                primary_key=True,
                test_needs_autoincrement=True,
            ),
            Column("col2", String(30)),
        )

        table2 = Table(
            "mytable2",
            metadata,
            Column(
                "col1",
                Integer,
                ForeignKey("mytable.col1"),
                primary_key=True,
                test_needs_autoincrement=True,
            ),
            Column("col3", String(30)),
        )

        @profile_memory()
        def go():
            class A(fixtures.ComparableEntity):
                pass

            class B(A):
                pass

            mapper(
                A,
                table1,
                polymorphic_on=table1.c.col2,
                polymorphic_identity="a",
            )
            mapper(B, table2, inherits=A, polymorphic_identity="b")

            sess = create_session()
            a1 = A()
            a2 = A()
            b1 = B(col3="b1")
            b2 = B(col3="b2")
            for x in [a1, a2, b1, b2]:
                sess.add(x)
            sess.flush()
            sess.expunge_all()

            alist = sess.query(A).order_by(A.col1).all()
            eq_([A(), A(), B(col3="b1"), B(col3="b2")], alist)

            for a in alist:
                sess.delete(a)
            sess.flush()

            # don't need to clear_mappers()
            del B
            del A

        metadata.create_all()
        try:
            go()
        finally:
            metadata.drop_all()
        assert_no_mappers()

    def test_with_manytomany(self):
        metadata = MetaData(self.engine)

        table1 = Table(
            "mytable",
            metadata,
            Column(
                "col1",
                Integer,
                primary_key=True,
                test_needs_autoincrement=True,
            ),
            Column("col2", String(30)),
        )

        table2 = Table(
            "mytable2",
            metadata,
            Column(
                "col1",
                Integer,
                primary_key=True,
                test_needs_autoincrement=True,
            ),
            Column("col2", String(30)),
        )

        table3 = Table(
            "t1tot2",
            metadata,
            Column("t1", Integer, ForeignKey("mytable.col1")),
            Column("t2", Integer, ForeignKey("mytable2.col1")),
        )

        @profile_memory()
        def go():
            class A(fixtures.ComparableEntity):
                pass

            class B(fixtures.ComparableEntity):
                pass

            mapper(
                A,
                table1,
                properties={
                    "bs": relationship(
                        B, secondary=table3, backref="as", order_by=table3.c.t1
                    )
                },
            )
            mapper(B, table2)

            sess = create_session()
            a1 = A(col2="a1")
            a2 = A(col2="a2")
            b1 = B(col2="b1")
            b2 = B(col2="b2")
            a1.bs.append(b1)
            a2.bs.append(b2)
            for x in [a1, a2]:
                sess.add(x)
            sess.flush()
            sess.expunge_all()

            alist = sess.query(A).order_by(A.col1).all()
            eq_([A(bs=[B(col2="b1")]), A(bs=[B(col2="b2")])], alist)

            for a in alist:
                sess.delete(a)
            sess.flush()

            # don't need to clear_mappers()
            del B
            del A

        metadata.create_all()
        try:
            go()
        finally:
            metadata.drop_all()
        assert_no_mappers()

    @testing.provide_metadata
    def test_key_fallback_result(self):
        e = self.engine
        m = self.metadata
        t = Table("t", m, Column("x", Integer), Column("y", Integer))
        m.create_all(e)
        e.execute(t.insert(), {"x": 1, "y": 1})

        @profile_memory()
        def go():
            r = e.execute(t.alias().select())
            for row in r:
                row[t.c.x]

        go()

    def test_many_discarded_relationships(self):
        """a use case that really isn't supported, nonetheless we can
        guard against memleaks here so why not"""

        m1 = MetaData()
        t1 = Table("t1", m1, Column("id", Integer, primary_key=True))
        t2 = Table(
            "t2",
            m1,
            Column("id", Integer, primary_key=True),
            Column("t1id", ForeignKey("t1.id")),
        )

        class T1(object):
            pass

        t1_mapper = mapper(T1, t1)

        @testing.emits_warning()
        @profile_memory()
        def go():
            class T2(object):
                pass

            t2_mapper = mapper(T2, t2)
            t1_mapper.add_property("bar", relationship(t2_mapper))
            s1 = Session()
            # this causes the path_registry to be invoked
            s1.query(t1_mapper)._compile_context()

        go()

    # fails on newer versions of pysqlite due to unusual memory behvior
    # in pysqlite itself. background at:
    # http://thread.gmane.org/gmane.comp.python.db.pysqlite.user/2290

    @testing.crashes("mysql+cymysql", "blocking")
    def test_join_cache_deprecated_coercion(self):
        metadata = MetaData(self.engine)
        table1 = Table(
            "table1",
            metadata,
            Column(
                "id", Integer, primary_key=True, test_needs_autoincrement=True
            ),
            Column("data", String(30)),
        )
        table2 = Table(
            "table2",
            metadata,
            Column(
                "id", Integer, primary_key=True, test_needs_autoincrement=True
            ),
            Column("data", String(30)),
            Column("t1id", Integer, ForeignKey("table1.id")),
        )

        class Foo(object):
            pass

        class Bar(object):
            pass

        mapper(
            Foo, table1, properties={"bars": relationship(mapper(Bar, table2))}
        )
        metadata.create_all()
        session = sessionmaker()

        @profile_memory()
        def go():
            s = table2.select()
            sess = session()
            with testing.expect_deprecated(
                "Implicit coercion of SELECT and " "textual SELECT constructs"
            ):
                sess.query(Foo).join(s, Foo.bars).all()
            sess.rollback()

        try:
            go()
        finally:
            metadata.drop_all()

    @testing.crashes("mysql+cymysql", "blocking")
    def test_join_cache(self):
        metadata = MetaData(self.engine)
        table1 = Table(
            "table1",
            metadata,
            Column(
                "id", Integer, primary_key=True, test_needs_autoincrement=True
            ),
            Column("data", String(30)),
        )
        table2 = Table(
            "table2",
            metadata,
            Column(
                "id", Integer, primary_key=True, test_needs_autoincrement=True
            ),
            Column("data", String(30)),
            Column("t1id", Integer, ForeignKey("table1.id")),
        )

        class Foo(object):
            pass

        class Bar(object):
            pass

        mapper(
            Foo, table1, properties={"bars": relationship(mapper(Bar, table2))}
        )
        metadata.create_all()
        session = sessionmaker()

        @profile_memory()
        def go():
            s = table2.select().subquery()
            sess = session()
            sess.query(Foo).join(s, Foo.bars).all()
            sess.rollback()

        try:
            go()
        finally:
            metadata.drop_all()
