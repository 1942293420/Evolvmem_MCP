"""Privileged first activation of registered legacy libraries; no MCP surface."""
from .coordinator import AuthorityIntent,AuthorityCoordinator,AuthorityOperation
from .worker import AuthorityProjector

__all__=['AuthorityIntent','AuthorityCoordinator','AuthorityOperation','AuthorityProjector']
