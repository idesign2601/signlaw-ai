# SignLaw AI — frontend

One-page Laravel Blade application. No React, Vue, Node or Vite; no build step.
Tailwind comes from a CDN and the only JavaScript is a dependent dropdown and a
double-submit guard.

Designed to sit on ordinary PHP hosting while the AI runs on a GPU elsewhere —
see `../docs/SPLIT_DEPLOYMENT.md`.

## What this tier does and does not do

| Laravel | FastAPI |
|---|---|
| Renders the page | Retrieval, embeddings, vector search |
| Validates input | Citation verification |
| Calls the API | Confidence scoring |
| Formats citations | Everything about bylaws |

If logic about bylaws ever appears in this directory, it is in the wrong tier.

**No province logic lives here.** The page renders whatever
`GET /api/v1/municipalities` returns. Adding Alberta means adding municipality
records and ingesting PDFs — no template, route, controller or JavaScript
change. The coverage list is likewise computed, not maintained: a municipality
appears as available only when in-force documents are actually indexed for it,
so the interface cannot advertise something the corpus cannot answer.

**The API key never reaches the browser.** Calls are server-to-server, which is
also why there is no CORS configuration and no mixed-content problem.

## Pages

| Route | What it is |
|---|---|
| `/` | Landing page |
| `/ask` | The question interface |
| `/zoning-check` | Placeholder. Renders text, calls nothing. |
| `/admin/login` | Single-password sign-in, throttled to six attempts a minute |
| `/admin` | Document dashboard — municipality, filename, pages, chunks, status, date |
| `/admin/upload` | Upload a bylaw PDF for indexing |

## Admin authentication

A single shared password in `ADMIN_PASSWORD`, checked in constant time and
remembered in the session. No users table, no registration, no password reset —
this application has no database of its own, and adding one to authenticate a
single operator would be the larger risk.

**That gate is convenience, not the real control.** The backend requires
`X-Admin-Key` on every admin route, and this application is the only thing that
holds it. A forged session here still cannot reach the API.

Unset `ADMIN_PASSWORD` disables admin sign-in entirely. That is the deliberate
default: a deployment nobody has configured should not have a working back door.

## Files

These are the application-specific files. They overlay a stock Laravel skeleton.

```
frontend/
├── app/Http/Controllers/
│   ├── AdminController.php                sign-in, dashboard, upload
│   ├── AskController.php                  question form and answer
│   └── PageController.php                 landing and the placeholder
├── app/Http/Middleware/RequireAdmin.php   session gate
├── app/Services/SignLawClient.php         the only coupling to FastAPI
├── config/signlaw.php                     API URL, keys, timeout, cache
├── routes/web.php
└── resources/views/
    ├── layouts/app.blade.php              shell, nav, footer, disclaimer
    ├── partials/coverage.blade.php        shared by landing and /ask
    ├── landing.blade.php
    ├── ask.blade.php
    ├── zoning-check.blade.php
    └── admin/
        ├── login.blade.php
        ├── dashboard.blade.php
        └── upload.blade.php
```

## Setup

Laravel's skeleton is not vendored here, so create it once and overlay:

```bash
cd signlaw-ai
composer create-project laravel/laravel frontend-skeleton
cp -r frontend/* frontend-skeleton/
rm -rf frontend && mv frontend-skeleton frontend
cd frontend
```

Register the client as a singleton in `AppServiceProvider::register()`:

```php
$this->app->singleton(\App\Services\SignLawClient::class, fn () => new \App\Services\SignLawClient(
    config('signlaw.api_url'),
    config('signlaw.timeout'),
    config('signlaw.coverage_cache_seconds'),
    config('signlaw.api_key'),
    config('signlaw.admin_key'),
));
```

Then configure and run:

```bash
cat >> .env <<'EOF'
SIGNLAW_API_URL=http://localhost:8000
SIGNLAW_API_KEY=
SIGNLAW_ADMIN_KEY=
ADMIN_PASSWORD=
EOF
php artisan serve --port=8080
```

Uploads are limited to 50 MB by the form. PHP itself caps them lower by default
— raise `upload_max_filesize` and `post_max_size` in `php.ini` (or your host's
panel) or large scanned bylaws will fail before Laravel ever sees them.

Leave `SIGNLAW_API_KEY` empty against a local backend: with
`ENVIRONMENT=local` and no `SECURITY__API_KEYS`, the backend allows
unauthenticated calls and logs a warning. Anything else requires the key.

With the backend running:

```bash
cd ../backend && .venv/bin/uvicorn app.main:app --port 8000
```

Open <http://localhost:8080>.

## Troubleshooting

**"Coverage is unavailable because the answering service could not be reached."**
`SIGNLAW_API_URL` is wrong, or the backend is down. The page degrades rather
than erroring, because a broken backend should not take the site with it.

**"The answering service rejected this application."**
`SIGNLAW_API_KEY` does not match any entry in the backend's
`SECURITY__API_KEYS`. Re-run `php artisan config:cache` if you just changed it.

**Changes to `.env` appear to do nothing.**
`config:cache` has baked the old values. Re-run it.

## What is deliberately missing

**Source PDF links.** Citations carry `source_url: null` because serving the
original documents is Phase 6. A link that 404s is worse than no link — it reads
as evidence that has been checked. The municipality, bylaw number, section and
page are enough to find the passage manually in the meantime.

**Sessions and history.** Each question is independent. The backend records a
trace for every answer, so the audit trail exists; surfacing it is Phase 6.
