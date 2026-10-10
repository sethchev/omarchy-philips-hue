"""Bounded Hue callers must never let urllib consume a redirect body."""

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
