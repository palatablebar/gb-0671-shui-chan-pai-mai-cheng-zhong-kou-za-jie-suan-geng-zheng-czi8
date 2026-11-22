import hashlib, json
from datetime import date, datetime, timedelta, timezone
from app.errors import BusinessError
from app.modules.archive.rules import seal_of

# 封存清单使用 Asia/Shanghai 日历日判定到期；该时区自1991年起无夏令时，固定偏移即可。
LOCAL_TZ = timezone(timedelta(hours=8), 'Asia/Shanghai')
DEFAULT_RETENTION_DAYS = 30

RETAIN_REASONS = {
    'MANUAL_HOLD': '争议登记未闭合',
    'DISPUTE_QUARANTINE': '隔离证据关联未闭合',
    'CORRECTION_OPEN': '更正链未闭合',
    'EVIDENCE_SHARED': '证据被待处理票引用',
    'EVIDENCE_MISMATCH': '票据与封存证据对不上',
}

def today():
    return datetime.now(LOCAL_TZ).date()

def parse_day(value):
    try:
        return date.fromisoformat(str(value).strip())
    except (ValueError, TypeError):
        raise BusinessError('BAD_TIME', '处置基准日须为 YYYY-MM-DD')

def retention_days(state):
    raw = state['context'].get('retention_days', DEFAULT_RETENTION_DAYS)
    try:
        days = int(raw)
    except (TypeError, ValueError):
        raise BusinessError('BAD_CONTEXT', '保留期须为整数天数')
    if days < 0:
        raise BusinessError('BAD_CONTEXT', '保留期不能为负')
    return days

def generated_day(state):
    raw = (state.get('manifest') or {}).get('generated_at')
    if raw:
        return datetime.fromisoformat(raw).astimezone(LOCAL_TZ).date()
    return today()

def retention_until(state):
    return generated_day(state) + timedelta(days=retention_days(state))

def _reason(code):
    return {'code': code, 'detail': RETAIN_REASONS[code]}

def _ticket_set(values):
    result = {str(v).strip() for v in values}
    result.discard('')
    return result

