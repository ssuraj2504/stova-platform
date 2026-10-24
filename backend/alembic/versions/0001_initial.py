"""Independent authentication, publishing records, and shared writing budget."""
from alembic import op
import sqlalchemy as sa

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("users", sa.Column("id", sa.String(36), primary_key=True), sa.Column("email", sa.String(254), nullable=False, unique=True), sa.Column("name", sa.String(100), nullable=False), sa.Column("password_hash", sa.String(300), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("auth_sessions", sa.Column("id", sa.String(36), primary_key=True), sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False), sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False), sa.Column("revoked", sa.Boolean(), nullable=False))
    op.create_index("ix_auth_sessions_user_id", "auth_sessions", ["user_id"])
    op.create_table("auth_throttles", sa.Column("scope", sa.String(64), primary_key=True), sa.Column("count", sa.Integer(), nullable=False), sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("projects", sa.Column("id", sa.String(36), primary_key=True), sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False), sa.Column("name", sa.String(100), nullable=False), sa.Column("color", sa.String(7), nullable=False), sa.Column("logo", sa.Text()), sa.Column("audience", sa.String(500), nullable=False), sa.Column("tone", sa.String(100), nullable=False), sa.Column("status", sa.String(12), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_projects_owner_id", "projects", ["owner_id"])
    op.create_table("articles", sa.Column("id", sa.String(36), primary_key=True), sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False), sa.Column("title", sa.String(200), nullable=False), sa.Column("slug", sa.String(120), nullable=False), sa.Column("content", sa.Text(), nullable=False), sa.Column("status", sa.String(12), nullable=False), sa.Column("word_count", sa.Integer(), nullable=False), sa.Column("error", sa.Text()), sa.Column("position", sa.Integer(), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False), sa.Column("tag", sa.String(80), nullable=False), sa.Column("constraints", sa.String(1000), nullable=False), sa.Column("drop_cap", sa.Boolean(), nullable=False), sa.Column("generation_token", sa.String(36)), sa.Column("generation_started_at", sa.DateTime(timezone=True)), sa.UniqueConstraint("project_id", "slug", name="uq_article_project_slug"))
    op.create_index("ix_articles_project_id", "articles", ["project_id"])
    op.create_table("writing_accounts", sa.Column("account_scope", sa.String(67), primary_key=True))
    op.create_table("writing_reservations", sa.Column("id", sa.String(36), primary_key=True), sa.Column("account_scope", sa.String(67), sa.ForeignKey("writing_accounts.account_scope"), nullable=False), sa.Column("utc_day", sa.String(10), nullable=False), sa.Column("adjacent_day", sa.String(10)), sa.Column("reserved_neurons", sa.Integer(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.CheckConstraint("reserved_neurons = 300", name="ck_writing_reservation_fixed"))
    op.create_index("ix_writing_scope_day", "writing_reservations", ["account_scope", "utc_day"])
    op.create_index("ix_writing_scope_adjacent", "writing_reservations", ["account_scope", "adjacent_day"])


def downgrade():
    for table in ("writing_reservations", "writing_accounts", "articles", "projects", "auth_throttles", "auth_sessions", "users"):
        op.drop_table(table)
