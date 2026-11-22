from fastapi.testclient import TestClient
from app import main, store
from app.engine import evaluate
from app.modules.disposal import rules
from app.config import ROOT
import sqlite3, json

def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DB_PATH', str(tmp_path/'test.sqlite'))
    return TestClient(main.app)

def expired_state(**overrides):
    overrides.setdefault('retention_days', 0)
    return evaluate({'overrides': overrides})

# ---------- 计划（纯函数） ----------

def test_plan_partial_retention():
    plan = rules.build_plan(expired_state(disputes=['00001']))
    assert plan['expired'] and plan['seal_ok'] and plan['executable']
    assert plan['retained'] == ['00001']
    assert len(plan['disposable']) == 19
    entry = next(e for e in plan['entries'] if e['ticket'] == '00001')
    assert [r['code'] for r in entry['reasons']] == ['MANUAL_HOLD']

def test_plan_not_expired_by_default():
    plan = rules.build_plan(evaluate())
    assert not plan['expired'] and not plan['executable']
    assert plan['retention_days'] == 30

def test_plan_quarantine_evidence_kept():
    plan = rules.build_plan(expired_state())
    assert plan['quarantine_retained'] == 8

def test_plan_quarantine_link_retains_ticket():
    state = expired_state()
    state['quarantine'].append({'line': 99, 'id': 'x', 'code': 'PAIR_MISSING', 'detail': '毛皮称重缺失或重复', 'raw': {'ticket': '00002'}})
    plan = rules.build_plan(state)
    entry = next(e for e in plan['entries'] if e['ticket'] == '00002')
    assert entry['decision'] == '保留'
    assert 'DISPUTE_QUARANTINE' in [r['code'] for r in entry['reasons']]

def test_plan_evidence_shared_retains_referencing_ticket():
    state = expired_state()
    state['final'][1]['lines'] = list(state['final'][0]['lines'])
    plan = rules.build_plan(state, extra_disputes=['00001'])
    entry = next(e for e in plan['entries'] if e['ticket'] == '00002')
    assert entry['decision'] == '保留'
    assert 'EVIDENCE_SHARED' in [r['code'] for r in entry['reasons']]

def test_plan_evidence_mismatch_not_disposed():
    state = expired_state()
    state['final'][3]['lines'] = [99999]
    plan = rules.build_plan(state)
    entry = next(e for e in plan['entries'] if e['ticket'] == '00004')
    assert entry['decision'] == '保留'
    assert 'EVIDENCE_MISMATCH' in [r['code'] for r in entry['reasons']]

def test_plan_correction_open_keeps_ticket():
    clean = (ROOT/'sample_data/clean.csv').read_text().rstrip('\n')
    stale = '00001v3gross,00001,gross,130,kg,黄鱼,12.30,0.05,3,1,0\n00001v3tare,00001,tare,15,kg,黄鱼,12.30,0.05,3,1,0\n'
    state = evaluate({'csv_text': clean+'\n'+stale, 'overrides': {'retention_days': 0}})
    assert state['version_conflicts']
    plan = rules.build_plan(state)
    retained = {e['ticket']: [r['code'] for r in e['reasons']] for e in plan['entries'] if e['decision'] == '保留'}
    assert retained == {'00001': ['CORRECTION_OPEN']}
    assert len(plan['disposable']) == 19

def test_plan_already_disposed_excluded():
    plan = rules.build_plan(expired_state(), already_disposed={'00005', '00006'})
    done = [e['ticket'] for e in plan['entries'] if e['decision'] == '已处置']
    assert done == ['00005', '00006']
    assert '00005' not in plan['disposable'] and len(plan['disposable']) == 18

# ---------- 执行（HTTP + 持久化） ----------

def create_run(c, **overrides):
    return c.post('/api/runs', json={'overrides': overrides}).json()['manifest']['run_id']