def build_plan(state, as_of=None, already_disposed=(), extra_disputes=()):
    """按票权衡到期处置：逐票判定 可处置/保留/已处置，不整批延期也不整批清空。

    纯函数：不读写存储。already_disposed 为历史处置记录中已处置票号，
    extra_disputes 为执行时追加的争议登记（与批次上下文 disputes 合并）。
    """
    as_of = as_of or today()
    until = retention_until(state)
    expired = as_of >= until
    seal_ok = seal_of(state['final']) == state['archive']['seal']
    disposed_before = _ticket_set(already_disposed)
    conflict_tickets = _ticket_set(c['ticket'] for c in state['version_conflicts'])
    quarantine_tickets = _ticket_set(q.get('raw', {}).get('ticket', '') for q in state['quarantine'])
    disputes = _ticket_set(state['context'].get('disputes', [])) | _ticket_set(extra_disputes)
    audit_index = {(a['ticket'], a['version']): a for a in state['version_audit']}
    record_lines = {r['_line'] for r in state['records']}

    entries = []
    for ticket in sorted(state['final'], key=lambda t: t['ticket']):
        tid = ticket['ticket']
        entry = {'ticket': tid, 'version': ticket['version'], 'amount': ticket['amount'],
                 'due': ticket['due'], 'lines': list(ticket['lines']), 'reasons': []}
        if tid in disposed_before:
            entry['decision'] = '已处置'
            entries.append(entry)
            continue
        if tid in disputes:
            entry['reasons'].append(_reason('MANUAL_HOLD'))
        if tid in quarantine_tickets:
            entry['reasons'].append(_reason('DISPUTE_QUARANTINE'))
        if tid in conflict_tickets:
            entry['reasons'].append(_reason('CORRECTION_OPEN'))
        audit = audit_index.get((tid, ticket['version']))
        if audit is None or audit['amount'] != ticket['amount'] or not set(ticket['lines']) <= record_lines:
            entry['reasons'].append(_reason('EVIDENCE_MISMATCH'))
        entries.append(entry)
    # 更正链断裂的票进不了生效集，但其原始依据必须按“更正中”保留。
    final_tickets = {t['ticket'] for t in state['final']}
    for conflict in sorted(state['version_conflicts'], key=lambda c: c['ticket']):
        if conflict['ticket'] not in final_tickets and conflict['ticket'] not in disposed_before:
            entries.append({'ticket': conflict['ticket'], 'version': conflict.get('version', ''),
                            'amount': '', 'due': '', 'lines': [], 'reasons': [_reason('CORRECTION_OPEN')]})
    # 证据被待处理票引用则连带保留，迭代到不动点。
    changed = True
    while changed:
        changed = False
        retained_lines = set()
        for entry in entries:
            if entry.get('decision') != '已处置' and entry['reasons']:
                retained_lines |= set(entry['lines'])
        for entry in entries:
            if entry.get('decision') == '已处置' or entry['reasons']:
                continue
            if set(entry['lines']) & retained_lines:
                entry['reasons'].append(_reason('EVIDENCE_SHARED'))
                changed = True
    for entry in entries:
        if 'decision' not in entry:
            entry['decision'] = '保留' if entry['reasons'] else '可处置'
    disposable = [e['ticket'] for e in entries if e['decision'] == '可处置']
    retained = [e['ticket'] for e in entries if e['decision'] == '保留']
    plan = {'run_id': (state.get('manifest') or {}).get('run_id', 'preview'),
            'seal': state['archive']['seal'], 'seal_ok': seal_ok,
            'expired': expired, 'as_of': as_of.isoformat(),
            'retention_until': until.isoformat(), 'retention_days': retention_days(state),
            'entries': entries, 'disposable': disposable, 'retained': retained,
            'already_disposed': sorted(disposed_before),
            'quarantine_retained': len(state['quarantine'])}
    plan['executable'] = bool(expired and seal_ok and disposable)
    plan['digest'] = hashlib.sha256(json.dumps(
        {'run_id': plan['run_id'], 'seal': plan['seal'], 'disposed': disposable, 'retained': retained},
        ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return plan

def _evidence_digest(state, entry):
    lines = set(entry['lines'])
    evidence = {'ticket': next(t for t in state['final'] if t['ticket'] == entry['ticket']),
                'audit': [a for a in state['version_audit'] if a['ticket'] == entry['ticket']],
                'records': [r for r in state['records'] if r['_line'] in lines]}
    return hashlib.sha256(json.dumps(evidence, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

def build_record(state, plan, revision, as_of, extra_disputes=()):
    """生成一版处置结果：处置明细含逐票证据摘要，处置后仍可对照封存复核。"""
    disposed = [{'ticket': e['ticket'], 'version': e['version'], 'amount': e['amount'],
                 'lines': e['lines'], 'evidence_sha256': _evidence_digest(state, e)}
                for e in plan['entries'] if e['decision'] == '可处置']
    retained = [{'ticket': e['ticket'], 'version': e['version'], 'reasons': e['reasons']}
                for e in plan['entries'] if e['decision'] == '保留']
    return {'disposal_id': f"{plan['run_id']}-D{revision:02d}", 'run_id': plan['run_id'],
            'revision': revision, 'seal': plan['seal'], 'as_of': as_of.isoformat(),
            'created_at': datetime.now(LOCAL_TZ).isoformat(),
            'disputes_registered': sorted(_ticket_set(extra_disputes)),
            'disposed': disposed, 'retained': retained,
            'quarantine_retained': plan['quarantine_retained'], 'digest': plan['digest']}

def plan_view(plan, records=()):
    rows = [{'ticket': e['ticket'], 'version': e['version'], 'amount': e['amount'], 'due': e['due'],
             'decision': e['decision'],
             'basis': '；'.join(r['detail'] for r in e['reasons']) or '—'}
            for e in plan['entries']]
    return {'title': '到期处置', 'rows': rows,
            'metrics': {'可处置': len(plan['disposable']), '保留': len(plan['retained']),
                        '已处置': len(plan['already_disposed']), '处置记录': len(records)}}

def apply(state):
    plan = build_plan(state)
    state['disposal'] = {'expired': plan['expired'], 'retention_until': plan['retention_until'],
                         'disposable': len(plan['disposable']), 'retained': len(plan['retained']),
                         'status': '可处置' if plan['executable'] else ('未到期' if not plan['expired'] else '暂缓')}
    state['views']['disposal'] = plan_view(plan)
