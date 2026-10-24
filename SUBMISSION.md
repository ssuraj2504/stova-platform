# Stova submission guide

## Review links

- [Live publishing studio](https://stova.174.129.245.156.sslip.io/)
- [Published sample collection](https://stova.174.129.245.156.sslip.io/blog/d6a04213-ed79-48aa-8fb0-6a60de2868ef)
- [Interactive API documentation](https://stova.174.129.245.156.sslip.io/api/docs)

Create a new account for a private workspace. No shared login or personal writing key is required for the live service. Published article links are readable without an account.

## Requirements and implementation

| Requirement | Implementation |
| --- | --- |
| Company name, logo and hexadecimal brand colour | Brand panel with raster image validation, colour validation and saved publication branding |
| One to ten separate topics | Ordered topic inputs with optional tags and writing notes; server-side topic limits |
| Separate blog pages | One article route per topic and a collection route per publication |
| Approximately 1,000 words per generated article | Server validates 950–1,100 words, with one bounded revision attempt when needed |
| Consistent company layout | Shared public reader applies the saved logo, company name and brand colour |
| Online hosting | AWS EC2, HTTPS, Docker services and persistent PostgreSQL |
| Multiple uses | Separate user accounts and multiple independently branded publications |
| React, JavaScript, HTML and CSS | React with TypeScript, Vite and custom CSS; TypeScript compiles to JavaScript |
| Python backend and REST APIs | FastAPI application behind a Tornado static server and streaming reverse proxy |
| Database | PostgreSQL, SQLAlchemy and Alembic migrations |
| GenAI integration | Cloudflare Workers AI through server-side HTTPS requests, streaming and bounded usage |
| Docker, Git and CI/CD | Container builds, PostgreSQL integration tests, HTTP smoke checks and a manual AWS release workflow |

## Suggested review sequence

1. Open the sample collection and an article to check the public layout.
2. Register an account and sign in to the workspace.
3. Enter a company name, upload a small PNG/JPEG/WebP logo, and choose a six-digit hex colour.
4. Add one topic for an initial generation check. Larger batches share the same daily allowance.
5. Generate the draft, review it in the article studio, and optionally download Markdown.
6. Publish the article and open its public address in a signed-out browser.
7. Create another publication to verify independent branding and account ownership.

## Running the source

Follow [the root README](README.md) for Docker and native development setup. A fresh source copy contains configuration examples only. Its owner must supply server-side writing credentials to enable generation locally; the live reference service is already configured.

The default Docker address is `http://127.0.0.1:8080`. Database state persists in a named volume. Do not remove that volume unless discarding the local database is intentional.

## Verification

The verification workflow covers:

- Frontend regression tests, TypeScript checks and a production build.
- PostgreSQL account, ownership, publication, word-count, generation and usage-budget tests.
- Real Tornado HTTP proxy, streaming, cancellation and header-isolation checks.
- AWS Free-plan refusal logic, direct-hosting configuration and initial-import safety.
- Container account creation, draft persistence and anonymous public publishing.

The live deployment was checked on 24 October 2026: HTTPS assets, authenticated workspace access, anonymous published pages, database readiness, and a real 1,049-word generation and publication. Earlier source content was transferred with matching full-row hashes across all eight application tables.

## Scope and limits

- Generated drafts target about 1,000 words; exact 1,000-word output is not guaranteed. Manual edits are allowed, so reviewed content can differ in length.
- The shared writing connection has a conservative daily reservation limit across all visitors. Reaching it pauses new generation; saved work and public articles remain available while hosting is active.
- Hosting uses an AWS Free account plan and expiring credits. It is time-limited, and the reference deployment is a single-server demonstration rather than a highly available production service.
- The current publication library loads the most recent 100 publications.
- Keep the workspace open while a batch is generating. Generation is started by the browser; pending topics are not a background job queue.
- Verification includes automated integration tests and selected live workflows; it is not a complete cross-browser or accessibility certification.

## Submission contents

Submit this standalone source tree or its source-only ZIP. Include the configuration example, lockfile, migrations, tests, container files, deployment scripts and font license.

Exclude real environment files, database exports, local logs, runtime caches, dependency directories and Git internals. If submitting a private repository link, give the reviewer repository access separately. No credentials are needed to read public article URLs.
