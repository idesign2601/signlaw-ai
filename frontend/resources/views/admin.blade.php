@extends('layouts.app')

@section('title', 'Admin')

@section('content')
<div class="mx-auto max-w-3xl px-5 py-16">

    <span class="inline-block rounded-full bg-slate-100 px-3 py-1 text-xs font-semibold uppercase tracking-wide text-slate-600">
        Not built yet
    </span>

    <h1 class="mt-4 text-2xl font-semibold tracking-tight">Document management</h1>

    <p class="mt-4 text-[15px] leading-relaxed text-slate-600">
        Uploading bylaw PDFs, watching ingestion progress, marking a bylaw superseded
        when a municipality amends it, and re-indexing after a model change. Today all
        of this runs from the command line on the server.
    </p>

    {{--
        This page renders nothing and calls nothing. That is the point: it must
        stay inert until it is guarded, because the operations it will perform
        — delete a document, trigger a re-index — are destructive.
    --}}
    <div class="mt-8 rounded-xl border border-amber-200 bg-amber-50 p-6">
        <h2 class="text-sm font-semibold text-amber-900">Before this page does anything</h2>
        <p class="mt-2 text-sm leading-relaxed text-amber-900">
            It must be behind authentication. The operations it will expose delete
            documents and trigger re-indexing, and the backend already guards them with
            an <code class="rounded bg-amber-100 px-1">X-Admin-Key</code> header. This
            placeholder deliberately renders no data and calls no endpoint, so there is
            nothing here to leak while the auth is missing.
        </p>
    </div>

    <div class="mt-6 rounded-xl border border-slate-200 p-6">
        <h2 class="text-sm font-semibold">How to do it today</h2>
        <dl class="mt-3 space-y-3 text-sm">
            <div>
                <dt class="font-medium text-slate-700">Index a bylaw</dt>
                <dd class="mt-0.5 font-mono text-xs text-slate-600">signlaw ingest documents/bylaws/burnaby_sign_bylaw.pdf</dd>
            </div>
            <div>
                <dt class="font-medium text-slate-700">Check what is indexed</dt>
                <dd class="mt-0.5 font-mono text-xs text-slate-600">signlaw health</dd>
            </div>
            <div>
                <dt class="font-medium text-slate-700">Re-index everything</dt>
                <dd class="mt-0.5 font-mono text-xs text-slate-600">signlaw ingest documents/bylaws/ --force</dd>
            </div>
            <div>
                <dt class="font-medium text-slate-700">Back up the corpus</dt>
                <dd class="mt-0.5 font-mono text-xs text-slate-600">make backup</dd>
            </div>
        </dl>
    </div>
</div>
@endsection
