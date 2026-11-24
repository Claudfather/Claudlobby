"""Explicit local-console owner administration, separate from browser authority.

An operator terminal is a usability/accident boundary, not same-UID isolation.
Before it opens, a change runs the operator-context check that the other
operator-only commands run (native unit ancestry on an activated host).
Processes with the operator's privileges can edit the same authority files.
These doors never grant website membership. Ordinary messages require an
explicit local actor/fleet approval separate from read pairing.
"""

from contextlib import closing, contextmanager
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import getpass
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import warnings
from types import SimpleNamespace

from ..command_result import CommandFailure, CommandOutput
from ..plane.owner_access import ACTION_DENIALS, AccessDenied, AccessUnavailable, OwnerAccess
from ..plane.owner_consent import (WorkspaceConsentChanged, WorkspaceConsentDeclined,
                                   apply_workspace_binding, preview_workspace_binding,
                                   require_workspace_expectation)
from ..plane.owner_source import SourceDenied, SourceUnavailable
from ..plane.owner_observation import owner_pairing_state, source_refusal
from .owner_terminal_consent import TerminalWorkspacePresenter, approve as _approve

_GENERATED = ("BOT_ID", "BOT_NAME", "BOT_DIR", "BOT_SERVICE", "FLEET_ROOT",
              "FLEET_NAME", "CLAUDLOBBY_FLEET", "CLAUDLOBBY_TIMER_CONTEXT")
_ACTION_LABELS = {
    "message": ("ordinary messages", "ordinary-message", "ALLOW", "REVOKE-MESSAGES"),
    "nudge": ("task nudges", "task-nudge", "ALLOW-NUDGES", "REVOKE-NUDGES"),
    "feedback": ("task feedback", "task-feedback", "ALLOW-FEEDBACK", "REVOKE-FEEDBACK"),
    "permission": ("permission decisions", "permission-decision", "ALLOW-PERMISSIONS", "REVOKE-PERMISSIONS"),
}


def _say(terminal, text):
    if terminal is not None:
        terminal.write(text)


def _consent(terminal, word, consent, action, scope, *, expires_at=None):
    """The guide's answer (``True``, or a card's typed value), or ``None`` at a terminal."""
    if consent is None:
        _approve(terminal, word)
        return None
    scope = {key: asdict(value) if is_dataclass(value) else value for key, value in scope.items()}
    return consent.approve(action, scope, expires_at=expires_at)


# The team card's second effect when it carries the messages mark (string S5).
_CARD_MESSAGES_EFFECT = ("messages to this team from your workspace under the name on this card, numbered if taken, "
                         "once the team is running; the launched goal sent once to the lead, which starts work on it without you; "
                         "no GitHub access")


def _card_finish_ready():
    """The team card carries the messages mark only while the finish that honours it exists."""
    from . import host_owner_setup
    return callable(getattr(host_owner_setup, "_card_messages", None))


# The rule (owner-approved wording, D14a): Owner changes require an interactive operator terminal, or
# the setup guide with its page's approvals. One exception: the finish of a Launch whose team card was
# approved with the messages mark may run without a terminal. It applies only what that card approved
# (messages on for that team under the card's name, and the card's goal sent once to the lead) and asks
# for nothing else.
#
# The threat model above stands: an operator terminal is a usability/accident boundary, not same-UID
# isolation, and processes with the operator's privileges can edit the same authority files. So any
# process of this account may start that one finish (``_unattended_finish``); the operator gate does
# not keep the account's own processes out. What the finish does is fixed by the stored card: it runs
# once under its lock and re-checks the approval, binding, grant and goal as it acts.
@contextmanager
def _terminal(root):
    if not sys.stdin.isatty():
        raise CommandFailure("conflict", "owner changes require an interactive operator terminal")
    from .operator_context import require_operator_context
    from ..resources import get_resources
    package = get_resources()
    if package.installation is not None and not os.path.lexists(root / "state/config.json"):
        # Cold ancestry has no declared consumers; positively prove the fresh
        # root before using the same cold operator gate as fleet setup.
        from ..plane.owner_source import fresh_owner_setup
        fresh_owner_setup(root, allow_absent=True)
        require_operator_context()
    else:
        require_operator_context(root)
    try:
        # A terminal is not seekable: buffered r+ would require seeks between
        # reading and writing. FileIO keeps this duplex console unbuffered.
        with io.TextIOWrapper(io.FileIO("/dev/tty", "r+"), encoding="utf-8",
                              line_buffering=True) as terminal:
            if not terminal.isatty():
                raise OSError
            yield terminal
    except (OSError, EOFError, KeyboardInterrupt) as exc:
        raise CommandFailure("conflict", "owner confirmation did not complete; inspect owner status") from exc


@contextmanager
def _unattended_finish(root):
    """The rule's one exception: ``setup --_finish-setup`` with no terminal (the computer's own setup
    waiter, after a marked Launch). The same operator gate as a terminal, on the selected root; no
    terminal is opened. ``_completed`` then refuses anything but a card-approved Launch's finish."""
    from .operator_context import require_operator_context
    require_operator_context(root)
    yield None


def _challenge(terminal, *, label="browser pairing"):
    # getpass must never fall back to echoing a challenge into redirected input.
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass(f"Paste the {label} code (hidden): ", stream=terminal)
        except getpass.GetPassWarning as exc:
            raise CommandFailure("conflict", "a terminal with hidden input is required") from exc


def _describe(terminal, grant):
    # JSON escapes terminal control characters, bidi controls and non-ASCII.
    _say(terminal, "Host: " + json.dumps(grant.host_uid) + "\n")
    _say(terminal, "Principal: " + json.dumps(asdict(grant.principal), ensure_ascii=True) + "\n")
    _say(terminal, f"Owner revision: {grant.revision}\n")


