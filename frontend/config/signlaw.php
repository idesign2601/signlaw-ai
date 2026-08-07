<?php

declare(strict_types=1);

return [

    /*
    |--------------------------------------------------------------------------
    | FastAPI backend
    |--------------------------------------------------------------------------
    |
    | Laravel handles the interface; FastAPI handles retrieval, embeddings,
    | vector search and answers. This is the only coupling between them.
    |
    | In a split deployment this is the public address of the GPU host. Use
    | https:// — the question and the answer both travel over it.
    |
    */

    'api_url' => env('SIGNLAW_API_URL', 'http://localhost:8000'),

    /*
    | Sent as X-API-Key on every call. Server-to-server only; it never reaches
    | the browser. Without it the backend is an open inference endpoint, so the
    | backend refuses to boot in production unless keys are configured.
    |
    | Generate: openssl rand -hex 32
    */

    'api_key' => env('SIGNLAW_API_KEY'),

    /*
    | Sent as X-Admin-Key on document management calls. A different secret from
    | the one above on purpose: the API key lets a client ask questions, this
    | one lets someone change what the answers are made of.
    |
    | Generate: openssl rand -hex 32
    */

    'admin_key' => env('SIGNLAW_ADMIN_KEY'),

    /*
    | Password for the admin area of this application. Unset disables admin
    | sign-in entirely, which is the right default — a deployment that has not
    | been configured should not have a working back door.
    |
    | This gate is convenience, not the real control. The backend requires
    | X-Admin-Key on every admin route, and this application is the only thing
    | that holds it.
    */

    'admin_password' => env('ADMIN_PASSWORD'),

    /*
    | Generous by design. A cold Ollama model load costs 10–30 seconds, and a
    | reranked hybrid retrieval over a large corpus is not instant either.
    | Timing out at a conventional 30s would fail the first question after any
    | idle period, which reads as a broken product.
    */
    'timeout' => (int) env('SIGNLAW_API_TIMEOUT', 120),

    /*
    | Coverage changes only when documents are ingested, so it is cached. Short
    | enough that a freshly ingested municipality appears without a deploy.
    */
    'coverage_cache_seconds' => (int) env('SIGNLAW_COVERAGE_CACHE', 60),

];
