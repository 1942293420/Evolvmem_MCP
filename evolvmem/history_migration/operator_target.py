from contextvars import ContextVar
from dataclasses import asdict
from .operator_config import trusted

_CONSTRUCTION=ContextVar('history_operator_target',default=None)
def permits(values):return _CONSTRUCTION.get()==values

def target_for(config):
    trusted(config)
    from .history_core.importer import Target
    d=config.document['target'];values={k:d[k] for k in Target.__dataclass_fields__}
    token=_CONSTRUCTION.set(values)
    try:return Target(**values)
    finally:_CONSTRUCTION.reset(token)

def gate(connection,target):
    from .operator_scope import LeaseConnection
    from .history_core.mapping import check
    from .batched_importer import BudgetConnection
    from .organization_complete import DeadlinePort
    import time
    while type(connection) in (BudgetConnection,DeadlinePort):
        if type(connection) is BudgetConnection:connection.budget.check()
        else:check(time.monotonic()<connection.deadline,'organization_stage_deadline')
        connection=connection.raw
    check(type(connection) is LeaseConnection,'formal_operator_lease_required')
    connection.authorize(target)
