@extends('layouts.app')

@section('title', 'Documents')

@section('content')
@php
    $stateStyles = [
        'completed' => 'bg-emerald-50 text-emerald-800 ring-emerald-200',
        'processing' => 'bg-blue-50 text-blue-800 ring-blue-200',
        'uploaded' => 'bg-slate-100 text-slate-700 ring-slate-200',
        'failed' => 'bg-red-50 text-red-800 ring-red-200',
    ];

    $rows = $documents['documents'] ?? [];
    $pending = $documents['pending'] ?? [];
    $working = collect($pending)->contains(fn ($item) => in_array($item['state'], ['uploaded', 'processing'], true));
@endphp

{{-- Only while something is indexing. A page that reloads forever is a page
     nobody leaves open. --}}
@if ($working)
    <meta http-equiv="refresh" content="10">
@endif

<div class="mx-auto max-w-5xl px-5 py-12">

    <div class="flex flex-wrap items-center justify-between gap-3">
        <div>
            <h1 class="text-2xl font-semibold tracking-tight">Documents</h1>
            <p class="mt-1 text-sm text-slate-600">
                {{ count($rows) }} indexed{{ $pending ? ', '.count($pending).' processing' : '' }}
            </p>
        </div>

        <div class="flex items-center gap-2">
            <a href="{{ route('admin.upload') }}"
               class="rounded-lg bg-slate-900 px-4 py-2 text-sm font-semibold text-white transition hover:bg-slate-700">
                Upload bylaw
            </a>
            <a href="{{ route('admin.zoning') }}"
               class="rounded-lg border border-slate-300 px-4 py-2 text-sm text-slate-700 transition hover:border-slate-400">
                Zoning providers
            </a>
            <form method="POST" action="{{ route('admin.logout') }}">
                @csrf
                <button type="submit" class="rounded-lg border border-slate-300 px-4 py-2 text-sm text-slate-700 transition hover:border-slate-400">
                    Sign out
                </button>
            </form>
        </div>
    </div>

    @if (session('status'))
        <p class="mt-6 rounded-lg bg-emerald-50 px-4 py-3 text-sm text-emerald-900 ring-1 ring-emerald-200">
            {{ session('status') }}
        </p>
    @endif

    @if ($error)
        <p class="mt-6 rounded-lg bg-red-50 px-4 py-3 text-sm text-red-900 ring-1 ring-red-200">{{ $error }}</p>
    @endif

    {{-- In flight ------------------------------------------------------- --}}
    @if ($pending)
        <section class="mt-8">
            <h2 class="text-sm font-semibold uppercase tracking-wide text-slate-500">Processing</h2>
            <div class="mt-3 space-y-2">
                @foreach ($pending as $item)
                    <div class="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-slate-200 px-4 py-3">
                        <div>
                            <p class="text-sm font-medium">{{ $item['filename'] }}</p>
                            @if ($item['error'] ?? null)
                                <p class="mt-1 text-xs text-red-700">{{ $item['error'] }}</p>
                            @endif
                        </div>
                        <span class="rounded-full px-2.5 py-1 text-xs font-semibold uppercase tracking-wide ring-1 {{ $stateStyles[$item['state']] ?? $stateStyles['uploaded'] }}">
                            {{ $item['state'] }}
                        </span>
                    </div>
                @endforeach
            </div>
            @if ($working)
                <p class="mt-2 text-xs text-slate-500">This page refreshes every 10 seconds while indexing runs.</p>
            @endif
        </section>
    @endif

    {{-- Indexed ---------------------------------------------------------- --}}
    <section class="mt-8">
        @if (! $rows)
            <div class="rounded-xl border border-dashed border-slate-300 p-10 text-center">
                <p class="text-sm font-medium">No documents indexed yet</p>
                <p class="mt-1 text-sm text-slate-600">
                    Upload a bylaw PDF to make it answerable.
                </p>
                <a href="{{ route('admin.upload') }}" class="mt-4 inline-block text-sm font-medium text-indigo-600 underline underline-offset-2">
                    Upload the first one
                </a>
            </div>
        @else
            <div class="overflow-x-auto rounded-xl border border-slate-200">
                <table class="w-full text-left text-sm">
                    <thead class="border-b border-slate-200 bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
                        <tr>
                            <th class="px-4 py-3 font-semibold">Municipality</th>
                            <th class="px-4 py-3 font-semibold">Document</th>
                            <th class="px-4 py-3 text-right font-semibold">Pages</th>
                            <th class="px-4 py-3 text-right font-semibold">Chunks</th>
                            <th class="px-4 py-3 font-semibold">Status</th>
                            <th class="px-4 py-3 font-semibold">Uploaded</th>
                        </tr>
                    </thead>
                    <tbody class="divide-y divide-slate-100">
                        @foreach ($rows as $row)
                            <tr>
                                <td class="px-4 py-3">
                                    {{ $row['municipality'] ?? '—' }}
                                    @unless ($row['municipality'])
                                        {{-- Undetected municipality means the
                                             document cannot be filtered by city
                                             and its citations lose precision. --}}
                                        <span class="ml-1 text-xs text-orange-700">not detected</span>
                                    @endunless
                                </td>
                                <td class="px-4 py-3">
                                    <span class="font-medium">{{ $row['title'] ?? $row['filename'] }}</span>
                                    @if ($row['bylaw_number'] ?? null)
                                        <span class="text-slate-500">No.&nbsp;{{ $row['bylaw_number'] }}</span>
                                    @endif
                                    @if ($row['year'] ?? null)
                                        <span class="text-slate-400">· {{ $row['year'] }}</span>
                                    @endif
                                    <span class="block font-mono text-xs text-slate-400">{{ $row['filename'] }}</span>
                                    @if (($row['ocr_applied'] ?? false) || (($row['text_quality'] ?? 1) < 0.95))
                                        {{-- OCR errors are plausible-looking, so
                                             this is worth surfacing rather than
                                             burying in the trace. --}}
                                        <span class="mt-0.5 block text-xs text-orange-700">
                                            OCR used · text quality {{ number_format(($row['text_quality'] ?? 0) * 100) }}%
                                        </span>
                                    @endif
                                </td>
                                <td class="px-4 py-3 text-right tabular-nums">{{ $row['pages'] }}</td>
                                <td class="px-4 py-3 text-right tabular-nums">{{ $row['chunks'] }}</td>
                                <td class="px-4 py-3">
                                    <span class="rounded-full px-2.5 py-1 text-xs font-semibold uppercase tracking-wide ring-1 {{ $stateStyles[$row['state']] ?? $stateStyles['uploaded'] }}">
                                        {{ $row['state'] }}
                                    </span>
                                    @if ($row['failed_stage'] ?? null)
                                        <span class="mt-1 block text-xs text-red-700">failed at {{ str_replace('_', ' ', $row['failed_stage']) }}</span>
                                    @endif
                                    @if (($row['status'] ?? '') !== 'in_force')
                                        <span class="mt-1 block text-xs text-slate-500">{{ str_replace('_', ' ', $row['status'] ?? '') }}</span>
                                    @endif
                                </td>
                                <td class="px-4 py-3 text-slate-600">
                                    {{ \Illuminate\Support\Carbon::parse($row['uploaded_at'])->format('j M Y') }}
                                </td>
                            </tr>
                        @endforeach
                    </tbody>
                </table>
            </div>

            <p class="mt-3 text-xs text-slate-500">
                A document showing zero chunks completed extraction but produced nothing
                retrievable — usually a scanned PDF whose pages had no text layer and no
                OCR available. It will never appear in an answer.
            </p>
        @endif
    </section>
</div>
@endsection
