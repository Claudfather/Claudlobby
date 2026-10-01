"""Dependency-light canonical check-in command registration."""

from importlib import import_module

from ..command_result import execute


def _dispatch(args):
    return execute(args.public_command,
                   lambda: import_module(".checkin", __package__).dispatch(args),
                   json_output=args.json, request_id=getattr(args, "request_id", None))


def register_checkin_subparsers(sub):
    checkin = sub.add_parser("checkin", help="Read or record fleet check-in decisions")
    children = checkin.add_subparsers(dest="checkin_action", required=True)

    listing = children.add_parser("list", help="List recorded decisions in the selected fleet")
    listing.add_argument("--bot", help="Only one generated bot's decisions")
    listing.add_argument("--since", default="7d", help="Window such as 24h, 7d or RFC3339")
    listing.add_argument("--last", action="store_true", help="Newest decision, ignoring --since")
    listing.add_argument("--raised", action="store_true", help="Only decisions that surfaced an ask")
    listing.add_argument("--limit", type=int, help="At most N rows, after filters")
    listing.add_argument("--summary", action="store_true", help="Roll up the full selected window")
    listing.add_argument("--json", action="store_true", help="One schema-1 result object")
    listing.set_defaults(func=_dispatch, public_command="checkin.list")

    showing = children.add_parser("show", help="Show one canonical check-in decision")
    showing.add_argument("checkin_id", metavar="CHECKIN_ID")
    showing.add_argument("--json", action="store_true", help="One schema-1 result object")
    showing.set_defaults(func=_dispatch, public_command="checkin.show")

    recording = children.add_parser("record", help="Commit a decision before acting")
    recording.add_argument("--file", required=True, metavar="PATH", help="Decision JSON")
    recording.add_argument("--selection-file", metavar="PATH", help="Optional full selection evidence JSON")
    recording.add_argument("--request-id", required=True, metavar="UUID")
    recording.add_argument("--dry-run", action="store_true", help="Validate without an identity or write")
    recording.add_argument("--json", action="store_true", help="One schema-1 result object")
    recording.set_defaults(func=_dispatch, public_command="checkin.record")

    selection = children.add_parser("selection", help="Inspect selection evidence offline")
    actions = selection.add_subparsers(dest="selection_action", required=True)
    verifying = actions.add_parser("verify", help="Validate one selection record without recording")
    verifying.add_argument("file", metavar="FILE")
    verifying.add_argument("--issue-states", metavar="FILE", help="Offline issue-state JSON map")
    verifying.add_argument("--json", action="store_true", help="One schema-1 result object")
    verifying.set_defaults(func=_dispatch, public_command="checkin.selection.verify")
    focus = actions.add_parser("focus-refs", help="List numbered sprint-focus issue references")
    focus.add_argument("file", metavar="FILE")
    focus.add_argument("--json", action="store_true", help="One schema-1 result object")
    focus.set_defaults(func=_dispatch, public_command="checkin.selection.focus-refs")
