"""/moks base-path routing, SPA deep links, authentication, CSRF and sessions."""
from tests.conftest import login


async def test_base_path_redirects_to_trailing_slash(client):
    r = await client.get("/moks", follow_redirects=False)
    assert r.status_code == 308 and r.headers["location"] == "/moks/"


async def test_deep_links_serve_the_spa_shell(client):
    for path in ("/moks/", "/moks/investigations/1b2c", "/moks/admin/connectors", "/moks/login"):
        r = await client.get(path)
        assert r.status_code == 200, path
        assert "MOKS test shell" in r.text
        assert r.headers["cache-control"] == "no-cache"
        assert "frame-ancestors 'none'" in r.headers["content-security-policy"]


async def test_assets_are_served_and_missing_assets_are_404_not_html(client):
    ok = await client.get("/moks/assets/app.js")
    assert ok.status_code == 200 and "immutable" in ok.headers["cache-control"]
    missing = await client.get("/moks/assets/nope.js")
    assert missing.status_code == 404 and "MOKS test shell" not in missing.text


async def test_path_traversal_is_not_served(client):
    r = await client.get("/moks/..%2f..%2fconftest.py")
    assert "fixtures" not in r.text and "import" not in r.text


async def test_unknown_api_paths_return_json_404(client):
    r = await client.get("/moks/api/does-not-exist")
    assert r.status_code == 404 and r.json()["detail"] == "Not found"


async def test_api_docs_disabled_by_default(client):
    assert (await client.get("/moks/api/docs")).status_code == 404


async def test_probes_and_public_health(client):
    assert (await client.get("/healthz")).json() == {"status": "ok"}
    assert (await client.get("/readyz")).status_code == 200
    assert (await client.get("/moks/api/health")).json()["status"] == "ok"


async def test_unauthenticated_api_is_rejected(client):
    assert (await client.get("/moks/api/auth/me")).status_code == 401
    assert (await client.get("/moks/api/investigations")).status_code == 401


async def test_session_cookie_attributes(client):
    r = await client.post("/moks/api/auth/dev-login", json={"email": "cookie@example.com"})
    cookie = r.headers["set-cookie"]
    assert "moks_session=" in cookie and "HttpOnly" in cookie and "Path=/moks" in cookie and "SameSite=lax" in cookie


async def test_csrf_and_origin_are_required_for_mutations(client):
    headers = await login(client, "admin@example.com")
    body = {"name": "csrf-env", "kind": "minikube"}
    assert (await client.post("/moks/api/environments", json=body)).status_code == 403  # no token
    bad_origin = {**headers, "Origin": "https://evil.example"}
    assert (await client.post("/moks/api/environments", json=body, headers=bad_origin)).status_code == 403
    assert (await client.post("/moks/api/environments", json=body, headers=headers)).status_code == 201


async def test_new_users_have_no_access_until_granted(client):
    headers = await login(client, "nobody@example.com")
    me = (await client.get("/moks/api/auth/me")).json()
    assert me["roles"] == [] and me["permissions"] == []
    assert (await client.get("/moks/api/admin/users")).status_code == 403
    assert (await client.post("/moks/api/environments", json={"name": "x", "kind": "eks"}, headers=headers)).status_code == 403


async def test_bootstrap_admin_gets_admin(client):
    await login(client, "admin@example.com")
    me = (await client.get("/moks/api/auth/me")).json()
    assert {"role": "admin", "environment": "*", "namespace": "*"} in me["roles"]


async def test_logout_revokes_the_session(client):
    headers = await login(client, "bye@example.com")
    r = await client.post("/moks/api/auth/logout", headers=headers)
    assert r.json()["redirect"] == "/moks/login"
    assert (await client.get("/moks/api/auth/me")).status_code == 401


async def test_denials_are_audited(client):
    await login(client, "denied@example.com")
    await client.get("/moks/api/admin/users")
    headers = await login(client, "admin@example.com")
    events = (await client.get("/moks/api/admin/audit", params={"actor": "denied@example.com", "decision": "deny"})).json()["events"]
    assert any(e["action"] == "authz.deny" for e in events)


