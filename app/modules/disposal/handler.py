from fastapi import APIRouter, Request, Body
from app.config import META
from app.engine import evaluate
from app.errors import BusinessError
from app.modules.disposal import rules
from app import store

router=APIRouter()

def _load(request):
    from app.main import DB_PATH
    run_id=request.query_params.get('run_id')
    if run_id:
        return DB_PATH,store.get(DB_PATH,run_id)
    return DB_PATH,store.save(DB_PATH,evaluate({'dataset':request.query_params.get('dataset','dirty')}))

def _as_of(value):
    return rules.parse_day(value) if value else None

@router.get('/disposal')
def page(request:Request):
    from app.web import templates
    db,state=_load(request)
    as_of=_as_of(request.query_params.get('as_of')) or rules.today()
    plan=rules.build_plan(state,as_of=as_of,already_disposed=store.disposed_tickets(db,state['manifest']['run_id']))
    records=store.disposal_history(db,state['manifest']['run_id'])
    batches=[]
    for item in store.history(db):
        run=store.get(db,item['id'])
        until=rules.retention_until(run)
        batches.append({'run_id':item['id'],'source':run['source'],'status':item['status'],
            'generated_at':run['manifest']['generated_at'][:10],'retention_until':until.isoformat(),
            'expired':'是' if as_of>=until else '否'})
    record_rows=[{'revision':r['revision'],'disposal_id':r['disposal_id'],'as_of':r['as_of'],
        'disposed':len(r['disposed']),'retained':len(r['retained']),'digest':r['digest'][:16]} for r in records]
    return templates.TemplateResponse(request=request,name='disposal.html',
        context={'meta':META,'key':'disposal','state':state,'view':rules.plan_view(plan,records),
            'plan':plan,'batches':batches,'record_rows':record_rows,
            'current_revision':records[-1]['revision'] if records else 0,'as_of':as_of.isoformat()})

@router.post('/api/disposal')
def calculate(payload:dict=Body(default={})):
    return evaluate(payload,until=8)

@router.get('/api/runs/{run_id}/disposal')
def plan_api(run_id:str,as_of:str=None):
    from app.main import DB_PATH
    state=store.get(DB_PATH,run_id)
    plan=rules.build_plan(state,as_of=_as_of(as_of),already_disposed=store.disposed_tickets(DB_PATH,run_id))
    return {'plan':plan,'records':store.disposal_history(DB_PATH,run_id),'revision':store.disposal_revision(DB_PATH,run_id)}

@router.post('/api/runs/{run_id}/disposal')
def execute(run_id:str,payload:dict=Body(default={})):
    from app.main import DB_PATH
    revision=payload.get('revision')
    if not isinstance(revision,int) or revision<0:
        raise BusinessError('BAD_REVISION','处置版本号须为非负整数')
    extra=payload.get('disputes',[])
    if not isinstance(extra,list) or not all(isinstance(t,str) for t in extra):
        raise BusinessError('BAD_CONTEXT','争议登记须为票号列表')
    as_of=_as_of(payload.get('as_of')) or rules.today()
    state=store.get(DB_PATH,run_id)
    plan=rules.build_plan(state,as_of=as_of,already_disposed=store.disposed_tickets(DB_PATH,run_id),extra_disputes=extra)
    # 全局闸门：任一不满足则整批暂缓，上一版处置结果原样保留。
    if not plan['seal_ok']:
        raise BusinessError('SEAL_MISMATCH','票据与封存证据对不上，禁止处置',409)
    if not plan['expired']:
        raise BusinessError('NOT_EXPIRED',f"保留期至 {plan['retention_until']}，未届满",409)
    if not plan['disposable']:
        return {'changed':False,'revision':store.disposal_revision(DB_PATH,run_id),
                'plan':plan,'records':store.disposal_history(DB_PATH,run_id)}
    record=rules.build_record(state,plan,revision+1,as_of,extra)
    store.record_disposal(DB_PATH,record,expected_revision=revision)
    return {'changed':True,'record':record,'plan':plan}
