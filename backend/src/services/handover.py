"""交班账同步（transactional outbox + 幂等交班账 + 按班组游标）。

业务写入和待同步记录在同一个事务里提交，交班程序停机时业务照常办单。
每次业务事务在提交前调用 ``record()``，从 ``handover_sequence`` 取一个递增
位置 ``seq``；该行锁一直持有到事务结束。``record()`` 是业务事务的最后一步，
因此只有提交瞬间参与排序，业务处理本身仍可并发。由于锁在提交前不释放，
较小的 ``seq`` 一定先于较大的 ``seq`` 提交。交班程序按 ``seq`` 顺序补读，
即使某个办单先开始、后提交，也不会被游标越过。

关键点：``handover_sequence`` 只由业务写入（``record``）触碰，交班程序
（``sync``/``state``）从不获取它，所以交班程序停机或落后都不会阻塞业务。

交班账是共享的一份，每个事件只记一次；以 ``outbox_id`` 为幂等键，回执丢失
后重试只会跳过已记账的条目，不会重复记。交班账写入与班组游标推进在同一
事务里，账写妥才算确认。每个班组只推进自己的游标，互不影响。
"""
import hashlib

from sqlalchemy import func, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from src.config import settings
from src.models.tables import cursors, ledger, outbox, sequence
from src.utils.encoding import canonical
from src.utils.time import utc_time

SEQUENCE_NAME = 'handover'
KIND_REPORT = 'report'
KIND_SIGNATURE = 'signature'


def _engine(explicit=None):
    return explicit if explicit is not None else settings.engine


def _insert_ignore(conn, table, values, key):
    """按方言插入一行，已存在则保持原值。``key`` 为冲突判定列（可多列）。

    返回实际插入的行数：冲突被忽略时为 0。
    """
    keys = [key] if isinstance(key, str) else list(key)
    dialect = conn.engine.dialect.name
    if dialect == 'mysql':
        statement = mysql_insert(table).values(**values)
        first = keys[0]
        statement = statement.on_duplicate_key_update(**{first: table.c[first]})
    elif dialect == 'sqlite':
        statement = sqlite_insert(table).values(**values).on_conflict_do_nothing(index_elements=keys)
    else:
        statement = postgres_insert(table).values(**values).on_conflict_do_nothing(index_elements=keys)
    return conn.execute(statement).rowcount


def init(engine=None):
    """确保同步计数行存在。可重复调用。"""
    engine = _engine(engine)
    with engine.begin() as conn:
        if conn.execute(select(sequence.c.name).where(sequence.c.name == SEQUENCE_NAME)).first():
            return
        _insert_ignore(conn, sequence, {'name': SEQUENCE_NAME, 'value': 0}, 'name')


def next_seq(conn):
    """在当前业务事务内取下一个提交位置。

    UPDATE 会锁住计数行直到事务提交，所以位置顺序即提交顺序。
    """
    result = conn.execute(sequence.update().where(sequence.c.name == SEQUENCE_NAME)
                          .values(value=sequence.c.value + 1))
    if result.rowcount == 0:
        _insert_ignore(conn, sequence, {'name': SEQUENCE_NAME, 'value': 0}, 'name')
        conn.execute(sequence.update().where(sequence.c.name == SEQUENCE_NAME)
                     .values(value=sequence.c.value + 1))
    return conn.execute(select(sequence.c.value).where(sequence.c.name == SEQUENCE_NAME)).scalar_one()


def record(conn, kind, reference, payload):
    """业务事务内登记待同步记录。与调用方的业务写入同成同败。"""
    seq = next_seq(conn)
    conn.execute(outbox.insert().values(seq=seq, kind=kind, reference=str(reference),
                                        payload=payload, created_at=utc_time()))
    return seq


def record_report(conn, evidence_id, run_id, digest, report):
    """试算完成：先入报告。"""
    return record(conn, KIND_REPORT, evidence_id, {
        'evidence_id': evidence_id,
        'run_id': run_id,
        'digest': digest,
        'report': report,
    })


def record_signature(conn, evidence_id, reviewer, reviewed_at, note):
    """复核签署：后入签字。"""
    return record(conn, KIND_SIGNATURE, evidence_id, {
        'evidence_id': evidence_id,
        'reviewer': reviewer,
        'reviewed_at': reviewed_at,
        'note': note,
    })


def ensure_report(conn, evidence_row):
    """确保报告先于签字入账。

    对任何报告尚未登记的既有证据（例如升级前已完成的试算）补记报告，随后
    签署才有可对应的前序记录。已登记则保持原位置不变。
    """
    reference = str(evidence_row['id'])
    existing = conn.execute(select(outbox.c.id).where(
        outbox.c.kind == KIND_REPORT, outbox.c.reference == reference)).first()
    if existing:
        return None
    return record_report(conn, evidence_row['id'], evidence_row['run_id'],
                         evidence_row['digest'], evidence_row['report'])


def _ensure_cursor(engine, crew):
    """确保游标行存在；已存在则不做任何写入。"""
    with engine.connect() as conn:
        if conn.execute(select(cursors.c.crew).where(cursors.c.crew == crew)).first():
            return
    with engine.begin() as conn:
        _insert_ignore(conn, cursors, {'crew': crew, 'last_seq': 0, 'updated_at': utc_time()}, 'crew')