async def test_local_login_rejects_bad_password_and_rate_limits(client, monkeypatch):
    from moks.config import get_settings
    from moks.cli import _create_user

    monkeypatch.setattr(get_settings(), "auth_mode", "local")
    await _create_user("local@example.com", "a-very-long-password", ["viewer"])
    bad = await client.post("/moks/api/auth/login", json={"email": "local@example.com", "password": "wrong"})
    assert bad.status_code == 401
    ok = await client.post("/moks/api/auth/login", json={"email": "local@example.com", "password": "a-very-long-password"})
    assert ok.status_code == 200
    codes = [(await client.post("/moks/api/auth/login", json={"email": "local@example.com", "password": "x"})).status_code for _ in range(6)]
    assert 429 in codes


async def test_dev_login_refused_outside_dev_mode(client, monkeypatch):
    from moks.config import get_settings

    monkeypatch.setattr(get_settings(), "auth_mode", "oidc")
    assert (await client.post("/moks/api/auth/dev-login", json={"email": "x@example.com"})).status_code == 404


def test_dev_mode_refused_in_production(monkeypatch):
    import pytest
    from pydantic import ValidationError

    from moks.config import Settings

    with pytest.raises(ValidationError):
        Settings(env="production", auth_mode="dev", session_secret="x" * 40)
    with pytest.raises(ValidationError):
        Settings(env="production", auth_mode="local", session_secret="short")


def test_open_redirects_are_blocked():
    from moks.auth.routes import safe_next

    assert safe_next("/moks/investigations/1") == "/moks/investigations/1"
    for evil in ("https://evil.com", "//evil.com", "/moks/../x://", "/other", None, "/moks\\@evil"):
        assert safe_next(evil) == "/moks/"


def test_database_dependency_commits_before_the_response():
    """Depends(get_db) with the default request scope commits AFTER the response is sent, so a client
    could sign in and immediately get 401 (observed live). Every use must be scope="function"."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[1] / "moks"
    offenders = [str(p) for p in root.rglob("*.py") if re.search(r"Depends\(get_db\)", p.read_text())]
    assert offenders == []


async def test_health_reports_the_build_and_mixed_versions_are_detected(client):
    from moks import versions
    from moks.db import session_scope
    from sqlalchemy import text
    from tests.conftest import make_user

    assert (await client.get("/moks/api/health")).json()["commit"] == "dev"
    await make_user("ver@example.com", ["viewer"])
    await login(client, "ver@example.com")
    async with session_scope() as db:
        await db.execute(text("DELETE FROM component_versions"))
    await versions.report("worker")
    assert (await client.get("/moks/api/system/versions")).json()["mixed"] is False
    async with session_scope() as db:
        await db.execute(text("INSERT INTO component_versions (instance, component, commit_sha, version) VALUES ('executor:old', 'executor', 'abc', '0.1.0')"))
    assert (await client.get("/moks/api/system/versions")).json()["mixed"] is True


async def test_password_check_does_not_run_on_the_event_loop(client, monkeypatch):
    """argon2id takes tens of milliseconds and 64 MiB by design. On the loop it would stall every other request."""
    import threading

    from moks.auth import routes
    from moks.cli import _create_user
    from moks.config import get_settings

    class Spy:
        def __init__(self, real):
            self.real, self.threads = real, []

        def verify(self, hashed, password):
            self.threads.append(threading.current_thread())
            return self.real.verify(hashed, password)

        def __getattr__(self, name):
            return getattr(self.real, name)

    monkeypatch.setattr(get_settings(), "auth_mode", "local")
    monkeypatch.setattr(get_settings(), "rate_login_per_minute", 1000)  # the rate-limit test above used this address's allowance
    await _create_user("threaded@example.com", "a-very-long-password", ["viewer"])
    spy = Spy(routes.hasher)
    monkeypatch.setattr(routes, "hasher", spy)
    bad = await client.post("/moks/api/auth/login", json={"email": "threaded@example.com", "password": "not-the-password"})
    ok = await client.post("/moks/api/auth/login", json={"email": "threaded@example.com", "password": "a-very-long-password"})
    assert (bad.status_code, ok.status_code) == (401, 200), (bad.text, ok.text)
    assert len(spy.threads) == 2 and all(t is not threading.current_thread() for t in spy.threads)


async def _oidc_sign_in(client, monkeypatch, *, sub: str, email: str):
    """One sign-in through /auth/oidc/login and /auth/oidc/callback against a stand-in identity provider.
    The provider's side (discovery, the token endpoint, the ID token's signature) is replaced; MOKS's side runs."""
    import types
    from urllib.parse import parse_qs, urlsplit

    import httpx

    from moks.auth import routes
    from moks.config import get_settings

    s = get_settings()
    for name, value in {"auth_mode": "oidc", "oidc_issuer": "https://idp.example", "oidc_client_id": "moks", "rate_login_per_minute": 1000}.items():
        monkeypatch.setattr(s, name, value)

    async def discovery():
        return {"issuer": "https://idp.example", "authorization_endpoint": "https://idp.example/authorize", "token_endpoint": "https://idp.example/token", "_jwks": {"keys": []}}

    class TokenEndpoint:
        def __init__(self, **_):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def post(self, url, **_):
            return httpx.Response(200, json={"id_token": "verified-by-the-stand-in"}, request=httpx.Request("POST", url))

    monkeypatch.setattr(routes, "_discovery", discovery)
    monkeypatch.setattr(routes, "httpx", types.SimpleNamespace(AsyncClient=TokenEndpoint))
    monkeypatch.setattr(routes, "_validate_id_token", lambda id_token, meta, nonce: {"sub": sub, "email": email, "email_verified": True, "name": "Pat"})
    client.cookies.clear()
    start = await client.get("/moks/api/auth/oidc/login", follow_redirects=False)
    assert start.status_code == 302 and start.headers["location"].startswith("https://idp.example/authorize?"), start.text
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    return await client.get("/moks/api/auth/oidc/callback", params={"code": "one-time-code", "state": state}, follow_redirects=False)


