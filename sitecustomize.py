import urllib3
import requests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_original_request = requests.sessions.Session.request


def _request_without_ssl_verify(self, method, url, **kwargs):
    kwargs.setdefault("verify", False)
    return _original_request(self, method, url, **kwargs)


requests.sessions.Session.request = _request_without_ssl_verify
