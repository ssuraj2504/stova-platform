import asyncio
import base64
from contextlib import aclosing
from datetime import timedelta, timezone
import json
import anyio
from time import perf_counter
from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only, selectinload
from .auth import current_user
from .db import get_session
from .errors import AppError
from .models import Article, Project, User, new_id, utcnow
from .schemas import ArticlePatch, BrandPatch, KeyPayload, Payload, Polish, ProjectCreate, TopicsPatch, validate_logo
from .serialization import article_json, count_words, project_json, project_summary, slugify, stamp

router = APIRouter(prefix="/api", tags=["Publishing"])
SILENT_REQUEST_DEADLINE_SECONDS = 50.0
SILENT_CLEANUP_GRACE_SECONDS = 5.0
SSE_KEEPALIVE_SECONDS = 15.0
STREAM_CLEANUP_TIMEOUT_SECONDS = 10.0


async def owned_project(session: AsyncSession, user: User | str, project_id: str, lock: bool = False) -> Project:
    owner = user.id if isinstance(user, User) else user
    query = select(Project).where(Project.id == project_id, Project.owner_id == owner).options(selectinload(Project.articles)).execution_options(populate_existing=True)
    if lock:
        query = query.with_for_update()
    project = await session.scalar(query)
    if not project:
        raise AppError("Publication not found.", 404)
    return project


def find_article(project: Project, article_id: str) -> Article:
    article = next((article for article in project.articles if article.id == article_id), None)
    if not article:
        raise AppError("Article not found.", 404)
    return article


def writable(project: Project, article: Article | None = None) -> None:
    if project.status == "published" or article and article.status == "published":
        raise AppError("This article or publication is already live. Its published edition cannot be edited.", 409)


def valid_content(content: str) -> int:
    words = count_words(content)
    if not 100 <= words <= 3000:
        raise AppError("Keep your article between 100 and 3,000 words.")
    return words


def distinct_slugs(topics) -> list[str]:
    if len({topic.title.lower() for topic in topics}) != len(topics):
        raise AppError("Each blog topic must be different.")
    used = set()
    slugs = []
    for topic in topics:
        base = slugify(topic.title)
        slug, suffix = base, 2
        while slug in used:
            slug = f"{base}-{suffix}"
            suffix += 1
        used.add(slug)
        slugs.append(slug)
    return slugs