def pending(crew, limit=100, engine=None):
    """预览尚未确认的待同步记录（只读，不推进游标）。"""
    engine = _engine(engine)
    limit = max(1, min(int(limit), 1000))
    with engine.connect() as conn:
        row = conn.execute(select(cursors.c.last_seq).where(cursors.c.crew == crew)).first()
        last_seq = row.last_seq if row else 0
        rows = conn.execute(select(outbox).where(outbox.c.seq > last_seq)
                            .order_by(outbox.c.seq).limit(limit)).mappings().all()
    return {'crew': crew, 'from_seq': last_seq,
            'entries': [{**dict(row), 'checksum': digest(row['payload'])} for row in rows]}


def sync(crew, limit=100, engine=None):
    """把一个班组的交班账从上次确认处补读到位。

    交班账是全班组共享的一份：每个事件只记一次，不按班组复制。交班账写入
    与班组游标推进在同一事务里，账写妥才算确认；每个班组只推进自己的游标，
    互不影响。回执丢失后重复调用不会重复记账。
    """
    engine = _engine(engine)
    limit = max(1, min(int(limit), 1000))
    _ensure_cursor(engine, crew)
    # 先无锁看一眼：已追平就不必取锁写游标，避免空轮询的写放大。
    with engine.connect() as conn:
        last_seq = conn.execute(select(cursors.c.last_seq).where(
            cursors.c.crew == crew)).scalar_one()
        head = conn.execute(select(func.max(outbox.c.seq))).scalar_one() or 0
    if head <= last_seq:
        return {'crew': crew, 'from_seq': last_seq, 'to_seq': last_seq,
                'recorded': 0, 'skipped': 0}

    with engine.begin() as conn:
        # 先对该班组的游标行做一次 UPDATE 以取得行锁（MySQL/Postgres）或写锁
        # （SQLite）并持有到提交：同班组的并发同步因此串行，读完再推进。
        # 其他班组的游标行不在此期间被锁，互不影响。
        conn.execute(cursors.update().where(cursors.c.crew == crew)
                     .values(updated_at=utc_time()))
        from_seq = conn.execute(select(cursors.c.last_seq).where(
            cursors.c.crew == crew)).scalar_one()
        batch = conn.execute(select(outbox).where(outbox.c.seq > from_seq)
                             .order_by(outbox.c.seq).limit(limit)).mappings().all()
        if not batch:
            return {'crew': crew, 'from_seq': from_seq, 'to_seq': from_seq,
                    'recorded': 0, 'skipped': 0}

        ids = [item['id'] for item in batch]
        already = set(conn.execute(select(ledger.c.outbox_id).where(
            ledger.c.outbox_id.in_(ids))).scalars())
        recorded = 0
        for item in batch:
            if item['id'] in already:
                continue
            # 跨班组的并发同步可能同时命中同一事件；用冲突忽略保证共享交班账
            # 每个事件只落一行，后到的班组静默跳过。
            recorded += _insert_ignore(conn, ledger, {
                'crew': crew, 'outbox_id': item['id'], 'seq': item['seq'],
                'kind': item['kind'], 'reference': item['reference'],
                'payload': item['payload'], 'recorded_at': utc_time()},
                'outbox_id')
        to_seq = batch[-1]['seq']
        # 与交班账同一事务提交；提交成功才算确认。
        conn.execute(cursors.update().where(cursors.c.crew == crew)
                     .values(last_seq=to_seq, updated_at=utc_time()))
    return {'crew': crew, 'from_seq': from_seq, 'to_seq': to_seq,
            'recorded': recorded, 'skipped': len(batch) - recorded}


def state(crew, engine=None):
    """查询某班组已追到哪一笔。"""
    engine = _engine(engine)
    with engine.connect() as conn:
        row = conn.execute(select(cursors).where(cursors.c.crew == crew)).mappings().first()
        last_seq = row['last_seq'] if row else 0
        updated_at = row['updated_at'] if row else None
        last_entry = conn.execute(select(ledger).where(ledger.c.seq <= last_seq)
                                  .order_by(ledger.c.seq.desc()).limit(1)).mappings().first()
        ledger_count = conn.execute(select(func.count()).select_from(ledger)
                                    .where(ledger.c.seq <= last_seq)).scalar_one()
        pending_count = conn.execute(select(func.count()).select_from(outbox)
                                     .where(outbox.c.seq > last_seq)).scalar_one()
        latest_seq = conn.execute(select(func.max(outbox.c.seq))).scalar_one() or 0
    return {
        'crew': crew,
        'last_seq': last_seq,
        'ledger_count': ledger_count,
        'pending_count': pending_count,
        'latest_seq': latest_seq,
        'caught_up': pending_count == 0,
        'updated_at': updated_at,
        'last_entry': dict(last_entry) if last_entry else None,
    }


def entries(limit=100, upto=None, engine=None):
    """按记账顺序列出共享交班账；``upto`` 限制为某班组已确认的位置。"""
    engine = _engine(engine)
    limit = max(1, min(int(limit), 1000))
    query = select(ledger).order_by(ledger.c.seq).limit(limit)
    if upto is not None:
        query = query.where(ledger.c.seq <= upto)
    with engine.connect() as conn:
        rows = conn.execute(query).mappings().all()
    return [dict(row) for row in rows]


def digest(payload):
    """待同步内容的校验摘要，便于交班程序核对。"""
    return hashlib.sha256(canonical(payload)).hexdigest()
