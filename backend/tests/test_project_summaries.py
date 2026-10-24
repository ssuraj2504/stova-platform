import base64
from datetime import timedelta

from sqlalchemy import event, select

from app.models import Project, new_id, utcnow
from .helpers import LOGO, article_path, create_project, project_payload, register


async def test_list_omits_large_saved_payloads_without_loading_them(client, application, provider):
    await register(client)
    # A complete PNG with permitted trailing padding reaches the upload limit.
    small_png = base64.b64decode(LOGO.split(",", 1)[1])
    raw_logo = small_png + b"\0" * (1_048_576 - len(small_png))
    logo = "data:image/png;base64," + base64.b64encode(raw_logo).decode()
    topics = [{"title": f"Editorial perspective {index}", "tag": "Journal", "constraints": "c" * 1000} for index in range(10)]
    response = await client.post("/api/projects", json={**project_payload(topics), "logo": logo})
    assert response.status_code == 201, response.text[:300]
    project = response.json()["project"]
    content = "narrative " * 2999 + "narrative"
    assert len(content) == 29_999
    for index in range(10):
        saved = await client.patch(article_path(project, index), json={"content": content, "dropCap": index == 0})
        assert saved.status_code == 200, saved.text[:300]

    statements = []

    def record_statement(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    engine = application.state.db.engine.sync_engine
    event.listen(engine, "before_cursor_execute", record_statement)
    try:
        listing = await client.get("/api/projects")
    finally:
        event.remove(engine, "before_cursor_execute", record_statement)
    assert listing.status_code == 200, listing.text[:300]
    assert len(listing.content) < 10_000, "Logo and article sizes must not enlarge the workspace list."
    assert "articles.content" not in "\n".join(statements)
    assert "articles.constraints" not in "\n".join(statements)
    # Only a boolean presence expression may refer to the logo in this query.
    project_query = next(statement for statement in statements if "FROM projects" in statement)
    assert "projects.logo IS NOT NULL" in project_query
    assert ", projects.logo," not in project_query and ", projects.logo \n" not in project_query

    summary = listing.json()["projects"][0]
    assert set(summary) == {"id", "name", "color", "logo", "audience", "tone", "status", "createdAt", "updatedAt", "articles"}
    assert summary["logo"] == f"/api/projects/{project['id']}/logo"
    assert len(summary["articles"]) == 10
    assert set(summary["articles"][0]) == {"id", "title", "slug", "status", "wordCount", "error", "updatedAt", "tag", "dropCap"}
    assert all(article["status"] == "ready" and article["wordCount"] == 3000 for article in summary["articles"])
    assert summary["articles"][0]["dropCap"] is True

    detail = await client.get(f"/api/projects/{project['id']}")
    assert detail.status_code == 200 and len(detail.content) > 1_600_000
    assert detail.json()["project"]["logo"] == logo
    assert all(article["content"] == content and article["constraints"] == "c" * 1000 for article in detail.json()["project"]["articles"])
    image = await client.get(summary["logo"])
    assert image.status_code == 200 and image.content == raw_logo
    assert image.headers["content-type"] == "image/png"
    assert image.headers["cache-control"] == "private, no-store"
    assert image.headers["x-content-type-options"] == "nosniff"
    assert provider.requests == []


async def test_private_logo_does_not_expose_other_owners_missing_or_corrupt_images(users, database):
    alice, bob, anonymous = users["alice"], users["bob"], users["anonymous"]
    project = await create_project(alice)
    no_logo = await alice.post("/api/projects", json={**project_payload(), "logo": None})
    assert no_logo.status_code == 201
    no_logo_id = no_logo.json()["project"]["id"]
    logo_path = f"/api/projects/{project['id']}/logo"
    image = await alice.get(logo_path)
    assert image.status_code == 200 and image.content == base64.b64decode(LOGO.split(",", 1)[1])
    for requester, path, expected_status in [
        (bob, logo_path, 404),
        (anonymous, logo_path, 401),
        (alice, f"/api/projects/{new_id()}/logo", 404),
        (alice, f"/api/projects/{no_logo_id}/logo", 404),
    ]:
        response = await requester.get(path)
        assert response.status_code == expected_status
        assert response.headers["cache-control"] == "private, no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["content-type"].startswith("application/json")
        assert response.json().get("error") and "base64" not in response.text
    summaries = (await alice.get("/api/projects")).json()["projects"]
    assert next(item for item in summaries if item["id"] == no_logo_id)["logo"] is None

    async with database.sessions() as session:
        stored = await session.scalar(select(Project).where(Project.id == project["id"]))
        stored.logo = "data:image/png;base64," + base64.b64encode(b"<script>private invalid image</script>").decode()
        await session.commit()
    invalid = await alice.get(logo_path)
    assert invalid.status_code == 404 and "script" not in invalid.text
    assert invalid.headers["cache-control"] == "private, no-store"


async def test_list_returns_only_the_100_most_recent_owned_projects(client, database):
    user = await register(client)
    start = utcnow() - timedelta(days=1)
    records = []
    async with database.sessions() as session:
        for index in range(101):
            project = Project(id=new_id(), owner_id=user["id"], name=f"Publication {index}", color="#123456", logo=None, audience="", tone="Clear", status="draft", created_at=start, updated_at=start + timedelta(seconds=index), articles=[])
            records.append(project)
            session.add(project)
        await session.commit()
    response = await client.get("/api/projects")
    assert response.status_code == 200, response.text
    summaries = response.json()["projects"]
    assert len(summaries) == 100
    assert [project["id"] for project in summaries] == [project.id for project in reversed(records[1:])]
    assert all(project["logo"] is None and project["articles"] == [] for project in summaries)
