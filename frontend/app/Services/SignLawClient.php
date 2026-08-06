<?php

declare(strict_types=1);

namespace App\Services;

use Illuminate\Http\Client\ConnectionException;
use Illuminate\Support\Facades\Http;
use Illuminate\Support\Facades\Log;
use RuntimeException;

/**
 * Client for the SignLaw FastAPI backend.
 *
 * Laravel owns the interface and nothing else. Retrieval, embeddings, vector
 * search, citation verification and confidence scoring all live in FastAPI, and
 * this class exists so that boundary stays visible: if logic about bylaws ever
 * appears in this file, something has been put in the wrong tier.
 *
 * There is no province logic here either. Coverage is whatever
 * /api/v1/municipalities returns, so adding Alberta means ingesting PDFs, not
 * editing PHP.
 *
 * Calls are server-to-server. The browser never contacts the backend, which is
 * what keeps the API key out of the page and removes CORS from the picture
 * entirely.
 */
final class SignLawClient
{
    public function __construct(
        private readonly string $baseUrl,
        private readonly int $timeout,
        private readonly int $coverageCacheSeconds,
        private readonly ?string $apiKey = null,
    ) {
    }

    /**
     * Provinces and their municipalities, with real availability.
     *
     * Cached briefly: the corpus changes at ingestion time, not per request,
     * and every page view would otherwise run a grouped count over documents.
     *
     * @return array{provinces: array<int, array<string, mixed>>, total_available: int}
     */
    public function coverage(): array
    {
        return cache()->remember(
            'signlaw.coverage',
            $this->coverageCacheSeconds,
            fn (): array => $this->get('/api/v1/municipalities'),
        );
    }

    /**
     * Ask a question about a municipal sign bylaw.
     *
     * Abstentions come back as HTTP 200 with `answered: false`. That is not an
     * error and must not be raised as one — "the bylaw does not address this"
     * is a useful answer, and the caller renders it differently from a failure.
     *
     * @return array<string, mixed>
     */
    public function ask(string $question, ?string $municipality = null): array
    {
        $payload = ['question' => $question];

        if ($municipality !== null && $municipality !== '') {
            $payload['municipality'] = $municipality;
        }

        try {
            $response = Http::withHeaders($this->headers())
                ->timeout($this->timeout)
                ->acceptJson()
                ->post($this->baseUrl.'/api/v1/ask', $payload);
        } catch (ConnectionException $exception) {
            Log::error('signlaw.ask.unreachable', ['error' => $exception->getMessage()]);

            throw new RuntimeException(
                'The answering service is unreachable. Please try again shortly.',
                previous: $exception,
            );
        }

        // 503 means the model or index is down — our problem, and worth saying
        // plainly rather than dressing up as an empty answer.
        if ($response->status() === 503) {
            throw new RuntimeException(
                $response->json('detail')
                    ?? 'The answering service is temporarily unavailable.',
            );
        }

        // A rejected key is a deployment fault, not something the visitor did.
        // Logged loudly and reported vaguely, because the detail is ours.
        if ($response->status() === 401 || $response->status() === 403) {
            Log::error('signlaw.ask.unauthorised', ['status' => $response->status()]);

            throw new RuntimeException(
                'The answering service rejected this application. '
                .'Check SIGNLAW_API_KEY.',
            );
        }

        if ($response->status() === 404) {
            throw new RuntimeException(
                'That municipality is not recognised. Choose one from the list.',
            );
        }

        if ($response->failed()) {
            Log::error('signlaw.ask.failed', [
                'status' => $response->status(),
                'body' => $response->body(),
            ]);

            throw new RuntimeException('The question could not be processed.');
        }

        return $response->json();
    }

    /**
     * Headers for every backend call.
     *
     * The API key is what stops the GPU being a free service for anyone who
     * finds the address: a single question occupies the card for seconds.
     *
     * @return array<string, string>
     */
    private function headers(): array
    {
        return $this->apiKey === null || $this->apiKey === ''
            ? []
            : ['X-API-Key' => $this->apiKey];
    }

    /**
     * @return array<string, mixed>
     */
    private function get(string $path): array
    {
        try {
            $response = Http::withHeaders($this->headers())
                ->timeout($this->timeout)
                ->acceptJson()
                ->get($this->baseUrl.$path);
        } catch (ConnectionException $exception) {
            Log::error('signlaw.get.unreachable', [
                'path' => $path,
                'error' => $exception->getMessage(),
            ]);

            // Coverage failing should degrade the page, not break it, so an
            // empty catalogue is returned and the view explains itself.
            return ['provinces' => [], 'total_available' => 0];
        }

        if ($response->failed()) {
            Log::error('signlaw.get.failed', [
                'path' => $path,
                'status' => $response->status(),
            ]);

            return ['provinces' => [], 'total_available' => 0];
        }

        return $response->json();
    }
}
