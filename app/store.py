import sqlite3, json
from pathlib import Path
from app.errors import BusinessError

def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con=sqlite3.connect(path,timeout=15)
    con.row_factory=sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    con.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, digest TEXT UNIQUE NOT NULL, result TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT '待复核')")
    con.execute("CREATE TABLE IF NOT EXISTS disposals (id TEXT PRIMARY KEY, run_id TEXT NOT NULL, revision INTEGER NOT NULL, digest TEXT NOT NULL, result TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(run_id,revision), UNIQUE(run_id,digest))")
    return con

def save(path, result):
    run_id=result['manifest']['run_id']
    with connect(path) as con:
        con.execute('INSERT OR IGNORE INTO runs(id,digest,result) VALUES(?,?,?)', (run_id,result['manifest']['sha256'],json.dumps(result,ensure_ascii=False)))
    return get(path,run_id)

def get(path, run_id):
    with connect(path) as con: row=con.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
    if not row: raise BusinessError('NOT_FOUND','批次不存在',404)
    result=json.loads(row['result']);result.update(revision=row['revision'],status=row['status'])
    return result

def history(path):
    with connect(path) as con:
        return [dict(r) for r in con.execute('SELECT id,revision,status FROM runs ORDER BY rowid DESC LIMIT 100')]

def review(path,run_id,revision):
    with connect(path) as con:
        cur=con.execute('UPDATE runs SET revision=revision+1,status=? WHERE id=? AND revision=?',('已复核',run_id,revision))
        if cur.rowcount!=1:
            if not con.execute('SELECT 1 FROM runs WHERE id=?',(run_id,)).fetchone():
                raise BusinessError('NOT_FOUND','批次不存在',404)
            raise BusinessError('STALE_VERSION','复核版本已更新，请刷新',409)
    return get(path,run_id)

def disposal_history(path,run_id):
    with connect(path) as con:
        rows=con.execute('SELECT result FROM disposals WHERE run_id=? ORDER BY revision',(run_id,)).fetchall()
    return [json.loads(r['result']) for r in rows]

def disposal_revision(path,run_id):
    with connect(path) as con:
        return con.execute('SELECT COALESCE(MAX(revision),0) AS rev FROM disposals WHERE run_id=?',(run_id,)).fetchone()['rev']

def disposed_tickets(path,run_id):
    tickets=set()
    for record in disposal_history(path,run_id):
        tickets.update(d['ticket'] for d in record['disposed'])
    return tickets

def record_disposal(path,record,expected_revision):
    # 单事务写入：版本检查失败或唯一约束冲突时整体回滚，上一版处置结果保持原样。
    with connect(path) as con:
        current=con.execute('SELECT COALESCE(MAX(revision),0) AS rev FROM disposals WHERE run_id=?',(record['run_id'],)).fetchone()['rev']
        if current!=expected_revision:
            raise BusinessError('STALE_VERSION','处置版本已更新，请刷新',409)
        try:
            con.execute('INSERT INTO disposals(id,run_id,revision,digest,result,created_at) VALUES(?,?,?,?,?,?)',
                (record['disposal_id'],record['run_id'],record['revision'],record['digest'],json.dumps(record,ensure_ascii=False),record['created_at']))
        except sqlite3.IntegrityError:
            raise BusinessError('DUPLICATE_DISPOSAL','相同处置结果已存在，不重复处置',409)
    return record
