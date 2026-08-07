<?php

declare(strict_types=1);

namespace App\Http\Controllers;

use App\Services\SignLawClient;
use Illuminate\Http\RedirectResponse;
use Illuminate\Http\Request;
use Illuminate\Http\UploadedFile;
use Illuminate\Validation\Rule;
use Illuminate\View\View;
use RuntimeException;

/**
 * Admin area: sign in, upload bylaws, watch them index.
 *
 * Holds no state itself. Documents, statuses and municipalities all come from
 * the backend, so two operators on two machines see the same thing.
 */
final class AdminController extends Controller
{
    public function __construct(private readonly SignLawClient $client)
    {
    }

    // -- authentication ------------------------------------------------------

    public function showLogin(): View|RedirectResponse
    {
        if (session('admin_authenticated') === true) {
            return redirect()->route('admin.dashboard');
        }

        return view('admin.login');
    }

    public function login(Request $request): RedirectResponse
    {
        $request->validate(['password' => ['required', 'string']]);

        $expected = config('signlaw.admin_password');

        if (! is_string($expected) || $expected === '') {
            return back()->withErrors([
                'password' => 'Admin access is disabled: ADMIN_PASSWORD is not configured.',
            ]);
        }

        // Constant time, so a wrong password cannot be narrowed by timing.
        if (! hash_equals($expected, (string) $request->input('password'))) {
            return back()->withErrors(['password' => 'Incorrect password.']);
        }

        // Regenerated on privilege change, so a session id captured before
        // sign-in cannot be replayed afterwards.
        $request->session()->regenerate();
        $request->session()->put('admin_authenticated', true);

        return redirect()->route('admin.dashboard');
    }

    public function logout(Request $request): RedirectResponse
    {
        $request->session()->forget('admin_authenticated');
        $request->session()->regenerate();

        return redirect()->route('landing');
    }

    // -- dashboard -----------------------------------------------------------

    public function dashboard(): View
    {
        try {
            $documents = $this->client->documents();
            $error = null;
        } catch (RuntimeException $exception) {
            $documents = ['documents' => [], 'pending' => [], 'total' => 0];
            $error = $exception->getMessage();
        }

        return view('admin.dashboard', [
            'documents' => $documents,
            'error' => $error,
        ]);
    }

    // -- upload --------------------------------------------------------------

    public function showUpload(): View
    {
        return view('admin.upload', ['coverage' => $this->client->coverage()]);
    }

    public function upload(Request $request): RedirectResponse
    {
        $coverage = $this->client->coverage();

        // Every catalogued municipality is uploadable, not only the available
        // ones: uploading is precisely how a municipality becomes available.
        $validated = $request->validate([
            'province' => ['required', 'string', Rule::in($this->provinceCodes($coverage))],
            'municipality' => ['required', 'string', Rule::in($this->allSlugs($coverage))],
            'title' => ['required', 'string', 'min:3', 'max:500'],
            'year' => ['nullable', 'integer', 'min:1900', 'max:2100'],
            'document' => ['required', 'file', 'mimetypes:application/pdf', 'max:51200'],
        ], [
            'document.mimetypes' => 'The document must be a PDF.',
            'document.max' => 'The PDF must be under 50 MB.',
        ]);

        /** @var UploadedFile $file */
        $file = $validated['document'];

        // Cast rather than trust: validation checks the shape of a value, it
        // does not convert it. An 'integer' rule still hands back the string
        // that arrived in the request body.
        $year = $validated['year'] ?? null;
        $year = ($year === null || $year === '') ? null : (int) $year;

        try {
            $result = $this->client->uploadDocument(
                province: $validated['province'],
                municipality: $validated['municipality'],
                title: $validated['title'],
                year: $year,
                file: $file,
            );
        } catch (RuntimeException $exception) {
            return back()->withInput()->withErrors(['document' => $exception->getMessage()]);
        }

        return redirect()
            ->route('admin.dashboard')
            ->with('status', $result['message'] ?? 'Upload accepted.');
    }

    // -- zoning providers ----------------------------------------------------

    public function zoning(Request $request): View
    {
        $coverage = $this->client->coverage();
        $selected = $request->query('municipality');

        if (! is_string($selected) || ! in_array($selected, $this->allSlugs($coverage), true)) {
            $selected = $this->allSlugs($coverage)[0] ?? null;
        }

        try {
            $config = $selected ? $this->client->zoningConfig($selected) : null;
            $error = null;
        } catch (RuntimeException $exception) {
            $config = null;
            $error = $exception->getMessage();
        }

        return view('admin.zoning', [
            'coverage' => $coverage,
            'selected' => $selected,
            'config' => $config,
            'error' => $error,
        ])->withErrors($error ? ['config' => $error] : []);
    }

    public function saveZoning(Request $request): RedirectResponse
    {
        $coverage = $this->client->coverage();

        $validated = $request->validate([
            'municipality' => ['required', 'string', Rule::in($this->allSlugs($coverage))],
            'kind' => ['nullable', 'string', 'max:40'],
            'endpoint' => ['nullable', 'url', 'max:1000'],
            'map_url' => ['nullable', 'url', 'max:1000'],
            'config' => ['nullable', 'string'],
        ]);

        // Validated here rather than server-side only: a malformed mapping
        // would otherwise be stored and fail later as an unexplained absence
        // of zoning results.
        $config = json_decode($validated['config'] ?? '{}', true);
        if (! is_array($config)) {
            return back()->withInput()->withErrors([
                'config' => 'The field mapping is not valid JSON.',
            ]);
        }

        try {
            $this->client->saveZoningConfig($validated['municipality'], [
                'kind' => $validated['kind'] ?: null,
                'endpoint' => $validated['endpoint'] ?: null,
                'map_url' => $validated['map_url'] ?: null,
                'config' => $config,
                'verified' => $request->boolean('verified'),
            ]);
        } catch (RuntimeException $exception) {
            return back()->withInput()->withErrors(['config' => $exception->getMessage()]);
        }

        return redirect()
            ->route('admin.zoning', ['municipality' => $validated['municipality']])
            ->with('status', 'Zoning configuration saved.');
    }

    /**
     * @param  array{provinces: array<int, array<string, mixed>>}  $coverage
     * @return array<int, string>
     */
    private function provinceCodes(array $coverage): array
    {
        return array_map(
            static fn (array $province): string => $province['code'],
            $coverage['provinces'] ?? [],
        );
    }

    /**
     * @param  array{provinces: array<int, array<string, mixed>>}  $coverage
     * @return array<int, string>
     */
    private function allSlugs(array $coverage): array
    {
        $slugs = [];

        foreach ($coverage['provinces'] ?? [] as $province) {
            foreach ($province['municipalities'] ?? [] as $municipality) {
                $slugs[] = $municipality['slug'];
            }
        }

        return $slugs;
    }
}
