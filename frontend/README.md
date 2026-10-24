# Stova frontend

A standalone React and TypeScript publishing studio, built with Vite. The workspace, editor, topic matrix and public journal readers share the same literary design and local fonts.

## Development

Run the FastAPI backend on `127.0.0.1:8000`, then:

```sh
npm ci
npm run dev
```

Open the URL printed by Vite, normally `http://127.0.0.1:5173`. Vite forwards `/api` requests to the backend. Login and registration use the backend's HttpOnly session cookie; no account tokens, passwords or writing keys are stored in browser storage.

## Checks and production output

```sh
npm run typecheck
npm test
npm run build
```

The production bundle is written to `dist/`. The platform gateway serves it and forwards `/api`, including streaming generation responses, to FastAPI. Non-API paths must fall back to `dist/index.html` so public journal URLs work when opened directly.

Public routes are `/blog/:publicationId` and `/blog/:publicationId/:articleSlug`. They read published content from `/api/publications/:publicationId` and `/api/publications/:publicationId/articles/:articleSlug` without requiring a workspace login.

Shared writing credentials belong in the backend environment. The frontend has no deployment-specific secrets or required environment variables.

Font license: `public/fonts/OFL.txt`.