def _active_owner(store):
    owner = store.current_grant()
    if owner is None or not owner.active:
        raise CommandFailure("conflict", "an active owner pairing is required")
    return owner


def _grant_preview(paths, store, fleet, actor_alias, release):
    """Read current frozen/recorded identities; never register at preview."""
    from ..activation_identity import read_current_identity_bindings
    from ..operation_context import _MissingHumanIdentity, bind_task_context, resolve_operation_scope

    owner = _active_owner(store)
    selected, origin = resolve_operation_scope(root=paths.root, fleet=fleet, package=paths.package)
    if origin is not None:
        raise CommandFailure("conflict", "owner grants require a local operator")
    bindings = read_current_identity_bindings(paths.root, selected.fleet.name, package=paths.package)
    ctx = None
    try:
        ctx = bind_task_context(selected, operator_alias=actor_alias)
    except _MissingHumanIdentity:
        # The binder checked host/fleet/bots before this sole absence. Compare
        # recorded IDs to activation too before approving any registration.
        from ..plane.db import connect_ro, db_file
        from ..plane.identity import lookup
        from ..plane.schema_state import require_current_schema
        with closing(connect_ro(db_file(paths.root))) as conn:
            conn.execute("BEGIN")
            require_current_schema(conn)
            if (lookup(conn, "fleet", selected.fleet.name) != bindings["fleet_uid"]
                    or any(lookup(conn, "actor", f"bot:{selected.fleet.name}/{name}") != uid
                           for name, uid in bindings["bots"].items())):
                raise CommandFailure("conflict", "active and recorded owner identities differ")
    if (owner.host_uid != bindings["host_uid"] or ctx is not None and (
            ctx.host_uid != bindings["host_uid"] or ctx.fleet_uid != bindings["fleet_uid"]
            or {name: bot.uid for name, bot in ctx.bots.items()} != bindings["bots"])):
        raise CommandFailure("conflict", "active and recorded owner identities differ")
    from ..runtime_admission import GitCurrentRuntime
    provenance = ({"revision": release.revision, "config_fingerprint": release.config_fingerprint}
                  if isinstance(release, GitCurrentRuntime) else {"release": release.release_id})
    return {"owner": owner, **provenance, "fleet": selected.fleet.name,
            "bindings": bindings, "actor_alias": actor_alias,
            "actor_uid": ctx.caller.uid if ctx is not None else None}


def _describe_binding(terminal, fleet_uid, actor_alias, actor_uid):
    _say(terminal, "Fleet UID: " + json.dumps(fleet_uid) + "\n")
    _say(terminal, "Actor: " + json.dumps(actor_alias, ensure_ascii=True) + "\n")
    _say(terminal, "Actor UID: " + json.dumps(actor_uid) + "\n")


def _describe_action(terminal, preview):
    _describe(terminal, preview["owner"])
    _say(terminal, "Fleet: " + json.dumps(preview["fleet"]) + "\n")
    _describe_binding(terminal, preview["bindings"]["fleet_uid"], preview["actor_alias"], preview["actor_uid"])


def _unchanged(before, after):
    if before != after:
        raise CommandFailure("conflict", "displayed owner or actor identities changed; inspect and retry")


def _prepare_action_approval(args, paths, store, terminal, *, consent=None):
    from ..operation_context import _valid_human_alias, resolve_task_mutation_context
    from ..runtime_admission import RuntimeIdentity, mutation_admission

    if not _valid_human_alias(args.actor):
        raise CommandFailure("invalid_argument", "--actor must be a canonical local human: alias")
    with mutation_admission(paths.root, identity=RuntimeIdentity.current()) as release:
        preview = _grant_preview(paths, store, args.target_fleet, args.actor, release)
    if preview["actor_uid"] is None:
        if not args.register_actor:
            raise CommandFailure("conflict", "human actor is not registered; explicitly use --register-actor")
        _describe_action(terminal, preview)
        _say(terminal, "Register this local actor's first contact. Its UID is allocated only after approval.\n"
                       "Registration alone does not grant owner actions.\n")
        _consent(terminal, "REGISTER", consent, "register_actor", preview)
        preview = _register_actor(paths, store, args.target_fleet, args.actor, preview)
    return preview


def _register_actor(paths, store, fleet, alias, preview, check=None):
    """Register one local human's first contact, bound to an unregistered ``preview``; no consent here.
    ``check()``, when given, runs inside the registration's admission just before the name is written
    and refuses by raising."""
    from ..operation_context import resolve_task_mutation_context
    from ..runtime_admission import RuntimeIdentity, mutation_admission

    with mutation_admission(paths.root, identity=RuntimeIdentity.current(),
                            expected_release=preview.get("release")) as release:
        _unchanged(preview, _grant_preview(paths, store, fleet, alias, release))
        if check is not None:
            check()
        resolve_task_mutation_context(root=paths.root, fleet=preview["fleet"],
                                      operator_alias=alias, package=paths.package)
        registered = _grant_preview(paths, store, fleet, alias, release)
        if registered["actor_uid"] is None:
            raise CommandFailure("conflict", "approved actor registration is unavailable")
        _unchanged(preview, {**registered, "actor_uid": None})
        return registered


def _approved_action_context(args, paths, store, preview, release):
    from ..operation_context import resolve_task_context

    _unchanged(preview, _grant_preview(paths, store, args.target_fleet, args.actor, release))
    ctx = resolve_task_context(root=paths.root, fleet=preview["fleet"],
                               operator_alias=args.actor, package=paths.package)
    if (ctx.host_uid != preview["owner"].host_uid or ctx.fleet_uid != preview["bindings"]["fleet_uid"]
            or ctx.caller.uid != preview["actor_uid"] or ctx.caller.alias != preview["actor_alias"]):
        raise CommandFailure("conflict", "approved actor identities changed")
    return ctx



