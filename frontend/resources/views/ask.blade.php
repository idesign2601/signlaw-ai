@extends('layouts.app')

@section('title', 'Ask')

@section('content')
@php
    $provinces = $coverage['provinces'] ?? [];

    // The dependent dropdown needs the catalogue client-side. Only available
    // municipalities are selectable; the rest appear under coverage.
    $selectable = [];
    foreach ($provinces as $province) {
        foreach ($province['municipalities'] ?? [] as $municipality) {
            if ($municipality['available'] ?? false) {
                $selectable[$province['code']][] = [
                    'slug' => $municipality['slug'],
                    'label' => $municipality['official_name'],
                ];
            }
        }
    }

    // A municipality is selectable only once a bylaw is indexed for it. Before
    // the first ingest that is every municipality, which would render two empty
    // dropdowns and no explanation — so the empty case gets its own panel.
    $hasCoverage = $selectable !== [];

    $bandStyles = [
        'high' => 'bg-emerald-50 text-emerald-800 ring-emerald-200',
        'medium' => 'bg-amber-50 text-amber-800 ring-amber-200',
        'low' => 'bg-orange-50 text-orange-800 ring-orange-200',
        'insufficient' => 'bg-slate-100 text-slate-700 ring-slate-200',
    ];
@endphp

<div class="mx-auto max-w-3xl px-5 py-12">

    <h1 class="text-2xl font-semibold tracking-tight">Ask about a sign bylaw</h1>
    <p class="mt-2 text-sm text-slate-600">
        Answers come only from indexed bylaw text, with the section and page they rest on.
    </p>

    {{-- Nothing indexed yet ---------------------------------------------- --}}
    @if (! $hasCoverage)
        <section class="mt-8 rounded-xl border border-dashed border-slate-300 p-8 text-center">
            <p class="text-sm font-medium">No bylaws are indexed yet</p>
            <p class="mx-auto mt-2 max-w-md text-sm leading-relaxed text-slate-600">
                @if ($provinces)
                    Municipalities are catalogued, but none has an in-force bylaw indexed,
                    so there is nothing to answer from. Upload a bylaw PDF to make a
                    municipality answerable.
                @else
                    The answering service could not be reached, so coverage is unknown.
                    Questions cannot be asked until it is available.
                @endif
            </p>
            <a href="{{ route('admin.dashboard') }}"
               class="mt-5 inline-block rounded-lg bg-slate-900 px-5 py-2.5 text-sm font-semibold text-white transition hover:bg-slate-700">
                Go to admin
            </a>
        </section>
    @endif

    {{-- Question form ----------------------------------------------------- --}}
    <section @class([
        'mt-8 rounded-xl border border-slate-200 p-6',
        'hidden' => ! $hasCoverage,
    ])>
        <form method="POST" action="{{ route('ask.submit') }}" id="ask-form">
            @csrf

            <div class="grid gap-4 sm:grid-cols-2">
                <div>
                    <label for="province" class="block text-sm font-medium text-slate-700">Province</label>
                    <select id="province"
                            class="mt-1.5 w-full rounded-lg border-slate-300 bg-white px-3 py-2.5 text-sm focus:border-indigo-500 focus:ring-indigo-500">
                        @foreach ($provinces as $province)
                            @continue(! ($province['available'] ?? false))
                            <option value="{{ $province['code'] }}">{{ $province['name'] }}</option>
                        @endforeach
                    </select>
                </div>

                <div>
                    <label for="municipality" class="block text-sm font-medium text-slate-700">Municipality</label>
                    <select id="municipality" name="municipality"
                            class="mt-1.5 w-full rounded-lg border-slate-300 bg-white px-3 py-2.5 text-sm focus:border-indigo-500 focus:ring-indigo-500">
                    </select>
                </div>
            </div>

            <div class="mt-4">
                <label for="question" class="block text-sm font-medium text-slate-700">Question</label>
                <textarea id="question" name="question" rows="3" required
                          placeholder="Does this building allow channel letters?"
                          class="mt-1.5 w-full rounded-lg border-slate-300 px-3 py-2.5 text-sm focus:border-indigo-500 focus:ring-indigo-500">{{ old('question', $question ?? '') }}</textarea>
            </div>

            @error('question')
                <p class="mt-2 text-sm text-red-600">{{ $message }}</p>
            @enderror
            @error('municipality')
                <p class="mt-2 text-sm text-red-600">{{ $message }}</p>
            @enderror

            <button type="submit" id="ask-button"
                    class="mt-4 w-full rounded-lg bg-slate-900 px-5 py-3 text-sm font-semibold text-white transition hover:bg-slate-700 disabled:cursor-not-allowed disabled:opacity-60">
                ASK AI
            </button>
        </form>
    </section>

    {{-- Failure ----------------------------------------------------------- --}}
    @if ($error)
        <section class="mt-6 rounded-xl border border-red-200 bg-red-50 p-6">
            <h2 class="text-sm font-semibold uppercase tracking-wide text-red-800">Unavailable</h2>
            <p class="mt-2 text-sm text-red-900">{{ $error }}</p>
        </section>
    @endif

    {{-- Answer ------------------------------------------------------------ --}}
    @if ($result)
        @php
            $answered = $result['answered'] ?? false;
            $confidence = $result['confidence'] ?? null;
            $band = $confidence['band'] ?? 'insufficient';
        @endphp

        <section class="mt-6 rounded-xl border border-slate-200 p-6">
            <div class="flex flex-wrap items-center justify-between gap-3">
                <h2 class="text-sm font-semibold uppercase tracking-wide text-slate-500">
                    {{ $answered ? 'Answer' : 'No answer given' }}
                </h2>

                @if ($confidence)
                    <span class="rounded-full px-3 py-1 text-xs font-semibold uppercase tracking-wide ring-1 {{ $bandStyles[$band] ?? $bandStyles['insufficient'] }}">
                        {{ $band }} · {{ number_format(($confidence['score'] ?? 0) * 100) }}%
                    </span>
                @endif
            </div>

            <p class="mt-4 whitespace-pre-line text-[15px] leading-relaxed text-slate-800">{{ $result['answer'] ?? '' }}</p>

            @if ($confidence && ($confidence['explanation'] ?? null))
                <p class="mt-3 text-xs text-slate-500">{{ $confidence['explanation'] }}</p>
            @endif

            {{-- Warnings are what make a confidence score worth trusting:
                 they say what it is discounting for. --}}
            @foreach ($confidence['warnings'] ?? [] as $warning)
                <p class="mt-2 rounded-lg bg-amber-50 px-3 py-2 text-xs text-amber-900 ring-1 ring-amber-200">{{ $warning }}</p>
            @endforeach

            {{-- Ambiguous municipality: ask, never guess. --}}
            @if ($result['clarification_options'] ?? [])
                <div class="mt-5 rounded-lg bg-amber-50 p-4 ring-1 ring-amber-200">
                    <p class="text-sm font-medium text-amber-900">These are separate jurisdictions with separate bylaws:</p>
                    <ul class="mt-2 list-inside list-disc text-sm text-amber-900">
                        @foreach ($result['clarification_options'] as $option)
                            <li>{{ $option }}</li>
                        @endforeach
                    </ul>
                    <p class="mt-2 text-xs text-amber-800">Pick one above and ask again.</p>
                </div>
            @endif

            {{-- Found, but superseded. Actionable in a way that "nothing
                 found" is not: it means a rule exists and the current version
                 is simply not indexed. --}}
            @if ($result['outdated_documents'] ?? [])
                <div class="mt-5 rounded-lg bg-orange-50 p-4 ring-1 ring-orange-200">
                    <p class="text-sm font-medium text-orange-900">Found only in documents no longer in force:</p>
                    <ul class="mt-2 list-inside list-disc text-sm text-orange-900">
                        @foreach ($result['outdated_documents'] as $document)
                            <li>{{ $document }}</li>
                        @endforeach
                    </ul>
                </div>
            @endif

            @if ($result['conditions'] ?? [])
                <div class="mt-5">
                    <h3 class="text-sm font-semibold text-slate-700">Conditions</h3>
                    <ul class="mt-2 list-inside list-disc text-sm text-slate-700">
                        @foreach ($result['conditions'] as $condition)
                            <li>{{ $condition }}</li>
                        @endforeach
                    </ul>
                </div>
            @endif

            @if ($result['citations'] ?? [])
                <h3 class="mt-7 text-sm font-semibold uppercase tracking-wide text-slate-500">Sources</h3>
                <div class="mt-3 space-y-3">
                    @foreach ($result['citations'] as $citation)
                        <article class="rounded-lg bg-slate-50 p-4">
                            <div class="flex flex-wrap items-baseline gap-x-2 gap-y-1 text-sm font-medium text-slate-900">
                                <span>{{ $citation['municipality'] ?? 'Unattributed' }}</span>
                                <span class="text-slate-400">·</span>
                                <span>{{ $citation['bylaw_title'] ?? 'Untitled bylaw' }}</span>
                                @if ($citation['bylaw_number'] ?? null)
                                    <span class="text-slate-500">No.&nbsp;{{ $citation['bylaw_number'] }}</span>
                                @endif
                            </div>

                            <div class="mt-1 flex flex-wrap gap-x-3 text-xs text-slate-600">
                                @if ($citation['section'] ?? null)
                                    <span>Section&nbsp;{{ $citation['section'] }}</span>
                                @endif
                                <span>Page&nbsp;{{ $citation['page'] ?? '—' }}</span>
                                @if (($citation['amendment_status'] ?? '') !== 'in_force')
                                    <span class="font-semibold text-orange-700">{{ str_replace('_', ' ', $citation['amendment_status'] ?? 'unknown') }}</span>
                                @endif
                                @if ($citation['from_ocr'] ?? false)
                                    {{-- OCR errors are plausible-looking, so a
                                         reader should know to check this one. --}}
                                    <span class="text-slate-500">recovered by OCR — verify against the PDF</span>
                                @endif
                            </div>

                            <blockquote class="mt-2 border-l-2 border-slate-300 pl-3 text-sm italic text-slate-700">
                                {{ $citation['quote'] ?? '' }}
                            </blockquote>
                        </article>
                    @endforeach
                </div>
            @endif

            @if ($result['trace_id'] ?? null)
                <p class="mt-6 font-mono text-[11px] text-slate-400">
                    trace {{ $result['trace_id'] }} · {{ $result['took_ms'] ?? 0 }}&nbsp;ms · outcome {{ $result['outcome'] ?? '' }}
                </p>
            @endif
        </section>
    @endif

    <div class="mt-16 border-t border-slate-200 pt-10">
        @include('partials.coverage', ['coverage' => $coverage])
    </div>
