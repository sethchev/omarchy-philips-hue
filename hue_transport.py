"""Bounded I/O for Hue network requests and short-lived helpers."""

import os
import selectors
import signal
import subprocess
import time
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


class OutputLimitExceeded(subprocess.SubprocessError):
    """A helper exceeded its output budget."""


def read_command_output(command, *, timeout, stdout_limit, stderr_limit=None,
                        stdout_rows=None, allow_partial=False):
    """Return bounded stdout, optionally keeping it on timeout or nonzero exit.

    When supplied, stderr_limit bounds how much stderr is read and discarded.
    Output limit violations always reject the captured output.
    """
    deadline = time.monotonic() + timeout
    stderr = subprocess.DEVNULL if stderr_limit is None else subprocess.PIPE
    with subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=stderr) as process:
        stdout = bytearray()
        newlines = 0
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ, stdout_limit)
                if process.stderr is not None:
                    selector.register(process.stderr, selectors.EVENT_READ, stderr_limit)
                budgets = {key.fd: key.data for key in selector.get_map().values()}
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(command, timeout)
                    for key, _ in selector.select(remaining):
                        # Read at most one byte beyond the remaining budget
                        # to distinguish exact-limit EOF from excess output.
                        block = os.read(key.fd, min(4096, budgets[key.fd] + 1))
                        if not block:
                            selector.unregister(key.fd)
                            continue
                        budgets[key.fd] -= len(block)
                        if budgets[key.fd] < 0:
                            raise OutputLimitExceeded("Helper exceeded its byte limit")
                        if key.fileobj is process.stdout:
                            if stdout_rows is not None:
                                newlines += block.count(b"\n")
                                if newlines + (not block.endswith(b"\n")) > stdout_rows:
                                    raise OutputLimitExceeded("Helper exceeded its row limit")
                            stdout.extend(block)
            status = process.wait(timeout=max(0, deadline - time.monotonic()))
            if status and not allow_partial:
                raise subprocess.CalledProcessError(status, command)
            return bytes(stdout)
        except subprocess.TimeoutExpired:
            if not allow_partial:
                raise
            return bytes(stdout)
        finally:
            if process.poll() is None:
                process.kill()


class DeadlineExceeded(RuntimeError):
    """A whole operation expired, including all reads and writes."""


@contextmanager
def operation_deadline(seconds=25):
    """Bound Linux CLI operations even when a peer keeps sending small chunks.

    Used on the main thread of the short-lived helpers, not worker threads.
    Restore the caller's signal handler and timer when leaving the scope.
    """
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
