# SignLaw AI — frontend

One-page Laravel Blade application. No React, Vue, Node or Vite; no build step.
Tailwind comes from a CDN and the only JavaScript is a dependent dropdown and a
double-submit guard.

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

## Files

These are the application-specific files. They overlay a stock Laravel skeleton.

```
frontend/
├── app/Http/Controllers/AskController.php   the only controller
├── app/Services/SignLawClient.php           the only coupling to FastAPI
├── config/signlaw.php                       API URL, timeout, cache
├── routes/web.php                           GET / and POST /
└── resources/views/ask/index.blade.php      the whole page
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

Register the client as a singleton in `bootstrap/providers.php` or
`AppServiceProvider::register()`:

```php
$this->app->singleton(\App\Services\SignLawClient::class, fn () => new \App\Services\SignLawClient(
    config('signlaw.api_url'),
    config('signlaw.timeout'),
    config('signlaw.coverage_cache_seconds'),
));
```

Then configure and run:

```bash
echo 'SIGNLAW_API_URL=http://localhost:8000' >> .env
php artisan serve --port=8080
```

With the FastAPI backend running:

```bash
cd ../backend && .venv/bin/uvicorn app.main:app --port 8000
```

Open <http://localhost:8080>.

## Timeouts

`SIGNLAW_API_TIMEOUT` defaults to 120 seconds, which looks excessive and is not.
A cold Ollama model load costs 10–30 seconds, and it is paid on the first
question after any idle period. A conventional 30-second timeout fails that
request and reads as a broken product. `OLLAMA_KEEP_ALIVE=30m` on the backend is
what actually keeps it rare.

## What is deliberately missing

**Source PDF links.** Citations carry `source_url: null` because serving the
original documents is Phase 6. A link that 404s is worse than no link — it reads
as evidence that has been checked. The municipality, bylaw number, section and
page are enough to find the passage manually in the meantime.

**Sessions and history.** Each question is independent. The backend records a
trace for every answer, so the audit trail exists; surfacing it is Phase 6.