def test_execute_partial_and_idempotent(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = create_run(c, retention_days=0, disputes=['00001'])
    r1 = c.post(f'/api/runs/{rid}/disposal', json={'revision': 0})
    assert r1.status_code == 200
    body = r1.json()
    assert body['changed'] and len(body['record']['disposed']) == 19
    assert [r['ticket'] for r in body['record']['retained']] == ['00001']
    assert body['record']['retained'][0]['reasons'][0]['code'] == 'MANUAL_HOLD'
    assert all(len(d['evidence_sha256']) == 64 for d in body['record']['disposed'])
    r2 = c.post(f'/api/runs/{rid}/disposal', json={'revision': 1})
    assert r2.status_code == 200 and not r2.json()['changed']
    current = c.get(f'/api/runs/{rid}/disposal').json()
    assert current['revision'] == 1 and len(current['records']) == 1
    assert current['plan']['already_disposed'] != [] and not current['plan']['disposable']

def test_execute_incremental_after_dispute_closed(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = create_run(c, retention_days=0)
    r1 = c.post(f'/api/runs/{rid}/disposal', json={'revision': 0, 'disputes': ['00001', '00002']}).json()
    assert r1['changed'] and len(r1['record']['disposed']) == 18
    r2 = c.post(f'/api/runs/{rid}/disposal', json={'revision': 1, 'disputes': ['00001']}).json()
    assert r2['changed'] and [d['ticket'] for d in r2['record']['disposed']] == ['00002']
    assert r2['record']['revision'] == 2
    r3 = c.post(f'/api/runs/{rid}/disposal', json={'revision': 2, 'disputes': ['00001']}).json()
    assert not r3['changed']
    assert len(c.get(f'/api/runs/{rid}/disposal').json()['records']) == 2

def test_execute_not_expired(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = create_run(c)
    r = c.post(f'/api/runs/{rid}/disposal', json={'revision': 0})
    assert r.status_code == 409 and r.json()['code'] == 'NOT_EXPIRED'
    assert c.get(f'/api/runs/{rid}/disposal').json()['records'] == []

def test_execute_with_future_as_of(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = create_run(c)
    r = c.post(f'/api/runs/{rid}/disposal', json={'revision': 0, 'as_of': '2099-01-01'})
    assert r.status_code == 200 and len(r.json()['record']['disposed']) == 20

def test_seal_mismatch_keeps_previous_record(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = create_run(c, retention_days=0)
    assert c.post(f'/api/runs/{rid}/disposal', json={'revision': 0}).json()['changed']
    db = str(tmp_path/'test.sqlite')
    with sqlite3.connect(db) as con:
        result = json.loads(con.execute('SELECT result FROM runs WHERE id=?', (rid,)).fetchone()[0])
        result['final'][0]['due'] = '0.00'
        con.execute('UPDATE runs SET result=? WHERE id=?', (json.dumps(result, ensure_ascii=False), rid))
    r = c.post(f'/api/runs/{rid}/disposal', json={'revision': 1})
    assert r.status_code == 409 and r.json()['code'] == 'SEAL_MISMATCH'
    records = c.get(f'/api/runs/{rid}/disposal').json()['records']
    assert len(records) == 1 and records[0]['revision'] == 1
    assert c.get(f'/api/runs/{rid}').status_code == 200

def test_execute_stale_revision(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = create_run(c, retention_days=0)
    r = c.post(f'/api/runs/{rid}/disposal', json={'revision': 3})
    assert r.status_code == 409 and r.json()['code'] == 'STALE_VERSION'
    assert c.get(f'/api/runs/{rid}/disposal').json()['records'] == []

def test_execute_bad_params(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = create_run(c, retention_days=0)
    assert c.post(f'/api/runs/{rid}/disposal', json={'revision': -1}).status_code == 422
    assert c.post(f'/api/runs/{rid}/disposal', json={'revision': 'x'}).status_code == 422
    assert c.post(f'/api/runs/{rid}/disposal', json={'revision': 0, 'disputes': '00001'}).status_code == 422
    assert c.post(f'/api/runs/{rid}/disposal', json={'revision': 0, 'as_of': '2026-13-40'}).status_code == 422

def test_archive_index_and_records_survive(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = create_run(c, retention_days=0)
    before = c.get(f'/api/runs/{rid}').json()
    assert c.post(f'/api/runs/{rid}/disposal', json={'revision': 0}).json()['changed']
    assert c.get(f'/api/runs/{rid}').json() == before
    rerun = c.post(f'/api/runs/{rid}/rerun')
    assert rerun.status_code == 200 and rerun.json()['manifest']['run_id'] == rid
    assert len(c.get('/api/runs').json()) == 1
    assert len(c.get(f'/api/runs/{rid}/disposal').json()['records']) == 1

def test_review_and_export_after_disposal(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = create_run(c, retention_days=0)
    c.post(f'/api/runs/{rid}/disposal', json={'revision': 0})
    assert c.get(f'/api/export/{rid}').status_code == 200
    assert c.post(f'/api/runs/{rid}/review', json={'revision': 1}).status_code == 200

def test_disposal_page(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = create_run(c, retention_days=0, disputes=['00001'])
    assert c.get('/disposal').status_code == 200
    html = c.get(f'/disposal?run_id={rid}').text
    assert '?run_id='+rid in html and '到期批次' in html
    assert '争议登记未闭合' in html and '可处置' in html
    c.post(f'/api/runs/{rid}/disposal', json={'revision': 0})
    html = c.get(f'/disposal?run_id={rid}').text
    assert '处置单号' in html and '已处置' in html
