<?php

declare(strict_types=1);

namespace App\Http\Controllers;

use App\Services\SignLawClient;
use Illuminate\Http\Request;
use Illuminate\Validation\Rule;
use Illuminate\View\View;
use RuntimeException;

/**
 * The question interface.
 *
 * A form post and a server-rendered response. No JSON endpoints, no SPA — the
 * answer arrives with the page rather than being fetched into it.
 */
final class AskController extends Controller
{
    public function __construct(private readonly SignLawClient $client)
    {
    }

    public function index(): View
    {
        return view('ask', [
            'coverage' => $this->client->coverage(),
            'result' => null,
            'error' => null,
        ]);
    }

    public function ask(Request $request): View
    {
        $coverage = $this->client->coverage();

        $validated = $request->validate([
            'question' => ['required', 'string', 'min:3', 'max:2000'],
            // Validated against what the backend reports as available, not a
            // list maintained here. A municipality that stops being available
            // — because its only bylaw was marked repealed — stops being
            // selectable without any change to this file.
            'municipality' => [
                'nullable',
                'string',
                Rule::in($this->availableSlugs($coverage)),
            ],
        ], [
            'municipality.in' => 'No bylaws are indexed for that municipality yet.',
        ]);

        try {
            $result = $this->client->ask(
                $validated['question'],
                $validated['municipality'] ?? null,
            );
            $error = null;
        } catch (RuntimeException $exception) {
            $result = null;
            $error = $exception->getMessage();
        }

        return view('ask', [
            'coverage' => $coverage,
            'result' => $result,
            'error' => $error,
            'question' => $validated['question'],
            'selectedMunicipality' => $validated['municipality'] ?? null,
        ]);
    }

    /**
     * @param  array{provinces: array<int, array<string, mixed>>}  $coverage
     * @return array<int, string>
     */
    private function availableSlugs(array $coverage): array
    {
        $slugs = [];

        foreach ($coverage['provinces'] ?? [] as $province) {
            foreach ($province['municipalities'] ?? [] as $municipality) {
                if ($municipality['available'] ?? false) {
                    $slugs[] = $municipality['slug'];
                }
            }
        }

        return $slugs;
    }
}
