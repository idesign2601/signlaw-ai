{{--
    Supported coverage.

    Shared by the landing page and /ask. Contains no province logic: it renders
    whatever GET /api/v1/municipalities returned, so adding Alberta is an
    ingestion job rather than a template change.

    Availability is computed by the backend from indexed in-force documents, not
    declared in a list. A municipality listed here can actually be asked about.
--}}
@php
    $provinces = $coverage['provinces'] ?? [];
@endphp

<section>
    <h2 class="text-xl font-semibold tracking-tight">Supported coverage</h2>

    @if (! $provinces)
        <p class="mt-3 text-sm text-slate-500">
            Coverage is unavailable because the answering service could not be reached.
        </p>
    @endif

    <div class="mt-5 grid gap-4 sm:grid-cols-2">
        @foreach ($provinces as $province)
            @php
                $available = array_values(array_filter(
                    $province['municipalities'] ?? [],
                    fn ($m) => $m['available'] ?? false,
                ));
                $soon = array_values(array_filter(
                    $province['municipalities'] ?? [],
                    fn ($m) => ! ($m['available'] ?? false),
                ));
            @endphp

            @if ($available)
                <div class="rounded-xl border border-slate-200 p-5">
                    <p class="text-xs font-semibold uppercase tracking-wide text-emerald-700">Currently available</p>
                    <p class="mt-1.5 font-medium">🇨🇦 {{ $province['name'] }}</p>
                    <ul class="mt-3 space-y-1.5 text-sm text-slate-700">
                        @foreach ($available as $municipality)
                            <li class="flex items-baseline gap-2">
                                <span class="text-emerald-600">✓</span>
                                <span>{{ $municipality['official_name'] }}</span>
                                <span class="text-xs text-slate-400">
                                    {{ $municipality['document_count'] }}
                                    {{ \Illuminate\Support\Str::plural('bylaw', $municipality['document_count']) }}
                                </span>
                            </li>
                        @endforeach
                    </ul>
                </div>
            @endif

            @if ($soon && ! $available)
                <div class="rounded-xl border border-slate-200 p-5">
                    <p class="text-xs font-semibold uppercase tracking-wide text-slate-400">Coming soon</p>
                    <p class="mt-1.5 font-medium">🇨🇦 {{ $province['name'] }}</p>
                    <ul class="mt-3 space-y-1.5 text-sm text-slate-500">
                        @foreach (array_slice($soon, 0, 8) as $municipality)
                            <li class="flex items-baseline gap-2">
                                <span class="text-slate-300">○</span>
                                <span>{{ $municipality['official_name'] }}</span>
                            </li>
                        @endforeach
                    </ul>
                </div>
            @endif
        @endforeach
    </div>

    <p class="mt-4 text-xs text-slate-500">
        A municipality appears as available only once in-force bylaw documents are
        actually indexed for it. Nothing listed here is a promise the corpus cannot keep.
    </p>
</section>