def _setup_messages(paths, store, terminal, fleet, *, consent=None):
    """The existing messages-only local consent, not future/browser authority."""
    if consent is not None:
        from .owner_stdio import StdioRefused
        try:
            alias = consent.approve("actor_alias", {"fleet": fleet,
                "effects": ["select a local human alias; no grant or message"]})
        except StdioRefused as exc:
            if exc.code == "declined":
                return {"state": "skipped"}
            raise
        args = SimpleNamespace(target_fleet=fleet, actor=alias, register_actor=True, capabilities=("messages",))
        try:
            with _grant_errors("actions"):
                _allow_actions(args, paths, store, None, consent=consent)
        except StdioRefused as exc:
            if exc.code == "declined":
                return {"state": "skipped"}
            raise
        return {"state": "allowed"}
    terminal.write("Optional: allow ordinary messages to this team. Enter the name you want to use for messages (for example human:me), or press Enter / type skip to finish without a new approval: ")
    terminal.flush()
    try:
        answer = terminal.readline(258)
    except (EOFError, KeyboardInterrupt):
        answer = ""

    def skipped():
        terminal.write("No new message approval was completed. Existing approvals were not revoked; actor registration alone grants no messages.\n")
        return {"state": "skipped"}

    if not answer.endswith("\n") or answer.strip() in ("", "skip"):
        return skipped()
    args = SimpleNamespace(target_fleet=fleet, actor=answer.strip(), register_actor=True,
                           capabilities=("messages",))

    def confirmation(size=-1):
        try:
            return terminal.readline(size)
        except (EOFError, KeyboardInterrupt):
            raise CommandFailure("conflict", "owner change cancelled") from None

    proxy = SimpleNamespace(write=terminal.write, flush=terminal.flush, readline=confirmation)
    try:
        with _grant_errors("actions"):
            _allow_actions(args, paths, store, proxy)
    except CommandFailure as exc:
        # Only a declined/unfinished confirmation is a skip. Storage failures or
        # interrupted effects remain errors/unknown and are never retried here.
        if exc.error.code == "conflict" and str(exc) == "owner change cancelled":
            return skipped()
        raise
    terminal.write("Messages approved for this team. Select it in your browser and choose Check action access. Nothing was sent.\n")
    return {"state": "allowed"}


def _post_setup_handoff(paths, result):
    from ..installation import setup_handoff_argv, InstallationError
    try:
        argv = setup_handoff_argv(paths.package.installation, result.data)
        for line in result.lines:
            print(line, flush=True)
        os.execve(argv[0], argv, dict(os.environ))
    except (OSError, InstallationError):
        raise CommandFailure("unavailable", "automatic setup completion is unavailable; keep the original operation", data=result.data,
            hint="Inspect host update status, continue only that original operation if needed, then reopen host owner setup after healthy DONE; do not launch again") from None


def _allow_actions(args, paths, store, terminal, *, consent=None):
    from ..runtime_admission import RuntimeIdentity, mutation_admission

    if len(set(args.capabilities)) != len(args.capabilities):
        raise CommandFailure("invalid_argument", "--capabilities must name each selected action only once")
    preview = _prepare_action_approval(args, paths, store, terminal, consent=consent)
    _describe_action(terminal, preview)
    _say(terminal, "Approve only these capabilities for this actor and fleet:\n")
    descriptions = {"messages": "ordinary messages to this fleet",
                    "nudges": "selected-task nudges that ask this fleet's manager to act",
                    "feedback": "task-linked comments to this fleet's configured lead"}
    for capability in args.capabilities:
        _say(terminal, f"- {capability}: {descriptions[capability]}\n")
    _say(terminal, "These capabilities may be used through direct owner access or its explicitly bound connected workspace.\n"
                   "Permission decisions require separate local approval through allow-permissions.\n"
                   "The selected approvals are saved together; matching approvals and prepared requests stay valid.\n")
    if consent is not None and args.capabilities != ("messages",):
        raise CommandFailure("invalid_argument", "guided consent supports ordinary messages only")
    _consent(terminal, "ALLOW-ACTIONS", consent, "allow_messages", preview)
    with mutation_admission(paths.root, identity=RuntimeIdentity.current(),
                            expected_release=preview.get("release")) as release:
        ctx = _approved_action_context(args, paths, store, preview, release)
        actions = tuple({"messages": "message", "nudges": "nudge", "feedback": "feedback"}[capability]
                        for capability in args.capabilities)
        grants = store.allow_actions(actions=actions, expected_owner=preview["owner"], fleet_uid=ctx.fleet_uid,
                                    actor_uid=ctx.caller.uid, actor_alias=ctx.caller.alias)
    return CommandOutput({"state": "allowed", "capabilities": args.capabilities,
                          "grants": [asdict(grant) for grant in grants]},
                         lines=("Owner actions allowed for this actor and fleet: " + ", ".join(args.capabilities) + ".",))


