"""Private, default-off policy for reusing an already installed timeout pair.

This module is not a product setting and has no network, RPC or request-facing
surface.  A trusted fixture launcher may call :func:`attest` and
:func:`install_profile` once, after its own readback/proof has verified the
actual installed schema and connection metadata, and before the Host starts.
With no installed profile every connection follows the original driver path.

The policy is two independent exact checks:

* the connection binding (kind, host, port, database, user, TLS mode/root CA)
  must equal an attested binding and the configured cap pair must equal the
  attested pair; and
* the SQL string, byte-for-byte, must have one of the reviewed SHA256 values
  for that kind from the pinned specification.

There is no AST scan, whitespace normalization, prefix match, denylist or
generic SELECT/INSERT/UPDATE shape rule.  Parameters are not part of the
digest and are never rewritten by the driver.
"""
from dataclasses import dataclass
import hashlib
import re
import threading

# Reviewed in 20261104-fixed-timeout-reuse-spec.md (36 statements; the task's
# SHA f8a9... pins the initial-profile version of that document), plus the
# separately reviewed pending-join and Task 2B mapping-join statements below (38 total). These are
# decoded source SQL strings, before parameter binding.  Keep this list exact:
# adding a statement requires a new review and a new proof.
APPROVED_SQL_SHA256 = {
    'control': frozenset({
        # evolvmem/library_catalog/adapter.py:160 begin (fused role check)
        '18713999bf3ddf09e27146955b5b35719796eb4a91a87f38469b1b05d87b43af',
        # evolvmem/library_catalog/adapter.py:286 credential_for_digest
        'fd99fd744176a5ffc1160f67e78dcc6e567a38a8d09649ae17daa861f56dac90',
        # evolvmem/library_catalog/adapter.py:496 _hold_routes
        'e4d6d4471d8530c13bb6981e58febf18d94e02e8db26e03e755a17614e57bd91',
        # evolvmem/library_catalog/adapter.py:252 _AUTHORIZATION_SQL
        'de9fa89ae4782959536f4ed4938cbfb6715e411b4005eb376aa4724bc33b221b',
    }),
    'content': frozenset({
        # evolvmem/library_foundation/sessions.py:467 inspect_role
        'd5004d0a5cae5796dbb13f6a49933823a3a79fa91554a3916df1cc10296aeffa',
        # evolvmem/library_foundation/sessions.py:485 session_context
        '83b71cd0fc803ea352f4459ba94c84ea53b2068e3f7d65fb82c7a9cc9ddcf9c3',
        # evolvmem/library_foundation/sessions.py:505 session_credential
        'b30d8df2c0c963a8d9b420d30cb878778eac14cd681747698378e82549c17a54',
        # evolvmem/library_project_acl/scope.py:38 resolve_project_scope
        'ae34ffc66afd58176bce81a708e70247a694671c54274faab7c7d532e4e3cb5d',
        # evolvmem/pg_business_core/executor.py:37 _store_result
        'e9f7be17b843f4fb304f64b0f0600cc92d28ca9d3090cb32003b4323257f6210',
        # evolvmem/pg_business_core/executor.py:43 _confirmed_result
        'e9f1e8c03f5b15e14a4d16fc48caa18ca9c2244897e2333a0e37774ed735d5e0',
        # evolvmem/pg_business_core/executor.py:95 mutate
        '9d4c8eacfbffb222191cbaa5ec424ca36b4afe69b4abe2072ade11c8af6a224b',
        # evolvmem/pg_business_core/executor.py:104 mutate
        '553ed1d7848843c45070ecc7fc27194a7f5a9d32d54d05335e1ca2c89776abd9',
        # evolvmem/pg_business_core/core.py:76 get_by_id
        'a5b7874023adf5f931b012b127a2f0cdcf7fe60fc7ba88622f79d09cfff1db78',
        # evolvmem/pg_business_core/core.py:79 get_by_key
        'df59162a57c25a2a23a1e3eb5a37450977dc137a1ebcf8099cb2588376c3f7e5',
        # evolvmem/pg_business_core/core.py:82 resolve_legacy_mapping
        '9c3c345299bcf3f5a9356d0932b6eb03f433d1e474d42b264da0581eff2ea02f',
        # evolvmem/pg_business_core/core.py:85 record_legacy_mapping
        'e789130a566efa4e94a83fc658375b3999bb143415bee15dab7c950bd90e989b',
        # evolvmem/pg_business_core/core.py:111 _resolution
        'b7bee334ea349c24b6c48540100e75d1882798181b05a6787fc0244ee101b5bd',
        # evolvmem/pg_business_core/core.py:121 _insert_projection
        'd17389c5f0bc7003cb44d5518b276c2de62bebc38746c034608d1561e9fc94a2',
        # evolvmem/pg_business_core/core.py:137 _create_item
        'd73b19f8b32a18572fce1f9a673dc29c2e2c1886d9aa45c427bf67cf9171d29e',
        # evolvmem/pg_business_core/core.py:146 _create_item
        'ebd4c4f8ab3d5feac8c993d133cbeb5eb64794c40b9076c2164d2c4c6a74a121',
        # evolvmem/pg_business_core/core.py:152 get_item
        '22803cace3543081d5305075a5df0a27d8e8c67bc5dbdc0a98075ec46c2151f7',
        # evolvmem/pg_business_core/core.py:157 get_item
        'c9ab395235557ed63d1b4f0309cc44d10338369b7d832ea52263c6a89346c386',
        # evolvmem/pg_business_core/core.py:174 _supported_item
        'a28c0381342e6f3cbb43051e479967c7fbd296b866d4a8ef472456e4deb6571e',
        # evolvmem/pg_business_core/core.py:185 record_migration_source
        '4290c8f1bd628651bc5fab16887768fd3410c59eb9d9c89113f3e9a783f62b46',
        # evolvmem/pg_business_core/core.py:187 record_migration_source
        '7fcbe7b77e300cf8ebaf6337516d999568318363ee648aada2c269633a4bb0eb',
        # Exact context LEFT JOIN in the separately reviewed pending-join source/spec.
        '2bffb82df131b22155a428293f4334f8686e4a6611231520119a0ee34ab3ed18',
        # evolvmem/pg_business_core/core.py:214 _pending
        '69c322988dc88cb645718698208c09a16d2132ded168ed370a67f3ecc39c595b',
        # evolvmem/pg_business_core/core.py:205 _pending
        '57f7b3118cabda34473b745ac796563204a22428c0b9bd77280b6461738ac4a5',
        # evolvmem/pg_business_core/core.py:234 supersede_item
        'e1d6178a6079d5fd540ed9bc390ff6d7134c7f267264b959c9c76c96f6f57837',
        # evolvmem/pg_business_core/core.py:240 supersede_item
        'aa961bf66113bb527021be812dd6be19efeb3e90b17a5063b97b1387ebb9fa6b',
        # evolvmem/pg_business_core/core.py:238 supersede_item
        '40916ed7be09255e2193f4ee246d2cb0761eacb7d3bd602cb77221d178678386',
        # evolvmem/pg_business_core/core.py:235 supersede_item
        '47767d630608bd96669fb3e9f03975c6a6e1ffbddeb541ae28c89aaeaf268a31',
        # evolvmem/pg_business_core/core.py:246 legacy_replace
        '12291a99069a2adc8addc887204ac97b76dcf446930d3cbc83e7501fe7d99ec5',
        # evolvmem/pg_business_core/core.py legacy_replace initial mapping JOIN (Task 2B)
        'e5c9e594172408f9aed339eb77d1855318af044e8ae6178261f4a7d77bad0d38',
        # evolvmem/pg_business_core/core.py:250 legacy_replace
        '28c67728270b479b60c1fdbd00e2e23ad0d15675cab8c5e392a6fbfe1ff19c8a',
        # evolvmem/pg_business_core/core.py:261 legacy_replace
        'db645beed1b2178a196cb21f2217883795599247448c3d5e6d92bbbed41c8427',
        # evolvmem/pg_business_core/core.py:284 legacy_remove
        '3aea1e601e2df82bcd7aa59a55f67041e9b493dfd45060046e47601683cadf0e',
        # evolvmem/pg_business_core/core.py:285 legacy_remove
        '8034a450741083e0ee28bcec1a3ceef8b17d8d7b7a442ad603ae737330a3660f',
    }),
}

