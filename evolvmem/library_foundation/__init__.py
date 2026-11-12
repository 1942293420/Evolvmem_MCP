"""Candidate foundation only. No MCP business backend is implemented or enabled."""
from .models import (AuthorizedRoute, AuthorizationSnapshot, ConnectionBinding,
                     CredentialBinding, FoundationError, LibraryDescriptor, RequestIdentity)
from .routing import Authenticator, AuthorityRouter, ConnectionRegistry
from .sessions import (AuthorityLease, ContentSessionState, DatabaseCredential, DatabaseRole, LibrarySession,
                       PsycopgDirectProvider, SessionManager)

__all__ = ['AuthorizedRoute', 'AuthorizationSnapshot', 'ConnectionBinding', 'CredentialBinding',
           'FoundationError', 'LibraryDescriptor', 'RequestIdentity', 'Authenticator',
           'AuthorityRouter', 'ConnectionRegistry', 'AuthorityLease', 'ContentSessionState',
           'DatabaseCredential', 'DatabaseRole', 'LibrarySession', 'PsycopgDirectProvider', 'SessionManager']