def _allow_action(args, paths, store, terminal, action):
    from ..runtime_admission import RuntimeIdentity, mutation_admission

    preview = _prepare_action_approval(args, paths, store, terminal)
    _describe_action(terminal, preview)
    terminal.write("This capability may be used through direct owner access or its explicitly bound connected workspace.\n"
                   "Workspace approval alone grants no fleet action.\n")
    if action == "message":
        terminal.write("Allow ordinary messages only as this actor in this fleet.\n"
                       "This grants no task mutations, replies or permission decisions.\n")
    elif action == "nudge":
        terminal.write("Allow selected-task nudges only as this actor in this fleet.\n"
                       "Nudges record a task event and ask its fleet manager to act.\n"
                       "This grants no ordinary messages, other task mutations or permission decisions.\n")
    elif action == "feedback":
        terminal.write("Allow task-linked comments as this actor to this fleet's configured lead.\n"
                       "Comments may concern open or completed work.\n"
                       "This grants no ordinary messages, nudges, task state changes or permission decisions.\n")
    else:
        terminal.write("Allow this actor to approve, decline and revoke exact task permission requests.\n"
                       "Requests can concern merges, deployments, spending or deletion.\n"
                       "Each decision must name the recorded request and its exact scope.\n"
                       "This capability records authority; it does not execute those effects.\n"
                       "Revoking this capability prevents new decisions; existing approvals remain until individually revoked.\n")
    label, _, allow_word, _ = _ACTION_LABELS[action]
    _approve(terminal, allow_word)
    with mutation_admission(paths.root, identity=RuntimeIdentity.current(),
                            expected_release=preview.get("release")) as release:
        ctx = _approved_action_context(args, paths, store, preview, release)
        allow = {"message": store.allow_messages, "nudge": store.allow_nudges,
                 "feedback": store.allow_feedback, "permission": store.allow_permissions}[action]
        grant = allow(expected_owner=preview["owner"], fleet_uid=ctx.fleet_uid,
                                    actor_uid=ctx.caller.uid, actor_alias=ctx.caller.alias)
    return CommandOutput({"state": "allowed", "grant": asdict(grant)},
                         lines=(f"Owner {label} allowed for this actor and fleet.",))


def _revoke_action(args, store, terminal, action):
    owner = _active_owner(store)
    current, revoke = {
        "message": (store.current_message_grant, store.revoke_messages),
        "nudge": (store.current_nudge_grant, store.revoke_nudges),
        "feedback": (store.current_feedback_grant, store.revoke_feedback),
        "permission": (store.current_permission_grant, store.revoke_permissions),
    }[action]
    grant = current(expected_owner=owner, fleet_uid=args.fleet_uid)
    _describe(terminal, owner)
    _describe_binding(terminal, grant.fleet_uid, grant.actor_alias, grant.actor_uid)
    _, label, _, revoke_word = _ACTION_LABELS[action]
    terminal.write(f"Revoke only this retained {label} grant. Owner read access remains.\n")
    _approve(terminal, revoke_word)
    if current(expected_owner=owner, fleet_uid=args.fleet_uid) != grant:
        raise CommandFailure("conflict", f"displayed {action} grant changed; inspect and retry")
    revoke(expected_owner=owner, fleet_uid=grant.fleet_uid, expected_grant=grant)
    return CommandOutput({"state": "revoked", "fleet_uid": grant.fleet_uid},
                         lines=(f"Owner {label} grant revoked. Read access remains.",))


@contextmanager
def _grant_errors(action):
    """Translate grant-authoring failures before they leave the terminal body."""
    from ..activation_state import ActivationError
    from ..operation_context import OperationContextError
    from ..runtime_admission import ReleaseMismatch
    from ..plane.schema_state import PendingMigrationError
    from ..plane.migrations import DowngradeError

    try:
        yield
    except ReleaseMismatch as exc:
        raise CommandFailure("release_mismatch", "owner grant requires this installation's current runtime") from exc
    except OperationContextError as exc:
        raise CommandFailure(exc.code, f"owner {action} identities could not be bound; verify the active fleet and actor") from exc
    except ActivationError as exc:
        raise CommandFailure("conflict", "active configuration is unavailable; verify the selected installation") from exc
    except AccessDenied as exc:
        for capability in (("message", "nudge", "feedback") if action == "actions" else (action,)):
            if exc.code == f"invalid_{capability}_binding":
                raise CommandFailure("invalid_argument", f"{capability} grants require canonical fleet and human actor bindings") from exc
            if exc.code == ACTION_DENIALS[capability]:
                raise CommandFailure("conflict", f"no retained {capability} grant for this fleet; inspect host owner status") from exc
            if exc.code == f"{capability}_binding_changed":
                raise CommandFailure("conflict", f"{capability} grant changed or already belongs to another actor; inspect host owner status "
                                     "and revoke the retained grant before approving a replacement") from exc
        if exc.code == "grant_changed":
            raise CommandFailure("conflict", "owner pairing changed; inspect host owner status before retrying") from exc
        raise
    except (PendingMigrationError, DowngradeError, sqlite3.Error, OSError) as exc:
        raise CommandFailure("unavailable", f"{action} grant could not be bound or persisted; inspect local authority, "
                             "active configuration and Plane storage") from exc


