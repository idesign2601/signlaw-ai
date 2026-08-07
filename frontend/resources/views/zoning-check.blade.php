@extends('layouts.app')

@section('title', 'Zoning check')

@section('content')
<div class="mx-auto max-w-3xl px-5 py-16">

    <span class="inline-block rounded-full bg-slate-100 px-3 py-1 text-xs font-semibold uppercase tracking-wide text-slate-600">
        Not built yet
    </span>

    <h1 class="mt-4 text-2xl font-semibold tracking-tight">Zoning check</h1>

    <p class="mt-4 text-[15px] leading-relaxed text-slate-600">
        Sign permissions depend on the zone a property sits in — permitted sign types,
        maximum area and height often differ between commercial, industrial and
        mixed-use zones within the same municipality. This module will take an address
        and return the sign rules for that property's zone.
    </p>

    {{--
        Deliberately explicit about what is missing. A placeholder that lists
        features without saying they are absent reads as a product page, and
        someone will plan around it.
    --}}
    <div class="mt-8 rounded-xl border border-slate-200 p-6">
        <h2 class="text-sm font-semibold">What it needs first</h2>
        <ul class="mt-3 space-y-2 text-sm leading-relaxed text-slate-600">
            <li class="flex gap-2">
                <span class="text-slate-300">○</span>
                <span>Zoning bylaws ingested alongside the sign bylaws. Only sign bylaws are indexed today.</span>
            </li>
            <li class="flex gap-2">
                <span class="text-slate-300">○</span>
                <span>Address-to-parcel lookup, then parcel-to-zone. Municipalities publish these separately and in different formats.</span>
            </li>
            <li class="flex gap-2">
                <span class="text-slate-300">○</span>
                <span>A way to express which sign rules a zone modifies, since sign bylaws and zoning bylaws cross-reference each other.</span>
            </li>
        </ul>
    </div>

    <p class="mt-6 text-sm leading-relaxed text-slate-600">
        In the meantime, <a href="{{ route('ask') }}" class="font-medium text-indigo-600 underline underline-offset-2">ask a question</a>
        naming the zone directly — for example, "what is the maximum fascia sign area in
        a C-2 zone in Burnaby?" — and the answer will cite the relevant section if the
        sign bylaw addresses it.
    </p>
</div>
@endsection
