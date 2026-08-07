@extends('layouts.app')

@section('title', 'SignLaw AI')

@section('content')
<div class="mx-auto max-w-5xl px-5">

    {{-- Hero ------------------------------------------------------------- --}}
    <section class="py-20 text-center sm:py-28">
        <h1 class="mx-auto max-w-2xl text-4xl font-semibold tracking-tight sm:text-5xl">
            Sign bylaw answers you can
            <span class="text-indigo-600">check</span>
        </h1>
        <p class="mx-auto mt-5 max-w-xl text-lg leading-relaxed text-slate-600">
            Ask a question about a Canadian municipal sign bylaw. Every answer cites
            the bylaw, section and page it came from — and when the bylaw does not
            say, the answer says so.
        </p>

        <div class="mt-8 flex flex-wrap items-center justify-center gap-3">
            <a href="{{ route('ask') }}"
               class="rounded-lg bg-slate-900 px-6 py-3 text-sm font-semibold text-white transition hover:bg-slate-700">
                Ask a question
            </a>
            <a href="#how-it-works"
               class="rounded-lg border border-slate-300 px-6 py-3 text-sm font-semibold text-slate-700 transition hover:border-slate-400">
                How it works
            </a>
        </div>
    </section>

    {{-- What makes it different ------------------------------------------- --}}
    <section class="grid gap-5 border-t border-slate-200 py-16 sm:grid-cols-3">
        <div>
            <h2 class="text-sm font-semibold">Citations, not summaries</h2>
            <p class="mt-2 text-sm leading-relaxed text-slate-600">
                Each statement carries its municipality, bylaw number, section and page.
                Quotes are checked to appear verbatim in the source before you see them.
            </p>
        </div>
        <div>
            <h2 class="text-sm font-semibold">It declines</h2>
            <p class="mt-2 text-sm leading-relaxed text-slate-600">
                If the indexed bylaws do not address your question, it says so rather
                than reasoning by analogy from a different kind of sign.
            </p>
        </div>
        <div>
            <h2 class="text-sm font-semibold">Current text only</h2>
            <p class="mt-2 text-sm leading-relaxed text-slate-600">
                Repealed and superseded provisions are excluded. Where only outdated
                text exists, it tells you that instead of quoting it.
            </p>
        </div>
    </section>

    {{-- How it works ------------------------------------------------------ --}}
    <section id="how-it-works" class="border-t border-slate-200 py-16">
        <h2 class="text-xl font-semibold tracking-tight">How it works</h2>

        <ol class="mt-6 grid gap-4 sm:grid-cols-3">
            @foreach ([
                ['Municipal PDF', 'The published bylaw is indexed page by page and section by section, with scanned pages recovered by OCR.'],
                ['AI processing', 'Your question retrieves the relevant sections, which are re-ranked and read by a local language model. Nothing is sent to a third party.'],
                ['Answer with citations', 'Every statement carries its bylaw, section and page, alongside a confidence score explaining what the score is discounting for.'],
            ] as $index => [$heading, $body])
                <li class="rounded-xl border border-slate-200 p-5">
                    <span class="flex h-7 w-7 items-center justify-center rounded-full bg-slate-900 text-xs font-semibold text-white">
                        {{ $index + 1 }}
                    </span>
                    <p class="mt-3 text-sm font-medium">{{ $heading }}</p>
                    <p class="mt-1.5 text-sm leading-relaxed text-slate-600">{{ $body }}</p>
                </li>
            @endforeach
        </ol>
    </section>

    {{-- Coverage ---------------------------------------------------------- --}}
    <div class="border-t border-slate-200 py-16">
        @include('partials.coverage', ['coverage' => $coverage])
    </div>

    {{-- Closing CTA ------------------------------------------------------- --}}
    <section class="border-t border-slate-200 py-16 text-center">
        <h2 class="text-2xl font-semibold tracking-tight">Ask your first question</h2>
        <p class="mx-auto mt-3 max-w-lg text-sm leading-relaxed text-slate-600">
            No account needed. Pick a municipality, ask, and read the sections the
            answer rests on.
        </p>
        <a href="{{ route('ask') }}"
           class="mt-6 inline-block rounded-lg bg-slate-900 px-6 py-3 text-sm font-semibold text-white transition hover:bg-slate-700">
            Ask a question
        </a>
    </section>
</div>
@endsection
