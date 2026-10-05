"""SW-1510-SCOPE: WorkContract execution enforcement, boundary by boundary.

Four criteria, each proven at a *real* boundary rather than in a helper:

1. **Execution write scope** - a declared write that escapes the attempt
   checkout (``..`` traversal, a symlink whose target lies outside, an absolute
   outside path, or a receipt/store/telemetry root) is refused *before* the
   child is spawned; a legitimate allowed output child succeeds.
2. **Real isolation for all tools including Bash children** - a malicious child
   (file write, SQLite create) cannot write a sealed protected store at the
   kernel boundary; the harness *attests* the adapter it actually used; when the
   needed isolation is missing or insufficient the launch refuses with a typed
   HOLD instead of running unisolated.
3. **Mutation / turn-limit admission** - every mutation and turn-limit change
   checks the authenticated actor, the unchanged pinned subject/brief, the
   deadline and the remaining budget; negative limits are refused; a finite
   envelope is enforced while an explicit owner-authorized no-cumulative-limit
   envelope is honoured (never replaced by an artificial default).
4. **Generic object scopes** - content / configuration / deployment / incident
   scopes are supported and validated against their own fields, never by
   inventing a repository SHA.

Self-contained ``sys.path`` handling, following the module's sibling tests, so
it also runs under direct execution. ``tempfile.tempdir`` is pinned to pytest's
``tmp_path`` so the immutable-flag availability probe and every scratch file
stay off the host ``TMPDIR``.
"""

import sys
import tempfile
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import pytest  # noqa: E402

from skillweave.core.proc.runner import (  # noqa: E402
    ScopeEnforcement,
    ScopeEnforcementUnavailable,
    ScopeViolation,
    run_process,
)
from skillweave.dispatch.work_contract import (  # noqa: E402
    ConfigurationSubject,
    ContentSubject,
    DeploymentSubject,
    IncidentSubject,
    WorkContract,
    WriteScope,
)
from skillweave.runtime.assay import (  # noqa: E402
    AdmissionRefused,
    AssayAdmission,
    AssayEnvelope,
    AssayEngine,
    StaticActorResolver,
    TurnBudgetExhaustedError,
)
from skillweave.runtime.worker_launcher import (  # noqa: E402
    PROTECTED_STORE_NAMES,
    ProtectedStores,
    StoreProtection,
    select_adapter,
)
from skillweave.runtime.write_scope import (  # noqa: E402
    ExecutionScope,
    ObjectScopeBindingError,
    WriteScopeViolation,
    is_inside_root,
    resolve_under,
    scope_from_work_contract,
)

#: The strongest real adapter on this host. Tests that need kernel-enforced
#: write isolation are skipped when the host offers none (the HOLD tests still
#: run - they are exactly the "no real isolation" case).
_REAL_ADAPTER = select_adapter()
_HAS_REAL_ISOLATION = _REAL_ADAPTER.available()
requires_isolation = pytest.mark.skipif(
    not _HAS_REAL_ISOLATION,
    reason="host offers no real write-isolation adapter (chflags/sandbox-exec)",
)
#: A *scope-enforced service launch* additionally requires the adapter to
#: confine a process to the attempt checkout — a seal-only adapter locks the
#: protected stores but leaves every other outside path writable. On this host
#: the strongest adapter is the seal-only immutable-flag adapter (Seatbelt
#: cannot apply inside the test harness), so the confined-completion cases are
#: skipped and the HOLD cases run instead.
_HAS_CHECKOUT_CONFINEMENT = bool(getattr(_REAL_ADAPTER, "confines_to_scope", False))
requires_confinement = pytest.mark.skipif(
    not _HAS_CHECKOUT_CONFINEMENT,
    reason="host adapter does not confine a process to the attempt checkout",
)