</div>

<script>
    // The only JavaScript in the application: filter municipalities by the
    // selected province, and stop double submits while an answer is generating.
    (function () {
        const catalogue = @json($selectable);
        const province = document.getElementById('province');
        const municipality = document.getElementById('municipality');
        const selected = @json($selectedMunicipality ?? null);

        function repopulate() {
            const options = catalogue[province.value] || [];
            municipality.innerHTML = '';

            if (options.length === 0) {
                municipality.appendChild(new Option('No municipalities indexed yet', ''));
                municipality.disabled = true;
                return;
            }

            municipality.disabled = false;
            for (const option of options) {
                const element = new Option(option.label, option.slug);
                element.selected = option.slug === selected;
                municipality.appendChild(element);
            }
        }

        if (province) {
            // Restore the province owning the previously selected municipality,
            // so returning with an answer does not silently move the user to a
            // different jurisdiction.
            if (selected) {
                for (const [code, options] of Object.entries(catalogue)) {
                    if (options.some((option) => option.slug === selected)) {
                        province.value = code;
                        break;
                    }
                }
            }
            province.addEventListener('change', repopulate);
            repopulate();
        }

        const form = document.getElementById('ask-form');
        const button = document.getElementById('ask-button');
        form.addEventListener('submit', function () {
            button.disabled = true;
            button.textContent = 'READING THE BYLAW…';
        });
    })();
</script>
@endsection