async def _subject_of(email: str):
    from sqlalchemy import select

    from moks.db import session_scope
    from moks.models import User

    async with session_scope() as db:
        return (await db.execute(select(User.subject).where(User.email == email))).scalar_one()


async def test_oidc_account_belongs_to_the_subject_that_created_it(client, monkeypatch):
    """An email claim is not an identity. A second subject presenting an existing account's email used to be
    signed in as that account, with its roles."""
    email = "pat@oidc.example"
    first = await _oidc_sign_in(client, monkeypatch, sub="subject-a", email=email)
    assert first.status_code == 302 and first.headers["location"] == "/moks/", first.headers.get("location")
    assert (await client.get("/moks/api/auth/me")).json()["email"] == email
    assert await _subject_of(email) == "subject-a"

    other = await _oidc_sign_in(client, monkeypatch, sub="subject-b", email=email)
    assert other.status_code == 302 and other.headers["location"] == "/moks/login?error=identity_conflict", other.headers.get("location")
    assert (await client.get("/moks/api/auth/me")).status_code == 401
    assert await _subject_of(email) == "subject-a"

    again = await _oidc_sign_in(client, monkeypatch, sub="subject-a", email=email)
    assert again.headers["location"] == "/moks/" and (await client.get("/moks/api/auth/me")).json()["email"] == email


async def test_unlinked_oidc_account_takes_the_next_subject_that_signs_in(client, monkeypatch):
    """`moks unlink-identity` is the way back for a person whose subject changed at the provider."""
    from sqlalchemy import select

    from moks.cli import _unlink_identity
    from moks.db import session_scope
    from moks.models import AuditEvent

    email = "moved@oidc.example"
    assert (await _oidc_sign_in(client, monkeypatch, sub="before-the-move", email=email)).headers["location"] == "/moks/"
    signed_in = dict(client.cookies)
    await _unlink_identity(email)
    assert await _subject_of(email) is None
    client.cookies.update(signed_in)
    assert (await client.get("/moks/api/auth/me")).status_code == 401  # unlinking ended the sessions it had

    assert (await _oidc_sign_in(client, monkeypatch, sub="after-the-move", email=email)).headers["location"] == "/moks/"
    assert await _subject_of(email) == "after-the-move"
    async with session_scope() as db:
        actions = (await db.execute(select(AuditEvent.action).where(AuditEvent.actor_email.in_([email, "cli"]), AuditEvent.action.in_(["user.unlink_identity", "user.link_identity"])))).scalars().all()
    assert "user.unlink_identity" in actions and "user.link_identity" in actions


async def test_oidc_refuses_an_email_that_belongs_to_a_password_account(client, monkeypatch):
    from moks.cli import _create_user

    await _create_user("local-first@oidc.example", "a-very-long-password", ["viewer"])  # created in the suite's own mode, not oidc
    r = await _oidc_sign_in(client, monkeypatch, sub="subject-c", email="local-first@oidc.example")
    assert r.headers["location"] == "/moks/login?error=account_conflict"
    assert (await client.get("/moks/api/auth/me")).status_code == 401
