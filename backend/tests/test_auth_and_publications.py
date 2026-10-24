from sqlalchemy import select

from app.auth import verify_password
from app.models import Article, User
from .helpers import FAKE_TOKEN, PASSWORD, article_content, article_path, create_project, project_payload, register, save_article


async def test_cookie_authentication_logout_and_password_semantics(client, application, database):
    password = "  correct horse battery staple  "
    response = await client.post("/api/auth/register", json={"email": "alice@example.com", "password": password, "name": "Alice"})
    assert response.status_code == 201, response.text
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "path=/" in cookie
    session_token = client.cookies.get(application.state.settings.cookie_name)
    assert (await client.get("/api/auth/me")).json()["user"]["email"] == "alice@example.com"
    async with database.sessions() as session:
        user = await session.scalar(select(User).where(User.email == "alice@example.com"))
        assert user.password_hash != password and user.password_hash.startswith("scrypt$")
        assert verify_password(password, user.password_hash)
        assert not verify_password(password.strip(), user.password_hash)
    assert (await client.post("/api/auth/logout", json={})).status_code == 200
    assert (await client.get("/api/auth/me")).status_code == 401
    client.cookies.set(application.state.settings.cookie_name, session_token)
    assert (await client.get("/api/auth/me")).status_code == 401, "A logged-out token must stay revoked when replayed."
    client.cookies.clear()
    assert (await client.post("/api/auth/login", json={"email": "alice@example.com", "password": password.strip()})).status_code == 401
    assert (await client.post("/api/auth/login", json={"email": "alice@example.com", "password": password})).status_code == 200
    assert (await client.post("/api/auth/register", json={"email": "ALICE@example.com", "password": PASSWORD})).status_code == 409


async def test_owner_isolation_ignores_old_identity_headers_and_blocks_csrf(users):
    alice, bob, anonymous = users["alice"], users["bob"], users["anonymous"]
    project = await create_project(alice)
    assert [item["id"] for item in (await alice.get("/api/projects")).json()["projects"]] == [project["id"]]
    assert (await bob.get("/api/projects")).json()["projects"] == []
    for method, path, body in [
        ("GET", f"/api/projects/{project['id']}", None),
        ("PATCH", f"/api/projects/{project['id']}", {"name": "Stolen publication"}),
        ("DELETE", f"/api/projects/{project['id']}", {}),
        ("POST", article_path(project) + "/generate", {}),
    ]:
        response = await bob.request(method, path, **({"json": body} if body is not None else {}))
        assert response.status_code == 404, response.text
    assert (await anonymous.get("/api/projects", headers={"oai-authenticated-user-id": users["alice_user"]["id"]})).status_code == 401
    assert (await alice.post("/api/projects", json=project_payload(), headers={"Origin": "https://unrelated.example"})).status_code == 403
    assert (await alice.post("/api/projects", json=project_payload(), headers={"Sec-Fetch-Site": "cross-site"})).status_code == 403
    assert (await alice.post("/api/projects", content="{}", headers={"Content-Type": "text/plain"})).status_code == 415


async def test_validation_and_collision_safe_topics(client, provider):
    await register(client)
    invalid = [
        {**project_payload(), "name": " "},
        {**project_payload(), "color": "red; background:url(bad)"},
        {**project_payload(), "topics": []},
        {**project_payload(), "topics": [f"Topic {i}" for i in range(11)]},
        {**project_payload(), "topics": ["Same topic", "same topic"]},
        {**project_payload(), "logo": "data:image/svg+xml;base64,PHN2Zy8+"},
    ]
    for body in invalid:
        response = await client.post("/api/projects", json=body)
        assert response.status_code == 400 and response.json().get("error"), response.text
    oversized = await client.post("/api/projects", json={**project_payload(), "audience": "x" * 1_600_000})
    assert oversized.status_code == 413
    project = await create_project(client, ["a", "a-3", "a?"])
    assert len({article["slug"] for article in project["articles"]}) == 3
    updated = [{"id": article["id"], "title": title, "tag": "", "constraints": ""} for article, title in zip(project["articles"], ["a?", "a", "a-3"])]
    response = await client.patch(f"/api/projects/{project['id']}/topics", json={"articles": updated})
    assert response.status_code == 200, response.text
    assert len({article["slug"] for article in response.json()["project"]["articles"]}) == 3
    assert provider.requests == []