def _allow_setup(args, paths, store, terminal, *, expected_retained=None, consent=None):
    import dataclasses
    from ..plane.owner_setup import OwnerSetup, grant_fields
    from ..plane.owner_source import fresh_owner_setup
    from ..plane.owner_setup import managed_setup
    from .operator_context import require_operator_context
    require_operator_context()
    setup = OwnerSetup(store)
    fresh_owner_setup(paths.root)
    managed_setup(paths.root)
    # Native search-path observation only; no enrollment or start.
    from ..supervision_inventory import Adapter, _catalog
    _, _, directories, _, _ = _catalog(Adapter(paths.package).read("svc_inventory_catalog"))
    directory = args.install_directory
    if directory not in directories:
        raise CommandFailure("invalid_argument", "setup directory is not an observed native search path")
    account = getattr(args, "account_directory", None)
    if args.template == "local-project" and account is None:
        account = os.environ.get("CLAUDE_CONFIG_DIR") or None
    managed = getattr(args, "managed_owner", None)
    grant = setup.candidate(_active_owner(store).principal, template=args.template,
        fleet=args.target_fleet, repository=args.repository, install_directory=str(directory),
        project_directory=getattr(args, "project_directory", None),
        account_directory=account, managed_owner=managed, provider_signin=getattr(args,"provider_signin",None))
    _say(terminal, "Approve one first-team setup from the bound connected workspace.\n"
        "The browser may author its goal, inspect a frozen configuration and explicitly launch it.\n"
        "This can enroll/start native services and consume provider usage. No GitHub access is granted.\n")
    if grant.template == "local-project":
        _say(terminal, "Approve read/write access to this exact local project: " + json.dumps(grant.project_directory, ensure_ascii=True) + "\n")
        _say(terminal, "Claude profile directory: " + json.dumps(grant.account_directory or str(Path.home()/".claude"), ensure_ascii=True)
                       + ". No credentials are read or copied by this approval.\n")
    if grant.provider_signin is not None:
        _say(terminal, "Separately approve one Claude subscription sign-in through the bound browser. The official CLI may mint a one-year token; it stays in this host data root only. Cancellation may leave an uncertain provider effect. No model, native start or message approval is included.\n")
        _say(terminal, json.dumps(grant.provider_signin,ensure_ascii=True)+"\n")
        _consent(terminal,"ALLOW-CLAUDE-SIGNIN", consent, "provider_signin",
                 {"grant": grant_fields(grant), "effects": ["one subscription sign-in; no model, service start or messages"]},
                 expires_at=grant.expires_at)
    display = grant_fields(grant)
    if grant.provider_signin is None:
        display.pop("provider_signin")
    if grant.project_directory is None:
        display.pop("project_directory")
    if grant.account_directory is None:
        display.pop("account_directory")
    if grant.managed_owner is None:
        display.pop("managed_owner")
    else:
        _say(terminal, "Also approve the managed owner connection service in this SAME Preview/Launch. "
                       "It persists until explicitly stopped; pairing, messages and private ingress remain separate approvals.\n")
    _say(terminal, json.dumps(display, ensure_ascii=True) + "\n")
    # Guided only: the card carries the messages mark and its answer the name the team will see.
    # The terminal's card is unchanged: no mark, no name.
    marked = consent is not None and _card_finish_ready()
    if marked:
        display["messages"] = {"sender": "card"}
    answer = _consent(terminal, "ALLOW-SETUP", consent, "allow_setup", {"grant": display,
        "effects": ["one browser Preview/Launch, local project read/write, native service enrollment/start, provider usage",
                    _CARD_MESSAGES_EFFECT if marked else "no messages or GitHub access"]}, expires_at=grant.expires_at)
    if marked:
        from ..operation_context import sender_alias_refusal
        from .owner_stdio import StdioRefused
        if not isinstance(answer, str) or sender_alias_refusal(answer) is not None:
            raise StdioRefused("sender_name_refused")
        grant = dataclasses.replace(grant, messages={"alias": answer})
    fresh_owner_setup(paths.root)
    managed_setup(paths.root, grant)
    if managed is not None:
        from .host_owner_setup import _commit_owner_override
        plan = getattr(args, "managed_owner_plan", None)
        if plan is None or plan["binding"] != managed:
            raise CommandFailure("conflict", "managed owner requires the locally reviewed policy change")
        _commit_owner_override(plan)
    setup.allow(grant, expected_retained=expected_retained)
    return CommandOutput({"state": "allowed"}, lines=("One first-team setup approved.",))


def _initialize_connected(store, terminal, *, prepare_workspace=False, consent=None):
    owner = _active_owner(store)
    _describe(terminal, owner)
    if prepare_workspace:
        _say(terminal, "Prepare optional local workspace authority and connected read-session storage "
                       "together in this authority database.\n"
                       "Both local confirmations are required before storage changes. "
                       "No binding, session or action grant is issued.\n")
        _consent(terminal, "INITIALIZE-WORKSPACE", consent, "initialize_workspace",
                 {"owner": asdict(owner), "effects": ["prepare workspace storage; no binding or grant"]})
    else:
        _say(terminal, "Prepare optional connected read-session storage in this authority database.\n"
                       "Workspace authority must already be initialized. No binding, session or action grant is issued.\n")
    _consent(terminal, "INITIALIZE-CONNECTED", consent, "initialize_connected",
             {"owner": asdict(owner), "effects": ["prepare connected storage; no session or grant"]})
    store.connected().initialize(expected_owner=owner, prepare_workspace=prepare_workspace)
    line = ("Local workspace authority and connected read-session storage initialized."
            if prepare_workspace else "Connected read-session storage initialized.")
    return CommandOutput({"state": "initialized"}, lines=(line,))


def _initialize_link_storage(store, terminal, *, consent):
    """The computer run's one storage card: both storages a workspace link needs.

    Its scope lists both, with the effect lines of today's two cards, and the
    approval makes the writes of ``_initialize_connected(prepare_workspace=True)``
    in its one transaction. Typed guide only."""
    if consent is None:
        raise CommandFailure("invalid_argument", "the computer run is the typed guide only")
    owner = _active_owner(store)
    _describe(terminal, owner)
    _consent(terminal, None, consent, "initialize_link_storage", {
        "owner": asdict(owner), "storages": ["workspace", "connected"],
        "effects": ["prepare workspace storage; no binding or grant",
                    "prepare connected storage; no session or grant"]})
    store.connected().initialize(expected_owner=owner, prepare_workspace=True)
    return CommandOutput({"state": "initialized"})


