@php
    /**
     * The whole product, one page.
     *
     * Contains no province logic. Everything below iterates whatever
     * /api/v1/municipalities returned, so adding Alberta is an ingestion job,
     * not a template change.
     */
    $provinces = $coverage['provinces'] ?? [];

    // The dependent dropdown needs the full catalogue client-side. Only
    // available municipalities are selectable; the rest are listed as coming
    // soon further down the page.
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

    $bandStyles = [
        'high' => 'bg-emerald-50 text-emerald-800 ring-emerald-200',
        'medium' => 'bg-amber-50 text-amber-800 ring-amber-200',
        'low' => 'bg-orange-50 text-orange-800 ring-orange-200',
        'insufficient' => 'bg-slate-100 text-slate-700 ring-slate-200',
    ];
@endphp
<!DOCTYPE html>
<html lang="en" class="h-full">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>SignLaw AI — AI assistant for Canadian sign bylaws</title>
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="h-full bg-slate-50 text-slate-900 antialiased">

<div class="mx-auto max-w-3xl px-5 py-10 sm:py-16">

    {{-- 1. Header ---------------------------------------------------------- --}}
    <header class="text-center">
        <h1 class="text-4xl font-semibold tracking-tight sm:text-5xl">SignLaw&nbsp;AI</h1>
        <p class="mt-3 text-lg text-slate-600">AI assistant for Canadian sign bylaws</p>
    </header>

    {{-- 2. Ask ------------------------------------------------------------- --}}
    <section class="mt-10 rounded-2xl bg-white p-6 shadow-sm ring-1 ring-slate-200 sm:p-8">
        <form method="POST" action="{{ route('ask.submit') }}" id="ask-form">
            @csrf

            <div class="grid gap-5 sm:grid-cols-2">
                <div>
                    <label for="province" class="block text-sm font-medium text-slate-700">Province</label>
                    <select id="province"
                            class="mt-1.5 w-full rounded-lg border-slate-300 bg-white px-3 py-2.5 text-sm shadow-sm focus:border-slate-900 focus:ring-slate-900">
                        @foreach ($provinces as $province)
                            @continue(! ($province['available'] ?? false))
                            <option value="{{ $province['code'] }}">{{ $province['name'] }}</option>
                        @endforeach
                    </select>
                </div>

                <div>
                    <label for="municipality" class="block text-sm font-medium text-slate-700">Municipality</label>
                    <select id="municipality" name="municipality"
                            class="mt-1.5 w-full rounded-lg border-slate-300 bg-white px-3 py-2.5 text-sm shadow-sm focus:border-slate-900 focus:ring-slate-900">
                        {{-- Populated from the catalogue below. --}}
                    </select>
                </div>
            </div>

            <div class="mt-5">
                <label for="question" class="block text-sm font-medium text-slate-700">Question</label>
                <textarea id="question" name="question" rows="3" required
                          placeholder="Does this building allow channel letters?"
                          class="mt-1.5 w-full rounded-lg border-slate-300 px-3 py-2.5 text-sm shadow-sm focus:border-slate-900 focus:ring-slate-900">{{ old('question', $question ?? '') }}</textarea>
            </div>

            @error('question')
                <p class="mt-2 text-sm text-red-600">{{ $message }}</p>
            @enderror
            @error('municipality')
                <p class="mt-2 text-sm text-red-600">{{ $message }}</p>
            @enderror

            <button type="submit" id="ask-button"
                    class="mt-5 w-full rounded-lg bg-slate-900 px-5 py-3 text-sm font-semibold text-white transition hover:bg-slate-700 disabled:cursor-not-allowed disabled:opacity-60">
                ASK AI
            </button>

            <p class="mt-4 text-center text-xs text-slate-500">
                Answers come only from indexed bylaw text. Informational only — not legal advice.
                Verify with the municipality before applying for a permit or fabricating signage.
            </p>
        </form>
    </section>

    {{-- 3. Answer ---------------------------------------------------------- --}}
    @if ($error)
        <section class="mt-6 rounded-2xl bg-red-50 p-6 ring-1 ring-red-200">
            <h2 class="text-sm font-semibold uppercase tracking-wide text-red-800">Unavailable</h2>
            <p class="mt-2 text-sm text-red-900">{{ $error }}</p>
        </section>
    @endif

    @if ($result)
        @php
            $answered = $result['answered'] ?? false;
            $outcome = $result['outcome'] ?? 'unknown';
            $confidence = $result['confidence'] ?? null;
            $band = $confidence['band'] ?? 'insufficient';
        @endphp

        <section class="mt-6 rounded-2xl bg-white p-6 shadow-sm ring-1 ring-slate-200 sm:p-8">
            <div class="flex flex-wrap items-center justify-between gap-3">
                <h2 class="text-sm font-semibold uppercase tracking-wide text-slate-500">
                    {{ $answered ? 'Answer' : 'No answer given' }}
                </h2>

                @if ($confidence)
                    <span class="rounded-full px-3 py-1 text-xs font-semibold uppercase tracking-wide ring-1 {{ $bandStyles[$band] ?? $bandStyles['insufficient'] }}">
                        {{ $band }} confidence · {{ number_format(($confidence['score'] ?? 0) * 100) }}%
                    </span>
                @endif
            </div>

            <p class="mt-4 whitespace-pre-line text-[15px] leading-relaxed text-slate-800">{{ $result['answer'] ?? '' }}</p>

            @if ($confidence && ($confidence['explanation'] ?? null))
                <p class="mt-3 text-xs text-slate-500">{{ $confidence['explanation'] }}</p>
            @endif

            {{-- Warnings are the reason a confidence score is trustworthy at
                 all: they say what the score is discounting for. --}}
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
                    <p class="mt-2 text-xs text-amber-800">Pick one from the Municipality list above and ask again.</p>
                </div>
            @endif

            {{-- Found, but superseded. Different from "nothing found", and
                 actionable in a way that "nothing found" is not. --}}
            @if ($result['outdated_documents'] ?? [])
                <div class="mt-5 rounded-lg bg-orange-50 p-4 ring-1 ring-orange-200">
                    <p class="text-sm font-medium text-orange-900">Found only in documents that are no longer in force:</p>
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
                        <article class="rounded-lg bg-slate-50 p-4 ring-1 ring-slate-200">
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
                                    {{-- OCR errors are plausible-looking, so the
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
                    trace {{ $result['trace_id'] }} · {{ $result['took_ms'] ?? 0 }}&nbsp;ms · outcome {{ $outcome }}
                </p>
            @endif
        </section>
    @endif

    {{-- 4. Coverage -------------------------------------------------------- --}}
    <section class="mt-12">
        <h2 class="text-lg font-semibold">Supported coverage</h2>

        @if (! $provinces)
            <p class="mt-3 text-sm text-slate-500">
                Coverage is unavailable because the answering service could not be reached.
            </p>
        @endif

        <div class="mt-4 grid gap-5 sm:grid-cols-2">
            @foreach ($provinces as $province)
                @php
                    $available = array_filter($province['municipalities'] ?? [], fn ($m) => $m['available'] ?? false);
                    $soon = array_filter($province['municipalities'] ?? [], fn ($m) => ! ($m['available'] ?? false));
                @endphp

                @if ($available)
                    <div class="rounded-2xl bg-white p-5 ring-1 ring-slate-200">
                        <h3 class="text-xs font-semibold uppercase tracking-wide text-slate-500">Currently available</h3>
                        <p class="mt-1 font-medium">🇨🇦 {{ $province['name'] }}</p>
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
                    <div class="rounded-2xl bg-white p-5 ring-1 ring-slate-200">
                        <h3 class="text-xs font-semibold uppercase tracking-wide text-slate-500">Coming soon</h3>
                        <p class="mt-1 font-medium">🇨🇦 {{ $province['name'] }}</p>
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
            A municipality is listed as available only when in-force bylaw documents are
            actually indexed for it. Nothing here is a promise the corpus cannot keep.
        </p>
    </section>

    {{-- 5. How it works ---------------------------------------------------- --}}
    <section class="mt-12">
        <h2 class="text-lg font-semibold">How it works</h2>
        <ol class="mt-4 space-y-3">
            <li class="flex items-start gap-3 rounded-xl bg-white p-4 ring-1 ring-slate-200">
                <span class="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-slate-900 text-xs font-semibold text-white">1</span>
                <div>
                    <p class="text-sm font-medium">Municipal PDF</p>
                    <p class="text-sm text-slate-600">The published bylaw is indexed page by page, section by section, with scanned pages recovered by OCR.</p>
                </div>
            </li>
            <li class="flex items-start gap-3 rounded-xl bg-white p-4 ring-1 ring-slate-200">
                <span class="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-slate-900 text-xs font-semibold text-white">2</span>
                <div>
                    <p class="text-sm font-medium">AI processing</p>
                    <p class="text-sm text-slate-600">Your question retrieves the relevant sections, which are re-ranked and read by a local language model. Nothing is sent to a third party.</p>
                </div>
            </li>
            <li class="flex items-start gap-3 rounded-xl bg-white p-4 ring-1 ring-slate-200">
                <span class="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-slate-900 text-xs font-semibold text-white">3</span>
                <div>
                    <p class="text-sm font-medium">Answer with citations</p>
                    <p class="text-sm text-slate-600">Every statement carries its bylaw, section and page, and each quote is checked to appear verbatim in the source before you see it. When the bylaw does not say, the answer says so.</p>
                </div>
            </li>
        </ol>
    </section>

    <footer class="mt-14 border-t border-slate-200 pt-6 text-center text-xs text-slate-500">
        SignLaw AI · Answers are generated from indexed bylaw text and may be incomplete.
        Always confirm with the municipality before relying on them.
    </footer>
</div>

<script>
    // The only JavaScript on the page: filter municipalities by the selected
    // province, and stop double submits on a slow answer. Everything else is a
    // plain form post.
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
            // Restore the province that owns the previously selected
            // municipality, so a reload after asking does not silently move
            // the user to a different jurisdiction.
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

</body>
</html>