# Separate opt-in native background inventory from the Optional second
# allowlist in 20261104-fixed-timeout-reuse-spec.md (main spec SHA
# fd76eb2a...).  The conditional vector upsert
# 03740220957bfa66ddcc376ac4acbdff3b79ddc4d7467c9674231b6fffeda5b6 is
# intentionally excluded.  These 14 are content-only and are never
# enabled by the base profile.
NATIVE_BACKGROUND_SQL_SHA256 = frozenset({
    # evolvmem/pg_vector_port/repository.py:36 check_model
    'f7bca20644fa0e4ba50390e7026fb0370321c80155a7e46484e5b20cbc2c4a05',
    # evolvmem/pg_vector_port/repository.py:49 next_job
    '19d378a937d0a87fd143a8e711873ca89d7d862ba7a8f60d3af91dbb620df57b',
    # evolvmem/pg_vector_port/repository.py:87 document
    '266f24af6ab8b33c7bc3a17d741d2eee27c584f562beeb1f973c91e66f05aebe',
    # evolvmem/pg_vector_port/repository.py:87 document
    'd36495243b1cbd776a23aea72d17f4cad1d7e158db9e5f859ad2685624ba9483',
    # evolvmem/pg_vector_port/repository.py:91 document
    '8b0ebe7289af6d9eeeb701246bb5ed0e0c33879dec1bb6a27435b2200506a125',
    # evolvmem/pg_vector_port/repository.py:91 document
    'e2492ecbb1b116e35030c6af08ae6cab91c394ecf1ac3c14bf512511ca6cf908',
    # evolvmem/pg_vector_port/repository.py:83 document
    'ef5a215074c3e3a6cbeb88237171776b0c7667feab3e401dae58a8111c984b33',
    # evolvmem/pg_vector_port/repository.py:83 document
    '3aa5c52d04869cfb2a189402f378e652a3c6a9659b20a463d682f42bb07a6e96',
    # evolvmem/pg_vector_port/repository.py:166 current_job
    'a134c5e874805543927ae0229c2d7e2f84c8d1ecf2fe220e3acd2d805f8a5b03',
    # evolvmem/pg_vector_port/repository.py:182 _claim_pair
    '78a39115e16b02b2ecb9c33baf05f6e49ef354713559884ea6b8b79912d3e1d1',
    # evolvmem/pg_vector_port/repository.py:267 apply
    '10ca98e02a0958ac48494b9a6ba0d7edb0b4767f09eb65b771bb32a8eab46ed6',
    # evolvmem/pg_vector_port/repository.py:285 _ack
    '773fae7d9c611e2bb52e1a5688d4388f21ae98a334d0194d2b294a3294ff1ce8',
    # evolvmem/pg_business_core/history_qualification_sql.py:16 historical_mode
    '2a8da2fe6082b4cf3b42950a0aca1e0b6e9461f4e3691ea7f37b6da2be3e015a',
    # evolvmem/library_host.py:266 start_projection.selection.page
    '65df6dec643ff4bb7fdfe069911531c3bef44543f9642e76647dcf4233f08c2e',
})

