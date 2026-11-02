"""Bounded Hue callers must never let urllib consume a redirect body."""

import signal
from contextlib import contextmanager

import urllib.error
import urllib.request


class RejectRedirects(urllib.request.HTTPRedirectHandler):
    def http_error_301(self, request, response, code, message, headers):
        # urllib's default handler drains this body with read() before following
        # Location. Reject at the error-handler boundary without reading it.
        response.close()
        error = urllib.error.HTTPError(
            request.full_url, code, "HTTP redirects are not permitted", headers, None)
        error.close()
        raise error

    http_error_302 = http_error_301
    http_error_303 = http_error_301
    http_error_307 = http_error_301
    http_error_308 = http_error_301


def no_redirect_opener(context=None):
    return urllib.request.build_opener(
        RejectRedirects(), urllib.request.HTTPSHandler(context=context))


class DeadlineExceeded(RuntimeError):
    """A whole operation expired, including all reads and writes."""


@contextmanager
def operation_deadline(seconds=25):
    """Bound Linux CLI operations even when a peer keeps sending small chunks.

    Used on the main thread of the short-lived helpers, not worker threads.
    Restore the caller's signal handler and timer when leaving the scope.
    """
    import time

    def expired(_signum, _frame):
        raise DeadlineExceeded("Hue operation timed out")

    started = time.monotonic()
    previous_handler = signal.signal(signal.SIGALRM, expired)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0]:
            signal.setitimer(signal.ITIMER_REAL,
                             max(0.001, previous_timer[0] - (time.monotonic() - started)),
                             previous_timer[1])