async def test_public_readers_only_expose_published_articles(users, database, provider):
    alice, public = users["alice"], users["anonymous"]
    topics = [{"title": "Published perspective", "constraints": "PRIVATE_EDITORIAL_CONSTRAINTS"}, {"title": "PRIVATE_DRAFT_TOPIC"}]
    project = await create_project(alice, topics)
    public_path = f"/api/publications/{project['id']}"
    assert (await public.get(public_path)).status_code == 404
    assert (await public.get(public_path + f"/articles/{project['articles'][0]['slug']}")).status_code == 404
    await save_article(alice, project, 0, drop_cap=True)
    await save_article(alice, project, 1, article_content("PRIVATE_DRAFT_CONTENT"))
    published = await alice.post(article_path(project) + "/publish", json={})
    assert published.status_code == 200, published.text
    assert published.json()["project"]["status"] == "draft", "One published article must not publish the other draft."
    index = await public.get(public_path)
    assert index.status_code == 200
    assert len(index.json()["project"]["articles"]) == 1
    assert "PRIVATE_DRAFT" not in index.text and "PRIVATE_EDITORIAL_CONSTRAINTS" not in index.text and "PRIVATE_AUDIENCE_BRIEF" not in index.text
    assert "password" not in index.text.lower() and "owner_id" not in index.text and FAKE_TOKEN not in index.text
    article_response = await public.get(public_path + f"/articles/{project['articles'][0]['slug']}")
    assert article_response.status_code == 200 and article_response.json()["article"]["dropCap"] is True
    assert article_response.json()["project"]["logo"] == project["logo"]
    assert (await public.get(public_path + f"/articles/{project['articles'][1]['slug']}")).status_code == 404
    assert (await alice.patch(article_path(project), json={"content": article_content("Changed")})).status_code == 409
    assert (await alice.post(article_path(project) + "/generate", json={})).status_code == 409
    assert (await alice.post(article_path(project) + "/polish", json={"action": "rewrite", "content": article_content()})).status_code == 409
    assert (await alice.patch(f"/api/projects/{project['id']}", json={"color": "#123456"})).status_code == 409
    assert provider.requests == []
    collection = await alice.post(f"/api/projects/{project['id']}/publish", json={})
    assert collection.status_code == 200 and collection.json()["project"]["status"] == "published"
    assert len((await public.get(public_path)).json()["project"]["articles"]) == 2
    assert (await alice.request("DELETE", f"/api/projects/{project['id']}", json={})).status_code == 200
    assert (await public.get(public_path)).status_code == 404
    async with database.sessions() as session:
        assert not (await session.scalars(select(Article).where(Article.project_id == project["id"]))).all()


async def test_ten_topics_publish_to_ten_distinct_public_addresses(client):
    await register(client)
    project = await create_project(client, [f"Practical perspective {index + 1}" for index in range(10)])
    assert len(project["articles"]) == 10
    assert (await client.post(f"/api/projects/{project['id']}/publish", json={})).status_code == 409
    for index in range(10):
        await save_article(client, project, index)
    response = await client.post(f"/api/projects/{project['id']}/publish", json={})
    assert response.status_code == 200, response.text
    for article in project["articles"]:
        page = await client.get(f"/api/publications/{project['id']}/articles/{article['slug']}")
        assert page.status_code == 200 and page.json()["article"]["title"] == article["title"]
        assert page.json()["article"]["wordCount"] == 1000