_DIGEST = re.compile(r'[0-9a-f]{64}\Z')
_ALLOWED_CONNECT_KEYS = frozenset({
    'host', 'hostaddr', 'port', 'dbname', 'user', 'password', 'connect_timeout',
    'autocommit', 'sslmode', 'sslrootcert',
})


@dataclass(frozen=True)
class Binding:
    """Exact connection identity attested by the trusted launcher."""
    kind: str
    host: str
    hostaddr: str
    port: int
    database: str
    user: str
    sslmode: str
    sslrootcert: str
    ca_sha256: str


@dataclass(frozen=True)
class Profile:
    """One attested binding plus the exact cap pair and proof digests."""
    binding: Binding
    statement_ms: int
    lock_ms: int
    source_sha256: str
    proof_sha256: str
    # 'base' enables only the 37 reviewed statements.  'native_background'
    # additionally enables the separate 14 content statements, and requires
    # its own secondary proof digest.  There is no third capability.
    capability: str
    secondary_proof_sha256: object


_LOCK = threading.Lock()
_REGISTRY = {}


def _valid_digest(value):
    return isinstance(value, str) and _DIGEST.fullmatch(value) is not None


def _valid_caps(statement_ms, lock_ms):
    # The V15 content binding is the equal capped pair 10000/10000; control is
    # 2000/500.  Both are valid as long as lock never exceeds statement.
    return (type(statement_ms) is int and type(lock_ms) is int
            and 1 <= lock_ms <= statement_ms <= 30000)


