"""Connection checks against fake API responses: each failure names its fix."""
from teampulse import health


class Resp:
    def __init__(self, status, body=None):
        self.status_code, self._body = status, body or {}
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def _hit(self, method, url):
        self.calls.append((method, url))
        for key, resp in self.routes.items():
            if key in url:
                return resp
        return Resp(404)

    def get(self, url, **kw):
        return self._hit("GET", url)

    def post(self, url, **kw):
        return self._hit("POST", url)


def by_name(checks):
    return {c.name: c for c in checks}


def test_not_configured_makes_no_calls():
    s = FakeSession({})
    assert [c.ok for c in health.check_github(s, token="", repos=[])] == [None]
    assert [c.ok for c in health.check_notion(s, token="", databases=[], pages=[])] == [None]
    assert [c.ok for c in health.check_discord(s, token="", guild="")] == [None]
    assert s.calls == []


def test_github_good_token_missing_repo_access():
    s = FakeSession({"/user": Resp(200, {"login": "riya-dev"}), "/repos/team/ok/issues": Resp(200, []),
                     "/repos/team/ok": Resp(200, {})})
    checks = by_name(health.check_github(s, token="t", repos=["team/ok", "team/secret"]))
    assert checks["Token"].ok and "riya-dev" in checks["Token"].detail
    assert checks["team/ok"].ok and checks["team/secret"].ok is False and "access" in checks["team/secret"].fix
    bad = health.check_github(FakeSession({"/user": Resp(401)}), token="t", repos=["x/y"])
    assert bad[0].ok is False and "new one" in bad[0].fix


def test_notion_unshared_database():
    s = FakeSession({"/users/me": Resp(200, {"name": "TeamPulse"}), "/databases/aaa/query": Resp(200, {}),
                     "/pages/ccc": Resp(200, {})})
    checks = by_name(health.check_notion(s, token="t", databases=["aaa", "bbb"], pages=["ccc"]))
    assert checks["Database aaa"].ok and checks["Database bbb"].ok is False
    assert "Connections" in checks["Database bbb"].fix and checks["Page ccc"].ok


def test_discord_intent_and_server():
    s = FakeSession({"/users/@me": Resp(200, {"username": "TeamPulse"}), "/applications/@me": Resp(200, {"flags": 0}),
                     "/guilds/42": Resp(200, {"name": "CampusEats"})})
    checks = by_name(health.check_discord(s, token="t", guild="42"))
    assert checks["Bot"].ok and checks["Team server"].ok and "CampusEats" in checks["Team server"].detail
    assert checks["Message Content intent"].ok is False and "Message Content Intent" in checks["Message Content intent"].fix
    s.routes["/applications/@me"] = Resp(200, {"flags": 1 << 19})
    assert by_name(health.check_discord(s, token="t", guild="42"))["Message Content intent"].ok


def test_network_errors_are_reported_not_raised():
    import requests

    class Down:
        def get(self, *a, **k):
            raise requests.ConnectionError("offline")

    check = health.check_github(Down(), token="t", repos=[])[0]
    assert check.ok is False and "internet" in check.fix