def _confirm_workspace(store, terminal, owner, *, recover=False, consent=None):
    workspace = store.workspace()
    original_binding = workspace.status()
    attempted_codes = set()
    recoverable = {
        "invalid_credential": "The workspace approval code has an invalid format.",
        "workspace_request_expired": "The workspace approval code is not recognized or has expired.",
        "workspace_proposal_missing": "The workspace approval proposal has not been staged.",
        "workspace_code_reused": "A retry requires a NEW browser workspace approval code.",
    }
    changed = {
        "owner": "workspace approval owner changed; inspect host owner status",
        "binding": "workspace binding changed; inspect host owner workspace-status",
    }
    presenter = consent if consent is not None else TerminalWorkspacePresenter(terminal)
    while True:
        try:
            require_workspace_expectation(store, expected_owner=owner, expected_binding=original_binding)
        except WorkspaceConsentChanged as exc:
            raise CommandFailure("conflict", changed[exc.subject]) from None
        try:
            code = consent.code("workspace") if consent is not None else _challenge(terminal, label="workspace approval")
            if code in attempted_codes:
                raise AccessDenied("workspace_code_reused")
            attempted_codes.add(code)
            proposal = preview_workspace_binding(store, code, expected_owner=owner,
                                                 expected_binding=original_binding)
            binding = apply_workspace_binding(store, proposal, presenter=presenter)
            return CommandOutput({"state": "active", "binding": asdict(binding)},
                                 lines=("Local workspace bound. No connected session was issued.",))
        except (EOFError, WorkspaceConsentDeclined):
            raise CommandFailure("conflict", "workspace approval cancelled") from None
        except WorkspaceConsentChanged as exc:
            raise CommandFailure("conflict", changed[exc.subject]) from None
        except AccessDenied as exc:
            message = recoverable.get(exc.code)
            if message is None:
                raise CommandFailure("conflict", "workspace request or binding changed; inspect host owner "
                                     "workspace-status; no owner pairing was replaced") from exc
            guidance = (message + " Inspect host owner workspace-status. Obtain a fresh verified proposal "
                        "in this browser: forget the old approval code if still shown, then choose Prepare local approval. "
                        "The existing owner pairing remains; no workspace binding was approved by this attempt.")
            if not recover:
                raise CommandFailure("conflict", guidance) from exc
            if consent is not None:
                consent.state("workspace_refused", code=exc.code)
                continue
            _say(terminal, guidance + "\nType RETRY to paste a NEW browser code, or CANCEL to stop: ")
            terminal.flush()
            try:
                answer = terminal.readline(32).strip()
            except (EOFError, OSError):
                answer = ""
            if answer != "RETRY":
                raise CommandFailure("conflict", "workspace approval cancelled") from None


def _initialize_first_owner(paths, terminal, *, consent=None):
    from ..plane.owner_setup import managed_setup
    from ..plane.owner_source import fresh_owner_setup
    try:
        package = managed_setup(paths.root)
    except AccessDenied as exc:
        raise CommandFailure("release_mismatch", "fresh owner preparation requires this data root's managed Git CLI") from exc
    fresh_owner_setup(paths.root, allow_absent=True)
    _say(terminal, "Prepare a new host identity and owner authority before its first team.\n"
                   "This grants no activation, private reads or fleet actions.\n")
    _consent(terminal, "INITIALIZE", consent, "initialize_owner", {
        "installation": str(package.installation.directory), "data_root": str(paths.root),
        "source_revision": package.source_revision,
        "effects": ["create host identity and owner authority; no activation, reads or fleet actions"]})
    if managed_setup(paths.root) != package:
        raise CommandFailure("conflict", "managed installation changed during approval")
    fresh_owner_setup(paths.root, allow_absent=True)
    from ..plane.ids import ensure_host_uid
    ensure_host_uid(paths.root / "state")
    OwnerAccess.initialize(paths.root, before_first_fleet=True)
    return CommandOutput({"state": "initialized"}, lines=("First-team owner authority is initialized.",))


def _confirm_owner(paths, store, terminal, *, consent=None):
    from ..plane.ids import read_host_uid
    challenge = store.inspect_pairing(consent.code("pairing") if consent is not None else _challenge(terminal))
    _say(terminal, "Compare this principal with the request shown in your browser:\n")
    _say(terminal, json.dumps(asdict(challenge.principal), ensure_ascii=True) + "\n")
    _say(terminal, "Host: " + json.dumps(read_host_uid(paths.root / "state")) + "\n")
    _say(terminal, "Expires: " + datetime.fromtimestamp(challenge.expires_at, timezone.utc).isoformat() + "\n")
    _say(terminal, "Approval permits private reads across this host's fleets.\n")
    _consent(terminal, "PAIR", consent, "pair_owner", {"principal": asdict(challenge.principal),
        "host_uid": read_host_uid(paths.root / "state"), "expires_at": challenge.expires_at,
        "effects": ["private reads across this host's fleets; no messages or setup grant"]},
        expires_at=challenge.expires_at)
    grant = store.confirm_pairing(challenge.token, expected_principal=challenge.principal)
    return CommandOutput({"state": "paired", "revision": grant.revision},
                         lines=("Owner paired. Return to your browser and sign in.",))


