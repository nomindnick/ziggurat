"""Shared network bounding for every outbound call this package makes.

WHY THIS MODULE EXISTS (item 3.1's most expensive lesson, re-applied by 3.1b):
under systemd ``Type=oneshot``, ``TimeoutStartSec`` defaults to INFINITY. One
black-holed TCP connection therefore holds the service Active *forever*, every
later timer trigger is silently skipped, and the cadence stops running with
nothing reporting it. Item 3.1 found that in ``ziggurat/league/source.py`` and
fixed it there with a private ``_bounded_socket``; item 3.1b found the same
defect unfixed at three NFL seams (Sleeper projections, Open-Meteo, ESPN
``kona_player_info``), so the helper is lifted here and used by all of them
rather than copied a fourth time.

Three mechanisms, because the seams differ:

* ``HTTP_TIMEOUT`` — the explicit value to pass to ``urllib.request.urlopen`` at
  the seams that DO take one. Prefer this always: it is per-call and touches no
  global state.
* ``bounded_espn()`` — for ``espn_api``, which calls module-level
  ``requests.get`` with NO timeout argument (grep the package for "timeout":
  zero hits). It temporarily swaps the ``requests`` module object *inside*
  ``espn_api.requests.espn_requests`` for a shim that injects ``timeout=``, and
  restores it on the way out.
* ``bounded_socket()`` — the process-wide socket default, kept as a second
  fence for any client that honours it.

**Why ``bounded_socket`` alone is NOT enough, measured 2026-07-24 (3.1b audit
finding).** ``socket.setdefaulttimeout()`` is only consulted when a socket is
created WITHOUT an explicit timeout. ``requests`` always passes one: with no
``timeout=`` argument it builds ``urllib3.Timeout(connect=None, read=None)``,
and urllib3's ``create_connection`` then calls ``sock.settimeout(None)``
explicitly — which DISCARDS the process default and blocks forever. Reproduced
against a local accept-and-never-reply server: ``with bounded_socket(3):
requests.get(blackhole)`` was still blocked when killed at 40s, while
``requests.get(blackhole, timeout=3)`` raised in 3.0s. So item 3.1's league-sync
hang fix and 3.1b's first ESPN fix were both ineffective at the seam they were
written for, and only ``TimeoutStartSec`` was doing any work. ``bounded_espn()``
is the one that actually bites; it is what the two ESPN seams use.

None of these is a wall-clock cap on a whole response (they bound individual
socket operations), so the systemd units still set ``TimeoutStartSec``. Defense
in depth: the per-request bound turns a hang into a clean error for every caller
including cron; the unit timeout catches whatever it cannot.
"""

import socket
from contextlib import contextmanager
from importlib import import_module

# Seconds any single socket operation may block. Generous enough for a slow
# nflverse parquet download over a bad link, short enough that a hung pull
# fails inside one timer interval instead of parking the unit.
HTTP_TIMEOUT = 60


@contextmanager
def bounded_socket(seconds: int = HTTP_TIMEOUT):
    """Bound socket blocking for the duration of one request, then RESTORE it.

    Restoring matters: the sync, the ingesters and the draft cockpit share a
    process, and leaving a global default behind would silently change the
    timeout behaviour of code that never asked for it.
    """
    previous = socket.getdefaulttimeout()
    socket.setdefaulttimeout(seconds)
    try:
        yield
    finally:
        socket.setdefaulttimeout(previous)


# The requests API surface espn_api uses. Anything else on the module passes
# through untouched (constants, exception classes, Session, ...).
_REQUEST_VERBS = ("get", "post", "put", "patch", "delete", "head", "options", "request")


class _TimeoutInjecting:
    """A stand-in for the ``requests`` module that supplies a default timeout.

    Only fills in a timeout the caller did not give; an explicit ``timeout=``
    always wins. Everything that is not a request verb is proxied straight
    through, so the shim is invisible to any other use of the module.
    """

    def __init__(self, module, seconds):
        self._module = module
        self._seconds = seconds

    def __getattr__(self, name):
        attr = getattr(self._module, name)
        if name not in _REQUEST_VERBS or not callable(attr):
            return attr

        def _bounded(*args, **kwargs):
            kwargs.setdefault("timeout", self._seconds)
            return attr(*args, **kwargs)

        return _bounded


@contextmanager
def bounded_espn(seconds: int = HTTP_TIMEOUT):
    """Bound every ESPN request made inside the block, then restore.

    THE seam that matters: ``espn_api`` issues bare ``requests.get`` calls, and
    ``requests`` ignores the process-wide socket default (see the module
    docstring for the measurement). Swapping the module object it resolves
    ``requests`` through is the smallest change that actually bounds it, and it
    needs no fork of the client.

    Also enters ``bounded_socket`` so anything the client does through plain
    sockets is covered too.
    """
    espn_requests = import_module("espn_api.requests.espn_requests")
    original = espn_requests.requests
    espn_requests.requests = _TimeoutInjecting(original, seconds)
    try:
        with bounded_socket(seconds):
            yield
    finally:
        espn_requests.requests = original
