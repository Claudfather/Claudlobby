"""Provider status pages for the creds-check tests (#2424).

``provider-status/`` holds pages in the shape of the two that creds-check
reads, ``status.railway.com/api/status`` and GitHub's
``githubstatus.com/api/v2/summary.json``, as captured live on 2026-10-21. Each
holds one state of a provider's API, for testing how a rejected token reads in
that state:

- ``railway-api-incident``: an open incident whose components include the API
  (PARTIAL_OUTAGE), so a rejection reads ``unknown``.
- ``railway-builds-incident``: an open incident on builds only, so a rejection
  still reads ``fail``.
- ``railway-operational``: no open incident, so a rejection reads ``fail``.
- ``github-operational``: every component operational and no incident, so a
  rejection reads ``fail``.
- ``github-api-incident``: "API Requests" in partial_outage and an open
  incident listing it, so a rejection reads ``unknown``.
"""

import shutil
from pathlib import Path

PAGES = Path(__file__).parent / "provider-status"

#: A case arm for a curl stub: answers the two status pages creds-check reads
#: with the page the test put in the stub's own directory, or, with none there,
#: the way curl answers a host it cannot reach.
STATUS_PAGE_ARM = r"""
case "$*" in
  *status.railway.com/api/status*|*githubstatus.com/api/v2/summary.json*)
    page="$(dirname "$0")/status-github.json"
    case "$*" in *status.railway.com*) page="$(dirname "$0")/status-railway.json" ;; esac
    out=""
    prev=""
    for a in "$@"; do
      [ "$prev" = "-o" ] && out="$a"
      prev="$a"
    done
    if [ -f "$page" ]; then
      cp "$page" "$out"
      printf '200'
      exit 0
    fi
    echo "curl: (6) Could not resolve host" >&2
    printf '000'
    exit 6 ;;
esac
"""


def serve_status(bindir: Path, provider: str, page: str | None = None, *, body: str | None = None) -> None:
    """Make the stub in ``bindir`` serve ``provider``'s status page: a captured
    page by name, a literal ``body``, or with neither, no page at all."""
    target = bindir / f"status-{provider}.json"
    if body is not None:
        target.write_text(body)
    elif page is not None:
        shutil.copyfile(PAGES / f"{page}.json", target)
    else:
        target.unlink(missing_ok=True)