def dispatch(args):
    from ..context import resolve_paths
    from ..paths import InvalidPathSelector
    from ..plane.ids import read_host_uid

    if any(key in os.environ for key in _GENERATED):
        raise CommandFailure("conflict", "owner commands require an operator shell without bot or fleet selectors")
    if args.root is None or args.fleet or args.seed:
        raise CommandFailure("invalid_argument", "owner commands require explicit --root and no fleet or seed")
    if args.owner_action not in {"status", "workspace-status", "setup-status"} and args.json:
        raise CommandFailure("invalid_argument", "--json is supported for owner status and workspace-status only")
    try:
        paths = resolve_paths(root=args.root)
        store = OwnerAccess(paths.root)
        if args.owner_action == "setup":
            from .host_owner_setup import run, interrupted
            if getattr(args, "finish_setup", None) is not None and not sys.stdin.isatty():
                with _unattended_finish(paths.root) as terminal, interrupted():
                    return run(args, paths, store, terminal, unattended=True)
            with _terminal(paths.root) as terminal, interrupted():
                result = run(args, paths, store, terminal)
            if result.data.get("state") == "launch_handoff":
                return _post_setup_handoff(paths, result)
            return result
        if args.owner_action == "serve":
            from ..plane.owner_server import OwnerServerConfigurationError, OwnerServerStartupError, serve

            pairing_origin = getattr(args, "bootstrap_pairing_origin", None)
            if pairing_origin is not None and (args.workspace_issuer is not None or args.workspace_origin is not None):
                raise CommandFailure("invalid_argument", "pairing mode cannot include workspace bootstrap")
            if (args.workspace_issuer is None) != (args.workspace_origin is None):
                raise CommandFailure("invalid_argument", "workspace issuer and origin must be supplied together")
            try:
                serve(paths.root, origin=args.origin, tailscale_binary=args.tailscale,
                      socket_path=args.socket, tailscale_socket=args.tailscale_socket,
                      package=paths.package, workspace_issuer=args.workspace_issuer,
                      workspace_origin=args.workspace_origin,
                      **({"parent_liveness_fd": args._parent_liveness_fd}
                         if getattr(args, "_parent_liveness_fd", None) is not None else {}),
                      **({"pairing_origin": pairing_origin} if pairing_origin is not None else {}))
            except OwnerServerStartupError as exc:
                raise CommandFailure("unavailable", "owner server startup failed; inspect local server logs") from exc
            except OwnerServerConfigurationError as exc:
                raise CommandFailure("unavailable", str(exc)) from exc
            except ImportError as exc:
                raise CommandFailure("unavailable", "owner serving requires the optional [plane-ui] dependencies") from exc
            except OSError as exc:
                raise CommandFailure("unavailable", "owner server failed; inspect local logs and configured resources") from exc
            return CommandOutput({"state": "stopped"}, lines=("Owner server stopped.",))
        if args.owner_action == "setup-status":
            from ..plane.owner_setup import OwnerSetup
            from ..plane.owner_setup import retained_fields
            from .host_owner_setup import card_line, card_status, finish_log_status
            state = OwnerSetup(store).retained_state()
            card = card_status(store, state)
            if card is not None and state.launch_id is not None:
                card["log"] = finish_log_status(paths.root, state.launch_id)  # Its path and existence only.
            return CommandOutput({**retained_fields(state), **({"card": card} if card is not None else {})},
                lines=(f"First-team setup approval: {state.state}.",
                *(() if state.launch_id is None else (f"Original request: {state.launch_id}; configuration {state.launch_plan}.",)),
                *(() if card is None else (card_line(card["messages"]),))))
        if args.owner_action == "workspace-status":
            state, binding = store.workspace().status()
            return CommandOutput({"state": state, "effective_active": state == "active",
                                  "binding": asdict(binding) if binding else None},
                                 lines=(f"Local workspace binding: {state}. No connected session is issued.",))
        if args.owner_action == "status":
            status = store.local_action_status()
            grant, messages, nudges, feedbacks = (status.owner, status.message_grants,
                                                  status.nudge_grants, status.feedback_grants)
            state = owner_pairing_state(grant)
            lines = [f"Owner access: {state}."]
            for message in messages:
                lines.append("Message grant: " + json.dumps(asdict(message), ensure_ascii=True))
            for nudge in nudges:
                lines.append("Nudge grant: " + json.dumps(asdict(nudge), ensure_ascii=True))
            for feedback in feedbacks:
                lines.append("Feedback grant: " + json.dumps(asdict(feedback), ensure_ascii=True))
            for permission in status.permission_grants:
                lines.append("Permission grant: " + json.dumps(asdict(permission), ensure_ascii=True))
            return CommandOutput({"state": state, "owner": asdict(grant) if grant else None,
                                  "message_grants": [asdict(message) for message in messages],
                                  "nudge_grants": [asdict(nudge) for nudge in nudges],
                                  "feedback_grants": [asdict(feedback) for feedback in feedbacks],
                                  "permission_grants": [asdict(p) for p in status.permission_grants]},
                                 lines=tuple(lines))
        with _terminal(paths.root) as terminal:
            if args.owner_action in {"allow-setup", "revoke-setup"}:
                from ..plane.owner_setup import OwnerSetup
                if args.owner_action == "revoke-setup":
                    from .operator_context import require_operator_context
                    require_operator_context()
                setup = OwnerSetup(store)
                if args.owner_action == "allow-setup":
                    return _allow_setup(args, paths, store, terminal)
                grant = setup.local()
                if grant is None: raise CommandFailure("not_found", "no retained setup approval")
                from ..plane.owner_setup import grant_fields
                terminal.write(json.dumps(grant_fields(grant), ensure_ascii=True) + "\n")
                _approve(terminal, "REVOKE-SETUP")
                setup.revoke(grant)
                return CommandOutput({"state": "revoked"}, lines=("Setup approval revoked; a started update is not undone.",))
            if args.owner_action == "initialize-connected":
                return _initialize_connected(store, terminal, prepare_workspace=args.prepare_workspace)
            if args.owner_action == "allow-actions":
                with _grant_errors("actions"):
                    return _allow_actions(args, paths, store, terminal)
            if args.owner_action in {"allow-messages", "revoke-messages", "allow-nudges", "revoke-nudges",
                                     "allow-feedback", "revoke-feedback", "allow-permissions", "revoke-permissions"}:
                action = {"messages": "message", "nudges": "nudge", "feedback": "feedback", "permissions": "permission"}[
                    args.owner_action.split("-", 1)[1]]
                with _grant_errors(action):
                    if args.owner_action.startswith("allow-"):
                        return _allow_action(args, paths, store, terminal, action)
                    return _revoke_action(args, store, terminal, action)
            if args.owner_action in {"initialize-workspace", "confirm-workspace", "revoke-workspace"}:
                workspace = store.workspace()
                owner = _active_owner(store)
                _describe(terminal, owner)
                if args.owner_action == "initialize-workspace":
                    terminal.write("Prepare optional local workspace authority. No workspace or session is approved.\n")
                    _approve(terminal, "INITIALIZE-WORKSPACE")
                    workspace.initialize(expected_owner=owner)
                    return CommandOutput({"state": "initialized"}, lines=("Local workspace authority initialized.",))
                if args.owner_action == "confirm-workspace":
                    return _confirm_workspace(store, terminal, owner)
                _, binding = workspace.status()
                if binding is None or not binding.active:
                    raise CommandFailure("conflict", "no retained active workspace binding to revoke")
                terminal.write(json.dumps(asdict(binding), ensure_ascii=True) + "\n")
                terminal.write("Revoke only this workspace binding. Direct owner access and fleet grants remain.\n")
                _approve(terminal, "REVOKE-WORKSPACE")
                revoked = workspace.revoke(expected_binding=binding, expected_owner=owner)
                return CommandOutput({"state": "revoked", "binding": asdict(revoked)},
                                     lines=("Local workspace binding revoked. Direct owner access remains.",))
            if args.owner_action == "bind-source":
                from ..plane.owner_source import bind_source

                store.current_grant()  # require initialized local authority
                host_uid = read_host_uid(paths.root / "state")
                terminal.write("Bind the existing Plane database to host " + json.dumps(host_uid) + ".\n"
                               "Confirm that all its history, including imported records, belongs to this installation.\n"
                               "This writes ownership metadata and indexes; it does not copy, migrate or remove records.\n")
                _approve(terminal, "BIND")
                bind_source(paths.root, expected_host_uid=host_uid)
                return CommandOutput({"state": "bound"}, lines=("Plane source bound to this installation.",))
            if args.owner_action == "initialize":
                before_first = getattr(args, "before_first_fleet", False)
                if before_first:
                    return _initialize_first_owner(paths, terminal)
                host_uid = read_host_uid(paths.root / "state")
                terminal.write("Prepare owner access for host " + json.dumps(host_uid) + ".\n"
                               "This does not pair anyone or expose a network service.\n")
                _approve(terminal, "INITIALIZE")
                OwnerAccess.initialize(paths.root)
                return CommandOutput({"state": "initialized"}, lines=("Owner authority is initialized.",))
            if args.owner_action == "confirm":
                return _confirm_owner(paths, store, terminal)
            if args.owner_action == "revoke":
                grant = store.current_grant()
                if grant is None or not grant.active:
                    raise CommandFailure("conflict", "no active owner pairing to revoke")
                _describe(terminal, grant)
                terminal.write("This revokes this pairing and its browser sessions. Fleets keep running.\n")
                _approve(terminal, "REVOKE")
                revoked = store.revoke_owner(expected_revision=grant.revision)
                return CommandOutput({"state": "revoked", "revision": revoked.revision},
                                     lines=("Owner pairing revoked. Existing sessions are no longer authorized.",))
            raise CommandFailure("invalid_argument", "unsupported owner command")
    except InvalidPathSelector as exc:
        raise CommandFailure("invalid_argument", "invalid host root selector") from exc
    except (SourceUnavailable, SourceDenied) as exc:
        observation = source_refusal(exc)
        code, message = {
            'source_binding_required': ('unavailable',
                "Plane source lacks required binding or indexes; verify the selected installation "
                "and re-run host owner bind-source. Binding refuses foreign or mixed-host history."),
            'source_denied': ('conflict',
                "Plane source is foreign or mixed-host; select the correct installation "
                "or investigate its history. Do not rebind this source."),
            'source_unavailable': ('unavailable',
                "Plane source cannot be verified; inspect the selected installation's "
                "database and schema locally. Pairing again cannot repair source state."),
        }[observation.reason]
        raise CommandFailure(code, message) from exc
    except AccessDenied as exc:
        if args.owner_action == "initialize-connected":
            raise CommandFailure("conflict", "connected initialization requires the current native paired owner; inspect host owner status") from exc
        if "workspace" in args.owner_action:
            raise CommandFailure("conflict", "workspace request or binding is no longer valid; inspect host owner "
                                 "workspace-status and obtain a fresh verified proposal if needed") from exc
        raise CommandFailure("conflict", "owner request is no longer valid; inspect status and request pairing again") from exc
    except (AccessUnavailable, ValueError) as exc:
        if args.owner_action == "initialize-connected":
            if args.prepare_workspace:
                raise CommandFailure("unavailable", "connected storage or workspace authority is unavailable; inspect local schemas; "
                                     "partial schemas are never repaired") from exc
            raise CommandFailure("unavailable", "connected storage or workspace authority is unavailable; inspect local schemas "
                                 "and initialize workspace authority first; partial schemas are never repaired") from exc
        if "workspace" in args.owner_action:
            raise CommandFailure("unavailable", "local workspace authority is unavailable; inspect the selected root, "
                                 "owner status and optional schema; use host owner initialize-workspace only "
                                 "when the optional schema has not been initialized") from exc
        raise CommandFailure("unavailable", "owner authority is unavailable; verify the selected installation") from exc
