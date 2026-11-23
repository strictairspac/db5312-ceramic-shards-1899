from fastapi import APIRouter, Depends, Query
from sqlalchemy import text

from src.config.settings import cache, engine
from src.controllers.workflow import download
from src.models.tables import audit, cases, evidence, runs
from src.schemas.workflow import CaseForm, HandoverSyncForm, LoginForm, ReviewForm
from src.services import domain, handover, workflow
from src.services.security import (current_user, handover_access, login, operator,
                                   reviewer)

router = APIRouter()


@router.get('/health')
def health():
    with engine.connect() as conn:
        conn.execute(text('SELECT 1')).scalar_one()
    cache.ping()
    return {'ok': True}


@router.post('/login')
def sign_in(form: LoginForm):
    return login(form.username, form.password)


@router.get('/spec')
def spec(_=Depends(current_user)):
    return domain.SPEC


@router.get('/cases')
def list_cases(_=Depends(current_user)):
    return workflow.recent(cases)


@router.post('/cases', status_code=201)
def create_case(form: CaseForm, actor=Depends(operator)):
    return workflow.make_case(form.title, form.payload, actor)


@router.get('/runs')
def list_runs(_=Depends(current_user)):
    return workflow.recent(runs)


@router.post('/cases/{case_id}/runs', status_code=202)
def start_run(case_id: int, actor=Depends(operator)):
    return workflow.enqueue(case_id, actor)


@router.get('/evidence')
def list_evidence(_=Depends(current_user)):
    return workflow.recent(evidence)


@router.get('/evidence/{identity}/download')
def export_evidence(identity: int, _=Depends(current_user)):
    return download(identity)


@router.post('/evidence/{identity}/review')
def review_evidence(identity: int, form: ReviewForm, actor=Depends(reviewer)):
    return workflow.review(identity, form.note, actor)


@router.get('/audit')
def list_audit(_=Depends(current_user)):
    return workflow.recent(audit)


@router.get('/handover/state')
def handover_state(crew: str = Query(min_length=1, max_length=40), _=Depends(handover_access)):
    """交班程序查询某班组已追到哪一笔。"""
    return handover.state(crew)


@router.get('/handover/crews/{crew}/state')
def handover_crew_state(crew: str, _=Depends(handover_access)):
    """路径式查询：某班组已追到哪一笔。"""
    return handover.state(crew)


@router.get('/handover/pending')
def handover_pending(crew: str = Query(min_length=1, max_length=40),
                     limit: int = Query(default=100, ge=1, le=1000), _=Depends(handover_access)):
    """只读预览尚未确认的待同步记录，不推进游标。"""
    return handover.pending(crew, limit)


@router.get('/handover/ledger')
def handover_ledger(limit: int = Query(default=100, ge=1, le=1000), _=Depends(handover_access)):
    """按记账顺序列出共享交班账（每个事件只记一次）。"""
    return handover.entries(limit)


@router.get('/handover/crews/{crew}/ledger')
def handover_crew_ledger(crew: str, limit: int = Query(default=100, ge=1, le=1000),
                         _=Depends(handover_access)):
    """该班组视角下的交班账：仅已确认到其游标的部分，每个事件仍只一份。"""
    return handover.entries(limit, upto=handover.state(crew)['last_seq'])


@router.post('/handover/sync')
def handover_sync(form: HandoverSyncForm, _=Depends(handover_access)):
    """把一个班组的交班账从上次确认处补读到最新。

    幂等：回执没送到时可重复调用，交班账不会重复记。
    """
    return handover.sync(form.crew, form.limit)


@router.post('/handover/crews/{crew}/sync')
def handover_crew_sync(crew: str, limit: int = Query(default=100, ge=1, le=1000),
                       _=Depends(handover_access)):
    """路径式补读：把某班组的交班账追到最新（幂等）。"""
    return handover.sync(crew, limit)