@router.get("/projects")
async def projects_get(user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    # The workspace shows the 100 most recently updated publications. Heavy fields
    # are deferred in SQL, so saved logos and article bodies never enter this list.
    query = (
        select(Project, (Project.logo.is_not(None) & (Project.logo != "")).label("has_logo"))
        .where(Project.owner_id == user.id)
        .options(
            load_only(Project.id, Project.name, Project.color, Project.audience, Project.tone, Project.status, Project.created_at, Project.updated_at, raiseload=True),
            selectinload(Project.articles).load_only(Article.id, Article.title, Article.slug, Article.status, Article.word_count, Article.error, Article.updated_at, Article.tag, Article.drop_cap, Article.position, raiseload=True),
        )
        .order_by(Project.updated_at.desc(), Project.id.desc())
        .limit(100)
    )
    projects = (await session.execute(query)).all()
    return {"projects": [project_summary(project, has_logo) for project, has_logo in projects]}


@router.post("/projects", status_code=201)
async def projects_post(body: ProjectCreate, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    date = utcnow()
    project = Project(id=new_id(), owner_id=user.id, **body.model_dump(exclude={"topics"}), status="draft", created_at=date, updated_at=date)
    slugs = distinct_slugs(body.topics)
    project.articles = [Article(id=new_id(), title=topic.title, slug=slugs[index], tag=topic.tag, constraints=topic.constraints, position=index, content="", status="draft", word_count=0, drop_cap=False, updated_at=date) for index, topic in enumerate(body.topics)]
    session.add(project)
    await session.commit()
    return {"project": project_json(project)}


@router.get("/projects/{project_id}")
async def project_get(project_id: str, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    return {"project": project_json(await owned_project(session, user, project_id))}


@router.get("/projects/{project_id}/logo")
async def project_logo_get(project_id: str, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    logo = await session.scalar(select(Project.logo).where(Project.id == project_id, Project.owner_id == user.id))
    try:
        if not validate_logo(logo):
            raise ValueError("No logo")
        metadata, encoded = logo.split(",", 1)
        raw = base64.b64decode(encoded, validate=True)
    except ValueError:
        raise AppError("Logo not found.", 404) from None
    return Response(content=raw, media_type=metadata[5:].split(";", 1)[0], headers={"Cache-Control": "private, no-store"})


@router.patch("/projects/{project_id}")
async def project_patch(project_id: str, body: BrandPatch, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    project = await owned_project(session, user, project_id, True)
    writable(project)
    if any(article.status == "published" for article in project.articles):
        raise AppError("This publication has live articles. Its brand details are locked for this edition.", 409)
    for key, value in body.model_dump(exclude_unset=True).items():
        if value is None and key != "logo":
            raise AppError(f"{key.capitalize()} cannot be empty.")
        setattr(project, key, value.upper() if key == "color" else value)
    project.updated_at = utcnow()
    await session.commit()
    return {"project": project_json(project)}


@router.delete("/projects/{project_id}")
async def project_delete(project_id: str, body: Payload, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    project = await owned_project(session, user, project_id, True)
    await session.delete(project)
    await session.commit()
    return {"deleted": True}


@router.patch("/projects/{project_id}/topics")
async def topics_patch(project_id: str, body: TopicsPatch, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    project = await owned_project(session, user, project_id, True)
    writable(project)
    date = utcnow()
    stale = date - timedelta(minutes=5)
    if any(article.content or article.status not in ("draft", "error") for article in project.articles):
        raise AppError("Topics are locked once an article has been written. Create a new publication for another brief.", 409)
    for article in project.articles:
        started = article.generation_started_at.replace(tzinfo=timezone.utc) if article.generation_started_at and article.generation_started_at.tzinfo is None else article.generation_started_at
        if article.generation_token and (not started or started >= stale):
            raise AppError("An article is being written. Wait for it to finish before changing topics.", 409)
    ids = [topic.id for topic in body.articles]
    if len(ids) != len(project.articles) or len(set(ids)) != len(ids) or set(ids) != {article.id for article in project.articles}:
        raise AppError("Use each existing topic exactly once.")
    slugs = distinct_slugs(body.articles)
    for article in project.articles:
        article.slug = f"~pending~{article.id}"
    await session.flush()
    for index, topic in enumerate(body.articles):
        article = find_article(project, topic.id)
        article.title, article.tag, article.constraints = topic.title, topic.tag, topic.constraints
        article.position, article.slug, article.updated_at = index, slugs[index], date
        article.status, article.error, article.generation_token, article.generation_started_at = "draft", None, None, None
    project.updated_at = date
    await session.commit()
    return {"project": project_json(project)}


@router.patch("/projects/{project_id}/articles/{article_id}")
async def article_patch(project_id: str, article_id: str, body: ArticlePatch, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    project = await owned_project(session, user, project_id, True)
    article = find_article(project, article_id)
    writable(project, article)
    article.word_count = valid_content(body.content)
    article.content, article.status, article.error = body.content, "ready", None
    if body.dropCap is not None:
        article.drop_cap = body.dropCap
    article.updated_at = project.updated_at = utcnow()
    article.generation_token = article.generation_started_at = None
    await session.commit()
    return {"article": article_json(article)}


async def lock_generation(session, user, project_id, article_id) -> dict:
    project = await owned_project(session, user, project_id, True)
    article = find_article(project, article_id)
    writable(project, article)
    date = utcnow()
    started = article.generation_started_at.replace(tzinfo=timezone.utc) if article.generation_started_at and article.generation_started_at.tzinfo is None else article.generation_started_at
    if article.generation_token and (not started or started >= date - timedelta(minutes=5)):
        raise AppError("This article is already being written. Please give it a moment.", 409)
    token = new_id()
    article.generation_token, article.generation_started_at = token, date
    article.updated_at = project.updated_at = date
    result = {"owner": user.id, "project": project_json(project), "article": article_json(article), "token": token}
    await session.commit()
    return result


async def finish_generation(database, generation, written=None, error: AppError | None = None):
    async with database.sessions() as session:
        project = await owned_project(session, generation["owner"], generation["project"]["id"], True)
        article = find_article(project, generation["article"]["id"])
        if article.generation_token != generation["token"] or article.status == "published":
            if error:
                return None
            raise AppError("This draft changed while it was being written. Your latest edits have been kept.", 409)
        if error:
            article.status = "ready" if article.content else "error"
            article.error = error.message
        else:
            article.content, article.word_count = written["content"], written["wordCount"]
            article.status, article.error = "ready", None
        article.generation_token = article.generation_started_at = None
        article.updated_at = project.updated_at = utcnow()
        await session.commit()
        return article_json(article)


def writing_error(error: Exception) -> AppError:
    return error if isinstance(error, AppError) else AppError("The writing service was interrupted. Your saved work is safe; please retry later.", 502)


async def keepalive_events(iterator):
    """Keep one provider read alive across heartbeats, and own its cancellation."""
    pending = None

    async def next_event():
        return await anext(iterator)

    try:
        while True:
            pending = asyncio.create_task(next_event(), name="stova-provider-next")
            while not pending.done():
                done, _ = await asyncio.wait({pending}, timeout=SSE_KEEPALIVE_SECONDS)
                if not done:
                    yield None
            try:
                event = pending.result()
            except StopAsyncIteration:
                return
            pending = None
            yield event
    finally:
        # A disconnected Starlette response may be inside a cancelled AnyIO
        # scope. Await the provider task before closing its async generator;
        # otherwise aclose races an outstanding __anext__ call.
        with anyio.move_on_after(STREAM_CLEANUP_TIMEOUT_SECONDS, shield=True):
            if pending is not None:
                if not pending.done():
                    pending.cancel()
                try:
                    await pending
                except (asyncio.CancelledError, StopAsyncIteration):
                    pass
                except Exception:
                    pass
            close = getattr(iterator, "aclose", None)
            if close is not None:
                await close()


@router.post("/projects/{project_id}/articles/{article_id}/generate")
async def article_generate(project_id: str, article_id: str, body: KeyPayload, request: Request, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    generation = None
    deadline = asyncio.get_running_loop().time() + SILENT_REQUEST_DEADLINE_SECONDS
    # Reserve cleanup inside the 50-second response window. A shorter deadline
    # (for example in tests) must still leave useful time to commit lock release.
    cleanup_grace = min(SILENT_CLEANUP_GRACE_SECONDS, SILENT_REQUEST_DEADLINE_SECONDS / 2)
    try:
        async with asyncio.timeout_at(deadline - cleanup_grace):
            generation = await lock_generation(session, user, project_id, article_id)
            async with aclosing(request.app.state.writer.generate(generation["project"], generation["article"], body.apiKey)) as events:
                async for event, payload in events:
                    if event == "result":
                        return {"article": await finish_generation(request.app.state.db, generation, payload)}
            raise AppError("Writing ended before a complete draft was returned.", 502)
    except TimeoutError:
        error = AppError("Writing took too long to finish. Your saved draft has been kept; please retry this article.", 504)
        if generation is not None:
            remaining = max(0, deadline - asyncio.get_running_loop().time())
            try:
                with anyio.move_on_after(remaining, shield=True):
                    await finish_generation(request.app.state.db, generation, error=error)
            except Exception:
                # Cleanup cannot extend the deadline or expose a storage error
                # in place of this request's safe timeout response.
                pass
        raise error from None
    except asyncio.CancelledError:
        if generation is not None:
            with anyio.move_on_after(STREAM_CLEANUP_TIMEOUT_SECONDS, shield=True):
                await finish_generation(request.app.state.db, generation, error=AppError("Writing was stopped. Your saved draft has been kept.", 409))
        raise
    except Exception as exc:
        error = writing_error(exc)
        if generation is not None:
            with anyio.move_on_after(max(0, deadline - asyncio.get_running_loop().time()), shield=True):
                await finish_generation(request.app.state.db, generation, error=error)
        raise error


@router.post("/projects/{project_id}/articles/{article_id}/stream")
async def article_stream(project_id: str, article_id: str, body: KeyPayload, request: Request, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    generation = await lock_generation(session, user, project_id, article_id)

    async def events():
        saved = False
        try:
            async with aclosing(keepalive_events(request.app.state.writer.generate(generation["project"], generation["article"], body.apiKey))) as provider_events:
                async for event_payload in provider_events:
                    if event_payload is None:
                        yield ": keepalive\n\n"
                        continue
                    event, payload = event_payload
                    if event == "result":
                        payload = {"article": await finish_generation(request.app.state.db, generation, payload)}
                        saved, event = True, "complete"
                    yield f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = writing_error(exc)
            try:
                await finish_generation(request.app.state.db, generation, error=error)
                saved = True
            except Exception:
                pass
            yield f"event: error\ndata: {json.dumps({'error': error.message, 'status': error.status, 'code': error.status})}\n\n"
        finally:
            if not saved:
                try:
                    # Starlette cancels the whole response task group on a client
                    # disconnect. Await cleanup inside a shielded AnyIO scope;
                    # spawning an asyncio shield task could leave the lock behind.
                    with anyio.move_on_after(STREAM_CLEANUP_TIMEOUT_SECONDS, shield=True):
                        await finish_generation(request.app.state.db, generation, error=AppError("Writing was stopped. Your saved draft has been kept.", 409))
                except Exception:
                    pass
    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "private, no-store, no-transform", "X-Accel-Buffering": "no"})


@router.post("/projects/{project_id}/articles/{article_id}/polish")
async def article_polish(project_id: str, article_id: str, body: Polish, request: Request, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    try:
        async with asyncio.timeout(SILENT_REQUEST_DEADLINE_SECONDS):
            project = await owned_project(session, user, project_id)
            article = find_article(project, article_id)
            writable(project, article)
            valid_content(body.content)
            return await request.app.state.writer.polish(project_json(project), article_json(article), body.apiKey, body.action, body.content, body.section, body.style)
    except TimeoutError:
        raise AppError("The editorial revision took too long. Your saved article has not changed; please retry a smaller section.", 504) from None


@router.post("/projects/{project_id}/articles/{article_id}/publish")
async def article_publish(project_id: str, article_id: str, body: Payload, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    project = await owned_project(session, user, project_id, True)
    article = find_article(project, article_id)
    if article.status != "published":
        writable(project, article)
        if article.status != "ready" or article.generation_token:
            raise AppError("Finish this article and wait for writing to stop before publishing it.", 409)
        article.status = "published"
        article.updated_at = project.updated_at = utcnow()
        project.status = "published" if all(item.status == "published" for item in project.articles) else "draft"
        await session.commit()
    return {"project": project_json(project), "article": article_json(article), "url": f"/blog/{project.id}/{article.slug}"}


@router.post("/projects/{project_id}/publish")
async def project_publish(project_id: str, body: Payload, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    project = await owned_project(session, user, project_id, True)
    if project.status != "published":
        if not project.articles or any(article.status not in ("ready", "published") or article.generation_token for article in project.articles):
            raise AppError("Finish every article before publishing your collection.", 409)
        date = utcnow()
        for article in project.articles:
            if article.status != "published":
                article.status, article.updated_at = "published", date
        project.status, project.updated_at = "published", date
        await session.commit()
    return {"project": project_json(project), "url": f"/blog/{project.id}"}


async def public_project(session, project_id) -> Project:
    project = await session.scalar(select(Project).where(Project.id == project_id).options(selectinload(Project.articles)))
    if not project or not any(article.status == "published" for article in project.articles):
        raise AppError("Publication not found.", 404)
    return project


@router.get("/publications/{project_id}")
async def publication_get(project_id: str, session: AsyncSession = Depends(get_session)):
    return {"project": project_json(await public_project(session, project_id), True)}


@router.get("/publications/{project_id}/articles/{slug}")
async def publication_article_get(project_id: str, slug: str, session: AsyncSession = Depends(get_session)):
    project = await public_project(session, project_id)
    article = next((article for article in project.articles if article.slug == slug and article.status == "published"), None)
    if not article:
        raise AppError("Article not found.", 404)
    result = article_json(article)
    result["constraints"] = ""
    return {"project": project_json(project, True), "article": result}


@router.get("/writing-status")
async def writing_status(request: Request, user: User = Depends(current_user)):
    return await request.app.state.writer.status()


async def health(request, session, key=None, probe=False):
    started, db_started = perf_counter(), perf_counter()
    try:
        await session.execute(text("SELECT 1"))
        database = {"status": "connected", "durationMs": round((perf_counter() - db_started) * 1000)}
    except Exception:
        await session.rollback()
        database = {"status": "unavailable", "durationMs": round((perf_counter() - db_started) * 1000)}
    state = await request.app.state.writer.status()
    writer = await request.app.state.writer.probe(key) if probe else {"provider": state["provider"], "status": state["status"], "usage": state["cloudflare"]["usage"], "model": state["cloudflare"]["model"], "generationVerified": False}
    groq = {key: value for key, value in writer.items() if key in ("status", "latencyMs", "error")} if writer["provider"] == "groq" else {"status": "not_configured"}
    timing = round((perf_counter() - started) * 1000)
    return {"worker": {"status": "operational", "durationMs": timing}, "api": {"status": "operational", "durationMs": timing}, "database": database, "groq": groq, "writer": writer, "checkedAt": stamp(utcnow())}


@router.get("/health")
async def health_get(request: Request, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    return await health(request, session)


@router.post("/health")
async def health_post(body: KeyPayload, request: Request, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    return await health(request, session, body.apiKey, True)