@pytest.fixture(autouse=True)
def _pinned_tmp(tmp_path, monkeypatch):
    """Pin the ambient temp dir to pytest's tmp_path (never the host TMPDIR)."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path), raising=False)
    yield


# -- helpers -----------------------------------------------------------------


def _protected(tmp_path):
    roots = {
        name: (tmp_path / "protected" / name).resolve()
        for name in PROTECTED_STORE_NAMES
    }
    protected = ProtectedStores(roots)
    protected.ensure()
    return protected, roots


def _enforcement(tmp_path, *, allow=None, require_checkout_confinement=False):
    """A live ScopeEnforcement over a real attempt checkout + sealed stores."""
    checkout = (tmp_path / "attempt").resolve()
    checkout.mkdir(parents=True, exist_ok=True)
    protected, roots = _protected(tmp_path)
    protection = StoreProtection(
        select_adapter(),
        protected,
        require_checkout_confinement=require_checkout_confinement,
    )
    if allow is None:
        allow = [str(checkout)]
    scope = ExecutionScope(
        kind="paths", checkout_root=str(checkout), allow=list(allow)
    )
    enforcement = ScopeEnforcement(
        isolation=protection,
        scope=scope,
        protected_roots=[str(p) for p in protected.write_protected_paths()],
        checkout_root=str(checkout),
        require_checkout_confinement=require_checkout_confinement,
    )
    return enforcement, checkout, roots


def _py(code, *argv):
    return [sys.executable, "-c", code, *argv]


# -- Criterion 1: execution write scope --------------------------------------


def test_declared_write_outside_checkout_is_refused_before_launch(tmp_path):
    enforcement, checkout, _ = _enforcement(tmp_path)
    outside = str((tmp_path / "escape.txt").resolve())
    with pytest.raises(ScopeViolation) as exc:
        run_process(_py("print('must not run')"), enforcement=enforcement,
                    declared_writes=[outside])
    assert exc.value.target == outside
    # Nothing was written outside: the child never spawned.
    assert not Path(outside).exists()


def test_traversal_segment_is_refused_before_resolution(tmp_path):
    enforcement, checkout, _ = _enforcement(tmp_path)
    with pytest.raises((ScopeViolation, WriteScopeViolation)) as exc:
        enforcement.authorize_writes([str(checkout / ".." / "climb.txt")])
    assert "traversal" in str(exc.value)


def test_symlink_target_replacement_escape_is_refused(tmp_path):
    """A symlink inside the checkout pointing outside resolves to the real
    target and is refused; the refusal names that resolved outside path."""
    enforcement, checkout, _ = _enforcement(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    link = checkout / "link"
    link.symlink_to(outside)
    with pytest.raises(ScopeViolation) as exc:
        enforcement.authorize_writes([str(link)])
    assert exc.value.resolved == str(outside.resolve())


def test_protected_store_target_is_refused_even_inside_checkout(tmp_path):
    enforcement, checkout, roots = _enforcement(tmp_path)
    # A receipt/store root that also sits under an allowed checkout is still
    # refused: receipt/store/telemetry paths are never writable.
    target = str(roots["terminal_receipts"] / "forged.receipt")
    with pytest.raises(ScopeViolation) as exc:
        enforcement.authorize_writes([target])
    assert "protected store" in exc.value.reason


def test_legitimate_allowed_output_child_succeeds(tmp_path):
    enforcement, checkout, _ = _enforcement(tmp_path)
    out = checkout / "out.txt"
    rc, stdout, stderr = run_process(
        _py("import sys; open(sys.argv[1], 'w').write('ok'); print('wrote')",
            str(out)),
        enforcement=enforcement,
        declared_writes=[str(out)],
    )
    assert rc == 0, stderr
    assert b"wrote" in stdout.encode()
    assert out.read_text() == "ok"


def test_resolve_under_follows_symlinks_and_refuses_escape(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "sub").mkdir()
    (root / "sub" / "f").write_text("x")
    assert resolve_under(str(root), "sub/f") == str((root / "sub" / "f").resolve())
    assert resolve_under(str(root), "../escape") is None
    link = root / "link"
    (tmp_path / "target").write_text("y")
    link.symlink_to(tmp_path / "target")
    assert resolve_under(str(root), "link") is None
    assert is_inside_root(str(root), str(root / "sub"))


# -- Criterion 2: real isolation for all tools, typed HOLD when absent -------


@requires_isolation
def test_attestation_reports_the_real_adapter_and_covers_all_tools(tmp_path):
    enforcement, _, _ = _enforcement(tmp_path)
    attestation = enforcement.attest()
    assert attestation.adapter == _REAL_ADAPTER.name
    assert attestation.denies_writes is True
    assert attestation.isolates_all_tools is True
    assert list(attestation.tools) == ["Bash"]
    # Honest capability reporting: the immutable-flag adapter seals protected
    # stores but does not confine a process to the checkout tree.
    assert isinstance(attestation.confines_to_scope, bool)


@requires_isolation
def test_malicious_child_cannot_write_a_sealed_protected_store(tmp_path):
    enforcement, _, roots = _enforcement(tmp_path)
    victim = roots["terminal_receipts"] / "run.host"
    victim.write_text("host-owned")
    code = "import sys; open(sys.argv[1], 'w').write('tampered'); print('tampered')"
    rc, stdout, stderr = run_process(_py(code, str(victim)), enforcement=enforcement)
    assert rc != 0, "the sealed store accepted a child write"
    assert "tampered" not in stdout
    assert victim.read_text() == "host-owned"


@requires_isolation
def test_malicious_sqlite_child_cannot_create_a_protected_store_db(tmp_path):
    enforcement, _, roots = _enforcement(tmp_path)
    evil_db = roots["authority_records"] / "evil.db"
    code = (
        "import sqlite3, sys; c = sqlite3.connect(sys.argv[1]); "
        "c.execute('create table t(x)'); c.commit(); print('created')"
    )
    rc, stdout, stderr = run_process(_py(code, str(evil_db)), enforcement=enforcement)
    assert rc != 0, "SQLite created a DB inside a sealed store"
    assert not evil_db.exists()


def test_missing_isolation_raises_a_typed_hold(tmp_path):
    checkout = tmp_path / "attempt"
    checkout.mkdir()
    scope = ExecutionScope(kind="paths", checkout_root=str(checkout))
    enforcement = ScopeEnforcement(isolation=None, scope=scope, require_isolation=True)
    with pytest.raises(ScopeEnforcementUnavailable) as exc:
        enforcement.attest()
    hold = exc.value.to_hold()
    assert hold["kind"] == "HOLD"
    assert hold["reason"] == "isolation_unavailable"
    assert hold["workload"] == "generic"


@requires_isolation
def test_insufficient_checkout_confinement_raises_a_typed_hold(tmp_path):
    """When checkout confinement is required but the adapter only seals
    protected stores, the launch is refused (HOLD), not downgraded."""
    if _REAL_ADAPTER.confines_to_scope:
        pytest.skip("host adapter already confines to the checkout")
    enforcement, _, _ = _enforcement(tmp_path, require_checkout_confinement=True)
    with pytest.raises(ScopeEnforcementUnavailable) as exc:
        enforcement.attest()
    assert "confine" in exc.value.reason


def test_no_isolation_never_launches_unisolated(tmp_path):
    """A missing adapter fails closed: the child is never spawned."""
    checkout = tmp_path / "attempt"
    checkout.mkdir()
    scope = ExecutionScope(kind="paths", checkout_root=str(checkout))
    enforcement = ScopeEnforcement(isolation=None, scope=scope, require_isolation=True)
    marker = tmp_path / "spawned"
    with pytest.raises(ScopeEnforcementUnavailable):
        run_process(_py("open(sys.argv[1], 'w').write('x')", str(marker)),
                    enforcement=enforcement)
    assert not marker.exists()


# -- Criterion 3: mutation / turn-limit admission ----------------------------


def _assay(max_turns=10, envelope=None, tokens=None):
    resolver = StaticActorResolver(tokens or {"good": "alice", "bad": ""})
    engine = AssayEngine(
        max_turns=max_turns, run_id="run-a", actor_resolver=resolver,
        envelope=envelope,
    )
    engine.pin(subject="subj-1", brief="brief-1")
    return engine


def _admission(engine, token="good", subject=None, brief=None, envelope=None):
    return AssayAdmission(
        actor_token=token,
        subject=subject if subject is not None else engine.subject_digest,
        brief=brief if brief is not None else engine.brief_digest,
        envelope=envelope or engine.envelope,
    )


def test_negative_turn_limit_is_refused():
    with pytest.raises(AdmissionRefused) as exc:
        AssayEngine(max_turns=-1, run_id="r")
    assert exc.value.axis == "turn_limit"


def test_negative_remaining_budget_is_refused():
    with pytest.raises(AdmissionRefused) as exc:
        AssayEnvelope(remaining_budget=-1)
    assert exc.value.axis == "envelope"


def test_negative_turn_limit_change_is_refused():
    engine = _assay()
    with pytest.raises(AdmissionRefused) as exc:
        engine.set_turn_limit(-5, admission=_admission(engine))
    assert exc.value.axis == "turn_limit"


def test_mutation_requires_an_authenticated_actor():
    engine = _assay()
    with pytest.raises(AdmissionRefused) as exc:
        engine.advance_turn(admission=_admission(engine, token="bad"))
    assert exc.value.axis == "actor"
    assert engine.current_turn == 0


def test_subject_and_brief_are_pinned_across_extension():
    engine = _assay()
    with pytest.raises(AdmissionRefused) as exc:
        engine.advance_turn(admission=_admission(engine, subject="swapped"))
    assert exc.value.axis == "subject"
    with pytest.raises(AdmissionRefused) as exc:
        engine.advance_turn(admission=_admission(engine, brief="swapped"))
    assert exc.value.axis == "brief"


def test_repin_that_differs_is_refused():
    engine = _assay()
    with pytest.raises(AdmissionRefused) as exc:
        engine.pin(subject="other", brief="brief-1")
    assert exc.value.axis == "pin"


def test_deadline_is_enforced():
    engine = _assay(
        envelope=AssayEnvelope(deadline="2000-01-01T00:00:00+00:00")
    )
    with pytest.raises(AdmissionRefused) as exc:
        engine.advance_turn(admission=_admission(engine))
    assert exc.value.axis == "deadline"


def test_finite_remaining_budget_is_enforced():
    envelope = AssayEnvelope(remaining_budget=1)
    engine = _assay(max_turns=None, envelope=envelope)
    engine.advance_turn(admission=_admission(engine))  # turn 1 (allowed)
    with pytest.raises(AdmissionRefused) as exc:
        engine.advance_turn(admission=_admission(engine))
    assert exc.value.axis == "budget"


def test_no_cumulative_limit_envelope_is_honoured_not_defaulted():
    """An explicit max_turns=None envelope allows productive work: turns are
    not stopped by an artificial default ceiling."""
    engine = _assay(max_turns=None)
    for _ in range(50):
        engine.advance_turn(admission=_admission(engine))
    assert engine.current_turn == 50


def test_no_cumulative_limit_change_is_recorded_with_actor_and_kind():
    engine = _assay()
    record = engine.set_turn_limit(None, admission=_admission(engine))
    assert record.max_turns is None
    assert record.kind == "no_cumulative_limit"
    assert record.actor == "alice"


def test_unauthorized_actor_cannot_extend_the_turn_bound():
    engine = _assay()
    with pytest.raises(AdmissionRefused) as exc:
        engine.set_turn_limit(999, admission=_admission(engine, token="bad"))
    assert exc.value.axis == "actor"
    assert engine.max_turns == 10


def test_turn_budget_exhaustion_still_stops_a_finite_engine():
    engine = _assay(max_turns=2)
    engine.advance_turn(admission=_admission(engine))
    engine.advance_turn(admission=_admission(engine))
    with pytest.raises(TurnBudgetExhaustedError):
        engine.advance_turn(admission=_admission(engine))


# -- Criterion 4: generic object scopes without a fabricated SHA -------------


@pytest.mark.parametrize(
    "subject_cls,fields",
    [
        (ContentSubject, {"channel": "web", "content_id": "c-1",
                          "revision": "rev-9"}),
        (ConfigurationSubject, {"config_id": "cfg-1", "environment": "prod",
                                "version": "v3"}),
        (DeploymentSubject, {"target": "cluster-a", "environment": "prod",
                             "artifact_digest": "sha256:deadbeef"}),
        (IncidentSubject, {"incident_id": "inc-7", "state": "mitigating",
                           "severity": "sev2"}),
    ],
)
def test_object_scope_binds_its_own_fields_without_a_commit(subject_cls, fields):
    subject = subject_cls(**fields)
    contract = WorkContract(
        id="wc-1",
        subject=subject,
        authority="ops",
        write_scope=WriteScope(kind=subject.kind, allow=[subject.kind]),
    )
    scope = scope_from_work_contract(contract)
    assert scope.kind == subject.kind
    assert scope.repository_commit is None
    binding = scope.validate_object()
    assert binding["repository_commit"] is None
    for name, value in fields.items():
        assert binding["bindings"][name] == value


def test_fabricated_repository_commit_on_an_object_scope_is_refused():
    scope = ExecutionScope(
        kind="deployment",
        subject={
            "kind": "deployment",
            "target": "cluster-a",
            "environment": "prod",
            "artifact_digest": "sha256:abc",
        },
        repository_commit="deadbeef",  # not a full 40-hex commit
    )
    with pytest.raises(ObjectScopeBindingError):
        scope.validate_object()


def test_object_scope_missing_field_is_refused():
    scope = ExecutionScope(
        kind="configuration",
        subject={"kind": "configuration", "config_id": "cfg-1", "environment": "prod"},
    )
    with pytest.raises(ObjectScopeBindingError):
        scope.validate_object()


def test_object_scope_rejects_a_path_style_kind():
    scope = ExecutionScope(kind="paths", checkout_root="/tmp")
    with pytest.raises(WriteScopeViolation):
        scope.validate_object()


# -- The real harness boundary: RunApplicationService wiring -----------------


def _service(tmp_path):
    from types import SimpleNamespace

    from skillweave.runsvc import RunApplicationService
    from skillweave.runtime.journal import EventJournal
    from skillweave.runtime.registry import RawArtifactStore
    from skillweave.runtime.store import SQLiteRunStore

    db = str((tmp_path / "runs.db").resolve())
    store = SQLiteRunStore(db)
    journal = EventJournal(store)
    raw = RawArtifactStore()
    service = RunApplicationService(store, journal, raw)

    class _Auth:
        def __init__(self, run_id):
            self._run_id = run_id

        def verify_credential(self, token):
            return SimpleNamespace(roles=set(), attempt_key=(self._run_id, "a"))

    return service, store, db, _Auth


def test_service_requires_identity_for_scope_enforced_execution(tmp_path):
    from skillweave.runsvc import RunIntegrationError

    service, store, _, _ = _service(tmp_path)
    with pytest.raises(RunIntegrationError) as exc:
        service.execute(
            _py("print('x')"),
            run_id="run-plain",
            tool="t",
            model="m",
            subject_repo="r",
            subject_commit="c",
            declared_writes=["out.txt"],
        )
    assert exc.value.stage == "identity"


def test_service_refuses_a_traversal_declared_write(tmp_path):
    service, store, _, Auth = _service(tmp_path)
    with pytest.raises((ScopeViolation, WriteScopeViolation)):
        service.execute(
            _py("print('must not run')"),
            run_id="run-esc",
            tool="t",
            model="m",
            subject_repo="r",
            subject_commit="c",
            authority_service=Auth("run-esc"),
            worker_token="tok",
            declared_writes=["../escape.txt"],
        )


@requires_confinement
def test_service_authenticated_scoped_run_completes(tmp_path):
    service, store, _, Auth = _service(tmp_path)
    result = service.execute(
        _py("print('scoped-ok')"),
        run_id="run-ok",
        tool="t",
        model="m",
        subject_repo="r",
        subject_commit="c",
        authority_service=Auth("run-ok"),
        worker_token="tok",
        declared_writes=["out.txt"],
    )
    assert result.gate_state == "pass"
    assert b"scoped-ok" in result.raw_bytes


@requires_confinement
def test_service_releases_the_seal_after_a_refused_launch(tmp_path):
    """A traversal refusal must not leak the protection window: a subsequent
    run still writes the host store (a leaked seal would lock SQLite)."""
    service, store, _, Auth = _service(tmp_path)
    with pytest.raises((ScopeViolation, WriteScopeViolation)):
        service.execute(
            _py("print('x')"),
            run_id="run-refused",
            tool="t",
            model="m",
            subject_repo="r",
            subject_commit="c",
            authority_service=Auth("run-refused"),
            worker_token="tok",
            declared_writes=["../escape.txt"],
        )
    result = service.execute(
        _py("print('after')"),
        run_id="run-after",
        tool="t",
        model="m",
        subject_repo="r",
        subject_commit="c",
        authority_service=Auth("run-after"),
        worker_token="tok",
        declared_writes=["out.txt"],
    )
    assert result.gate_state == "pass"


@requires_isolation
def test_service_denies_an_undeclared_protected_store_write(tmp_path):
    """Even a real child that ignores its declared scope cannot tamper with a
    protected store: the kernel denies it and the run is a non-pass.

    Runs on the *identity-only* path (no declared write scope), so the original
    SW-1510-IDENTITY guarantee — the protected authority stores sealed for the
    child — is exercised with the host's strongest adapter, which on a
    non-Seatbelt host is the seal-only immutable-flag adapter.
    """
    service, store, db, Auth = _service(tmp_path)
    victim = str(Path(db).resolve())  # the run authority DB itself
    code = "import sys; open(sys.argv[1], 'a').write('x'); print('tampered')"
    result = service.execute(
        _py(code, victim),
        run_id="run-tamper",
        tool="t",
        model="m",
        subject_repo="r",
        subject_commit="c",
        authority_service=Auth("run-tamper"),
        worker_token="tok",
    )
    assert b"tampered" not in result.raw_bytes
    assert result.gate_state != "pass"


def test_service_raises_a_typed_hold_when_no_real_isolation(tmp_path, monkeypatch):
    """When no real isolation adapter exists, the scope-enforced launch refuses
    with a typed HOLD before the child is spawned - never an unisolated run."""
    from skillweave.runtime import worker_launcher

    monkeypatch.setattr(
        worker_launcher, "select_adapter",
        lambda preferred=None: worker_launcher.FailClosedAdapter(reason="test"),
    )
    service, store, _, Auth = _service(tmp_path)
    marker = tmp_path / "spawned"
    with pytest.raises(ScopeEnforcementUnavailable) as exc:
        service.execute(
            _py("open(sys.argv[1], 'w').write('x')", str(marker)),
            run_id="run-hold",
            tool="t",
            model="m",
            subject_repo="r",
            subject_commit="c",
            authority_service=Auth("run-hold"),
            worker_token="tok",
            declared_writes=["out.txt"],
        )
    assert exc.value.to_hold()["reason"] == "isolation_unavailable"
    assert not marker.exists()


def test_service_refuses_a_scope_launch_without_checkout_confinement(tmp_path):
    """Automatic required checkout confinement (criterion 2): a scope-enforced
    launch on an adapter that only seals the protected authority stores HOLDs
    *before* the child is spawned - it is never launched as an unconfined run.

    This is the fix for the counterproof in which a supported seal-only adapter
    let a real child write an undeclared file outside the attempt checkout. The
    caller sets no hidden flag; the requirement is the launch's own.
    """
    if _HAS_CHECKOUT_CONFINEMENT:
        pytest.skip("host adapter already confines to the checkout")
    if not _HAS_REAL_ISOLATION:
        pytest.skip("no real adapter at all (the typed-hold case covers that)")
    service, store, _, Auth = _service(tmp_path)
    marker = tmp_path / "undeclared-escape.txt"
    with pytest.raises(ScopeEnforcementUnavailable) as exc:
        service.execute(
            _py("open(sys.argv[1], 'w').write('escaped')", str(marker)),
            run_id="run-unconfined",
            tool="t",
            model="m",
            subject_repo="r",
            subject_commit="c",
            authority_service=Auth("run-unconfined"),
            worker_token="tok",
            declared_writes=["out.txt"],
        )
    assert exc.value.to_hold()["reason"] == "isolation_unavailable"
    assert "checkout" in exc.value.reason
    # The refused launch never ran: no undeclared file was created outside.
    assert not marker.exists()
    # And the seal was released, so the host store is still writable.
    assert store.get_run("run-unconfined") is not None


@requires_confinement
def test_service_enforces_a_generic_object_contract(tmp_path):
    """A content work contract runs through the real boundary and is validated
    against its own fields - no repository SHA is invented."""
    from skillweave.dispatch.work_contract import WriteScope as _WS

    service, store, _, Auth = _service(tmp_path)
    contract = WorkContract(
        id="wc-content",
        subject=ContentSubject(channel="web", content_id="c-1", revision="rev-9"),
        authority="ops",
        write_scope=_WS(kind="content", allow=["content"]),
    )
    result = service.execute(
        _py("print('content-ok')"),
        run_id="run-content",
        tool="t",
        model="m",
        subject_repo="r",
        subject_commit="c",
        authority_service=Auth("run-content"),
        worker_token="tok",
        work_contract=contract,
    )
    assert result.gate_state == "pass"
    assert b"content-ok" in result.raw_bytes


# -- Criterion 3 (native service path): guarded admission drives the drill ---


def _real_service(tmp_path):
    """A service plus a live HostAuthorityService, on a real SQLite run DB."""
    from types import SimpleNamespace  # noqa: F401

    from skillweave.runsvc import RunApplicationService
    from skillweave.runtime.authority_service import HostAuthorityService
    from skillweave.runtime.journal import EventJournal
    from skillweave.runtime.registry import RawArtifactStore
    from skillweave.runtime.store import SQLiteRunStore

    db = str((tmp_path / "authority-runs.db").resolve())
    store = SQLiteRunStore(db)
    service = RunApplicationService(store, EventJournal(store), RawArtifactStore())
    return service, HostAuthorityService()


def _worker_credential(authority, *, run_id, task_id, attempt):
    return authority.issue_attempt_credential(
        run_id=run_id, task_id=task_id, attempt=attempt, scope={"writes": []}
    )


def test_service_assay_run_enforces_admission_and_records_turn_limit(tmp_path):
    """A native Assay run on the identity-authenticated path binds the real
    host authority as its actor and records the owner-authorized envelope's
    turn-limit change under that authenticated actor."""
    from skillweave.runtime.assay import AssayEnvelope

    service, authority = _real_service(tmp_path)
    token = _worker_credential(authority, run_id="auth-assay", task_id="t-1", attempt=1)
    envelope = AssayEnvelope(
        deadline="2999-01-01T00:00:00Z", remaining_budget=50, require_actor=True
    )
    result = service.execute(
        _py("print('guarded-assay')"),
        run_id="auth-assay",
        tool="t",
        model="m",
        subject_repo="r",
        subject_commit="c",
        methodology="assay",
        autonomy_policy="unicorn",
        task_id="t-1",
        attempt=1,
        authority_service=authority,
        worker_token=token,
        assay_actor_token=token,
        assay_envelope=envelope,
    )
    assert result.assay["outcome"] == "pass"
    assert [p["phase"] for p in result.assay["phases"]] == [
        "pin", "hypothesis", "probe", "measure", "classify", "pass"
    ]


def test_service_assay_run_refuses_an_expired_authority_deadline(tmp_path):
    """An expired authority deadline refuses *before* the real probe is
    spawned, and the failure is durable rather than leaving a dangling run."""
    from skillweave.runtime.assay import AssayEnvelope

    service, authority = _real_service(tmp_path)
    store = service.store
    token = _worker_credential(authority, run_id="auth-exp", task_id="t-1", attempt=1)
    marker = tmp_path / "must-not-run"
    with pytest.raises(AdmissionRefused) as exc:
        service.execute(
            _py("import sys; open(sys.argv[1], 'w').write('ran')", str(marker)),
            run_id="auth-exp",
            tool="t",
            model="m",
            subject_repo="r",
            subject_commit="c",
            methodology="assay",
            autonomy_policy="unicorn",
            task_id="t-1",
            attempt=1,
            authority_service=authority,
            worker_token=token,
            assay_actor_token=token,
            assay_envelope=AssayEnvelope(
                deadline="2000-01-01T00:00:00Z", require_actor=True
            ),
        )
    assert exc.value.axis == "deadline"
    assert not marker.exists()
    # Durable, not dangling: the run reached a terminal state.
    assert store.get_run("auth-exp").state in ("advance_or_stop", "failed")


def test_service_assay_run_refuses_a_wrong_attempt_actor(tmp_path):
    """An actor credential bound to a different run/task/attempt cannot drive
    the drill: the host authority refuses it at admission."""
    from skillweave.runtime.assay import AssayEnvelope

    service, authority = _real_service(tmp_path)
    wrong = _worker_credential(authority, run_id="other-run", task_id="t-9", attempt=7)
    with pytest.raises(Exception) as exc:
        service.execute(
            _py("print('never')"),
            run_id="auth-swap",
            tool="t",
            model="m",
            subject_repo="r",
            subject_commit="c",
            methodology="assay",
            autonomy_policy="unicorn",
            task_id="t-1",
            attempt=1,
            authority_service=authority,
            worker_token=wrong,
            assay_actor_token=wrong,
            assay_envelope=AssayEnvelope(require_actor=True),
        )
    # Refused before launch (identity binds token to run at ingress).
    assert not isinstance(exc.value, AssertionError)


def test_service_assay_run_legacy_path_needs_no_actor(tmp_path):
    """The unchanged in-process Assay path (no authority service) still runs,
    with no admission and no actor requirement."""
    service, store, _, _ = _service(tmp_path)
    result = service.execute(
        _py("print('legacy-assay')"),
        run_id="legacy-assay",
        tool="t",
        model="m",
        subject_repo="r",
        subject_commit="c",
        methodology="assay",
        autonomy_policy="unicorn",
    )
    assert result.assay["outcome"] == "pass"
    assert [p["phase"] for p in result.assay["phases"]][:5] == [
        "pin", "hypothesis", "probe", "measure", "classify"
    ]
