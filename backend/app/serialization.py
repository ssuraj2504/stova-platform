from datetime import datetime, timezone
import re
import unicodedata
from .models import Article, Project, User


def stamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def count_words(content: str) -> int:
    content = re.sub(r"```[\s\S]*?```", " ", content)
    content = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", content)
    return len(re.sub(r"[#*_`>|~]", " ", content).split())


def slugify(title: str) -> str:
    title = "".join(char for char in unicodedata.normalize("NFKD", title) if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:80].rstrip("-") or "story"


def article_json(article: Article) -> dict:
    return {"id": article.id, "title": article.title, "slug": article.slug, "content": article.content, "status": article.status, "wordCount": article.word_count, "error": article.error, "updatedAt": stamp(article.updated_at), "tag": article.tag, "constraints": article.constraints, "dropCap": article.drop_cap}


def article_summary(article: Article) -> dict:
    return {"id": article.id, "title": article.title, "slug": article.slug, "status": article.status, "wordCount": article.word_count, "error": article.error, "updatedAt": stamp(article.updated_at), "tag": article.tag, "dropCap": article.drop_cap}


def project_summary(project: Project, has_logo: bool) -> dict:
    return {"id": project.id, "name": project.name, "color": project.color, "logo": f"/api/projects/{project.id}/logo" if has_logo else None, "audience": project.audience, "tone": project.tone, "status": project.status, "createdAt": stamp(project.created_at), "updatedAt": stamp(project.updated_at), "articles": [article_summary(article) for article in sorted(project.articles, key=lambda article: article.position)]}


def project_json(project: Project, public: bool = False) -> dict:
    articles = [article for article in project.articles if not public or article.status == "published"]
    values = [article_json(article) for article in sorted(articles, key=lambda article: article.position)]
    if public:
        for article in values:
            article["constraints"] = ""
    return {"id": project.id, "name": project.name, "color": project.color, "logo": project.logo, "audience": "" if public else project.audience, "tone": project.tone, "status": project.status, "createdAt": stamp(project.created_at), "updatedAt": stamp(project.updated_at), "articles": values}


def user_json(user: User) -> dict:
    return {"id": user.id, "email": user.email, "name": user.name}