def attest(*, kind, host, hostaddr, port, database, user, sslmode='verify-full',
           sslrootcert, ca_sha256, statement_ms, lock_ms, source_sha256, proof_sha256,
           capability='base', secondary_proof_sha256=None):
    """Build an immutable profile from trusted readback metadata.

    The caller is responsible for having verified the actual installed schema
    and actual connection metadata before calling this.  This function only
    rejects malformed metadata; it does not perform any proof itself.
    """
    if kind not in APPROVED_SQL_SHA256:
        raise ValueError('unsupported reuse kind')
    if not isinstance(host, str) or not host:
        raise ValueError('exact host required')
    if not isinstance(hostaddr, str) or not hostaddr:
        raise ValueError('exact hostaddr required')
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('exact port required')
    if not isinstance(database, str) or not database:
        raise ValueError('exact database required')
    if not isinstance(user, str) or not user:
        raise ValueError('exact user required')
    if sslmode != 'verify-full':
        raise ValueError('verify-full required')
    if not isinstance(sslrootcert, str) or not sslrootcert:
        raise ValueError('exact CA rootcert path required')
    if not _valid_digest(ca_sha256):
        raise ValueError('CA proof sha256 required')
    if not _valid_caps(statement_ms, lock_ms):
        raise ValueError('invalid capped pair')
    if not _valid_digest(source_sha256) or not _valid_digest(proof_sha256):
        raise ValueError('source and proof sha256 required')
    if capability not in ('base', 'native_background'):
        raise ValueError('unsupported reuse capability')
    if capability == 'native_background':
        if kind != 'content':
            raise ValueError('native background capability is content-only')
        if not _valid_digest(secondary_proof_sha256):
            raise ValueError('secondary native-background proof sha256 required')
    elif secondary_proof_sha256 is not None:
        raise ValueError('secondary proof requires native_background capability')
    return Profile(Binding(kind, host, hostaddr, port, database, user, sslmode,
                           sslrootcert, ca_sha256),
                   statement_ms, lock_ms, source_sha256, proof_sha256,
                   capability, secondary_proof_sha256)


def install_profile(profile):
    """Enable exactly one attested binding until the process exits/clears it.

    Call once from the trusted launcher, after readback and before Host
    startup.  There is intentionally no request/RPC entry point.
    """
    if not isinstance(profile, Profile):
        raise TypeError('attested Profile required')
    binding = profile.binding
    if (binding.kind not in APPROVED_SQL_SHA256 or binding.sslmode != 'verify-full'
            or not isinstance(binding.host, str) or not binding.host
            or not isinstance(binding.hostaddr, str) or not binding.hostaddr
            or type(binding.port) is not int or not 1 <= binding.port <= 65535
            or not isinstance(binding.database, str) or not binding.database
            or not isinstance(binding.user, str) or not binding.user
            or not isinstance(binding.sslrootcert, str) or not binding.sslrootcert
            or not _valid_digest(binding.ca_sha256)
            or not _valid_caps(profile.statement_ms, profile.lock_ms)
            or not _valid_digest(profile.source_sha256)
            or not _valid_digest(profile.proof_sha256)
            or profile.capability not in ('base', 'native_background')
            or (profile.capability == 'native_background'
                and (binding.kind != 'content'
                     or not _valid_digest(profile.secondary_proof_sha256)))
            or (profile.capability == 'base'
                and profile.secondary_proof_sha256 is not None)):
        raise ValueError('invalid attested profile')
    key = _binding_key(binding, profile.statement_ms, profile.lock_ms)
    with _LOCK:
        bucket = _REGISTRY.setdefault(key, [])
        if profile not in bucket:
            bucket.append(profile)


