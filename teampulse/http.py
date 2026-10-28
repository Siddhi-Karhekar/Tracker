"""HTTP sessions for the GitHub and Notion pollers: bounded retries for rate limits and brief outages.

Retries 429 and 5xx up to 3 times with exponential backoff (1 s, 2 s, 4 s), honouring the service's
Retry-After header. Only reads are made, so retrying a POST (Notion's database query) is safe. After the last
retry the response is returned as is and the caller's raise_for_status() reports it.
"""
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

RETRY = Retry(total=3, connect=3, read=2, backoff_factor=1, status_forcelist=(429, 500, 502, 503, 504),
              allowed_methods=frozenset({"GET", "POST"}), respect_retry_after_header=True, raise_on_status=False)


def session() -> requests.Session:
    s = requests.Session()
    adapter = HTTPAdapter(max_retries=RETRY)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s
