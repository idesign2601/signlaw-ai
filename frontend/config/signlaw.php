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