def _binding_key(binding, statement_ms, lock_ms):
    return (binding.kind, binding.host, binding.hostaddr, binding.port,
            binding.database, binding.user, binding.sslmode,
            binding.sslrootcert, statement_ms, lock_ms)


def clear_attested_profiles():
    """Remove all enablement; intended for fixture teardown and tests."""
    with _LOCK:
        _REGISTRY.clear()


def _binding_from_kwargs(kind, kwargs):
    if kind not in APPROVED_SQL_SHA256 or type(kwargs) is not dict:
        return None
    if set(kwargs) - _ALLOWED_CONNECT_KEYS:
        return None
    host, hostaddr, port = kwargs.get('host'), kwargs.get('hostaddr'), kwargs.get('port')
    database, user = kwargs.get('dbname'), kwargs.get('user')
    sslmode, root = kwargs.get('sslmode'), kwargs.get('sslrootcert')
    if not isinstance(host, str) or not host:
        return None
    if not isinstance(hostaddr, str) or not hostaddr:
        return None
    if type(port) is not int or not 1 <= port <= 65535:
        return None
    if not isinstance(database, str) or not database:
        return None
    if not isinstance(user, str) or not user:
        return None
    if sslmode != 'verify-full':
        return None
    if not isinstance(root, str) or not root:
        return None
    # ca_sha256 is known only to the attested profile, never to request kwargs.
    return Binding(kind, host, hostaddr, port, database, user, sslmode, root, '')


def profile_for(kind, kwargs, *, statement_ms, lock_ms, profile=None):
    """Return the matching immutable profile, or None for the original path."""
    try:
        binding = _binding_from_kwargs(kind, kwargs)
        if binding is None or not _valid_caps(statement_ms, lock_ms):
            return None
        if profile is not None:
            if (not isinstance(profile, Profile)
                    or _binding_key(profile.binding, statement_ms, lock_ms)
                    != _binding_key(binding, statement_ms, lock_ms)
                    or profile.statement_ms != statement_ms or profile.lock_ms != lock_ms
                    or not _valid_digest(profile.binding.ca_sha256)
                    or not _valid_digest(profile.source_sha256)
                    or not _valid_digest(profile.proof_sha256)
                    or profile.capability not in ('base', 'native_background')
                    or (profile.capability == 'native_background'
                        and (profile.binding.kind != 'content'
                             or not _valid_digest(profile.secondary_proof_sha256)))
                    or (profile.capability == 'base'
                        and profile.secondary_proof_sha256 is not None)):
                return None
            return profile
        key = _binding_key(binding, statement_ms, lock_ms)
        with _LOCK:
            candidates = tuple(_REGISTRY.get(key, ()))
        if len(candidates) != 1:
            # None (default off) or an ambiguous fixture install: both stay on
            # the original path rather than guessing which proof applies.
            return None
        return candidates[0]
    except Exception:
        return None


def approved_sql(kind, sql, profile=None):
    """True only for an exact reviewed SQL string permitted by the profile.

    The base 37 are always available for a matching base profile.  The 14
    native-background statements are content-only and require a profile that
    explicitly selected the secondary capability and supplied its separate
    proof digest.  A base-only profile can never use the extra 14.
    """
    # Exact built-in str only: a str subclass can override encode() and must
    # fall back to the ordinary setter path, never influence eligibility.
    if type(sql) is not str or kind not in APPROVED_SQL_SHA256:
        return False
    digest = hashlib.sha256(sql.encode('utf-8')).hexdigest()
    if digest in APPROVED_SQL_SHA256[kind]:
        return True
    return (profile is not None and kind == 'content'
            and getattr(profile, 'capability', None) == 'native_background'
            and _valid_digest(getattr(profile, 'secondary_proof_sha256', None))
            and digest in NATIVE_BACKGROUND_SQL_SHA256)
