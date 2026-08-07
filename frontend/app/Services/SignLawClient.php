<?php

declare(strict_types=1);

namespace App\Services;

use Illuminate\Http\Client\ConnectionException;
use Illuminate\Http\UploadedFile;
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
        private readonly ?string $adminKey = null,
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
     * Every indexed document, plus uploads still processing.
     *
     * Not cached: an operator watching an upload index needs the current state,
     * and a stale dashboard would look like a stalled ingest.
     *
     * @return array{documents: array<int, array<string, mixed>>, pending: array<int, array<string, mixed>>, total: int}
     */
    public function documents(): array
    {
        $response = Http::withHeaders($this->adminHeaders())
            ->timeout($this->timeout)
            ->acceptJson()
            ->get($this->baseUrl.'/api/v1/admin/documents');

        if ($response->status() === 401 || $response->status() === 403) {
            throw new RuntimeException(
                'The backend rejected the admin key. Check SIGNLAW_ADMIN_KEY.',
            );
        }

        if ($response->failed()) {
            Log::error('signlaw.documents.failed', ['status' => $response->status()]);

            throw new RuntimeException('Could not load documents from the backend.');
        }

        return $response->json();
    }

    /**
     * Upload a bylaw PDF for indexing.
     *
     * Returns as soon as the backend accepts it. Extraction, OCR, chunking and
     * embedding continue in the background — a large scanned bylaw takes
     * minutes, and the dashboard is where that is watched.
     *
     * @return array<string, mixed>
     */
    public function uploadDocument(
        string $province,
        string $municipality,
        string $title,
        ?int $year,
        UploadedFile $file,
    ): array {
        // A plain associative array, not Guzzle's ['name' => …, 'contents' => …]
        // form, and no asMultipart(): attach() already switches the request to
        // multipart, and mixing the two produces a malformed body.
        $payload = [
            'province' => $province,
            'municipality' => $municipality,
            'title' => $title,
        ];

        if ($year !== null) {
            $payload['year'] = (string) $year;
        }

        try {
            $response = Http::withHeaders($this->adminHeaders())
                // Generous: the upload itself is quick, but a 50 MB scanned
                // bylaw over a domestic connection is not.
                ->timeout($this->timeout)
                ->attach('file', $file->get(), $file->getClientOriginalName())
                ->post($this->baseUrl.'/api/v1/admin/documents/upload', $payload);
        } catch (ConnectionException $exception) {
            Log::error('signlaw.upload.unreachable', ['error' => $exception->getMessage()]);

            throw new RuntimeException('The backend is unreachable. Try again shortly.');
        }

        if ($response->status() === 401 || $response->status() === 403) {
            throw new RuntimeException(
                'The backend rejected the admin key. Check SIGNLAW_ADMIN_KEY.',
            );
        }

        // 400 and 413 carry a message written for the operator — the file is
        // not a PDF, the municipality is unknown, the file is too large.
        if (in_array($response->status(), [400, 413], strict: true)) {
            throw new RuntimeException(
                $response->json('detail') ?? 'The backend rejected the upload.',
            );
        }

        // 422 names the field that failed. Surfacing it matters: "the upload
        // could not be processed" gives an operator nothing to act on, and the
        // backend already said exactly what was wrong.
        if ($response->status() === 422) {
            $fields = collect($response->json('errors') ?? [])
                ->map(static fn (array $error): string => trim(
                    ($error['field'] ?? '').' '.($error['message'] ?? '')
                ))
                ->filter()
                ->implode('; ');

            Log::error('signlaw.upload.rejected', ['body' => $response->body()]);

            throw new RuntimeException(
                $fields !== ''
                    ? "The backend rejected the upload: {$fields}"
                    : ($response->json('detail') ?? 'The upload failed validation.'),
            );
        }

        if ($response->failed()) {
            Log::error('signlaw.upload.failed', [
                'status' => $response->status(),
                'body' => $response->body(),
            ]);

            throw new RuntimeException(
                "The upload could not be processed (HTTP {$response->status()}).",
            );
        }

        return $response->json();
    }

    /**
     * A municipality's zoning provider configuration.
     *
     * @return array<string, mixed>
     */
    public function zoningConfig(string $slug): array
    {
        $response = Http::withHeaders($this->adminHeaders())
            ->timeout($this->timeout)
            ->acceptJson()
            ->get($this->baseUrl."/api/v1/admin/municipalities/{$slug}/zoning");

        if ($response->status() === 404) {
            throw new RuntimeException(
                'No municipality record yet. Ingest a bylaw for this city first.',
            );
        }

        if ($response->failed()) {
            throw new RuntimeException('Could not load the zoning configuration.');
        }

        return $response->json();
    }

    /**
     * @param  array<string, mixed>  $config
     * @return array<string, mixed>
     */
    public function saveZoningConfig(string $slug, array $payload): array
    {
        $response = Http::withHeaders($this->adminHeaders())
            ->timeout($this->timeout)
            ->acceptJson()
            ->put($this->baseUrl."/api/v1/admin/municipalities/{$slug}/zoning", $payload);

        if ($response->status() === 400) {
            throw new RuntimeException(
                $response->json('detail') ?? 'The configuration was rejected.',
            );
        }

        if ($response->failed()) {
            Log::error('signlaw.zoning_config.failed', ['status' => $response->status()]);

            throw new RuntimeException('Could not save the zoning configuration.');
        }

        return $response->json();
    }

    /**
     * Resolve an address to its zoning district.
     *
     * @return array<string, mixed>
     */
    public function zoningLookup(string $address, ?string $municipality = null): array
    {
        $payload = ['address' => $address];
        if ($municipality !== null && $municipality !== '') {
            $payload['municipality'] = $municipality;
        }

        return $this->post('/api/v1/zoning/lookup', $payload);
    }

    /**
     * Check a proposed sign against the bylaw.
     *
     * @param  array<string, mixed>  $spec
     * @return array<string, mixed>
     */
    public function complianceCheck(array $spec): array
    {
        return $this->post('/api/v1/compliance/check', $spec);
    }

    /**
     * @param  array<string, mixed>  $payload
     * @return array<string, mixed>
     */
    private function post(string $path, array $payload): array
    {
        try {
            $response = Http::withHeaders($this->headers())
                ->timeout($this->timeout)
                ->acceptJson()
                ->post($this->baseUrl.$path, $payload);
        } catch (ConnectionException $exception) {
            Log::error('signlaw.post.unreachable', [
                'path' => $path,
                'error' => $exception->getMessage(),
            ]);

            throw new RuntimeException('The service is unreachable. Try again shortly.');
        }

        if (in_array($response->status(), [400, 404], strict: true)) {
            throw new RuntimeException(
                $response->json('detail') ?? 'That request could not be processed.',
            );
        }

        if ($response->failed()) {
            Log::error('signlaw.post.failed', [
                'path' => $path,
                'status' => $response->status(),
            ]);

            throw new RuntimeException('That request could not be processed.');
        }

        return $response->json();
    }

    /**
     * @return array<string, string>
     */
    private function adminHeaders(): array
    {
        return $this->adminKey === null || $this->adminKey === ''
            ? []
            : ['X-Admin-Key' => $this->adminKey];
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
