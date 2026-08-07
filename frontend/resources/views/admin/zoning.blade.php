@extends('layouts.app')

@section('title', 'Zoning providers')

@section('content')
@php
    $provinces = $coverage['provinces'] ?? [];
    $config = $config ?? null;
@endphp

<div class="mx-auto max-w-3xl px-5 py-12">

    <div class="flex items-center justify-between">
        <h1 class="text-2xl font-semibold tracking-tight">Zoning providers</h1>
        <a href="{{ route('admin.dashboard') }}" class="text-sm text-slate-600 hover:text-slate-900">← Dashboard</a>
    </div>

    <p class="mt-3 text-sm leading-relaxed text-slate-600">
        Adding a municipality is configuration, not code: pick the kind of service the
        city publishes, give its endpoint, and map its field names.
    </p>

    {{-- Pick a municipality ---------------------------------------------- --}}
    <form method="GET" action="{{ route('admin.zoning') }}" class="mt-6 flex flex-wrap gap-3">
        <select name="municipality"
                class="flex-1 rounded-lg border-slate-300 bg-white px-3 py-2.5 text-sm focus:border-indigo-500 focus:ring-indigo-500">
            @foreach ($provinces as $province)
                <optgroup label="{{ $province['name'] }}">
                    @foreach ($province['municipalities'] ?? [] as $municipality)
                        <option value="{{ $municipality['slug'] }}" @selected(($selected ?? null) === $municipality['slug'])>
                            {{ $municipality['official_name'] }}
                        </option>
                    @endforeach
                </optgroup>
            @endforeach
        </select>
        <button type="submit" class="rounded-lg border border-slate-300 px-4 py-2 text-sm font-medium transition hover:border-slate-400">
            Load
        </button>
    </form>

    @if (session('status'))
        <p class="mt-6 rounded-lg bg-emerald-50 px-4 py-3 text-sm text-emerald-900 ring-1 ring-emerald-200">{{ session('status') }}</p>
    @endif
    @foreach ($errors->all() as $message)
        <p class="mt-6 rounded-lg bg-red-50 px-4 py-3 text-sm text-red-900 ring-1 ring-red-200">{{ $message }}</p>
    @endforeach

    @if ($config)
        <form method="POST" action="{{ route('admin.zoning.save') }}" class="mt-8 rounded-xl border border-slate-200 p-6">
            @csrf
            <input type="hidden" name="municipality" value="{{ $selected }}">

            <div class="grid gap-4 sm:grid-cols-2">
                <div>
                    <label for="kind" class="block text-sm font-medium text-slate-700">Service kind</label>
                    <select id="kind" name="kind"
                            class="mt-1.5 w-full rounded-lg border-slate-300 bg-white px-3 py-2.5 text-sm focus:border-indigo-500 focus:ring-indigo-500">
                        <option value="">Not configured</option>
                        @foreach ($config['kinds'] ?? [] as $kind)
                            <option value="{{ $kind }}" @selected(($config['kind'] ?? null) === $kind)>{{ $kind }}</option>
                        @endforeach
                    </select>
                    <p class="mt-1.5 text-xs text-slate-500">
                        The query grammar the city speaks. Cities differ only in vocabulary below.
                    </p>
                </div>

                <div>
                    <label for="map_url" class="block text-sm font-medium text-slate-700">Public map URL</label>
                    <input type="url" id="map_url" name="map_url" value="{{ old('map_url', $config['map_url'] ?? '') }}"
                           class="mt-1.5 w-full rounded-lg border-slate-300 px-3 py-2.5 text-sm focus:border-indigo-500 focus:ring-indigo-500">
                    <p class="mt-1.5 text-xs text-slate-500">Shown with every result so a person can check it.</p>
                </div>
            </div>

            <div class="mt-4">
                <label for="endpoint" class="block text-sm font-medium text-slate-700">Endpoint</label>
                <input type="url" id="endpoint" name="endpoint" value="{{ old('endpoint', $config['endpoint'] ?? '') }}"
                       placeholder="https://gis.example.ca/arcgis/rest/services/Zoning/MapServer/0"
                       class="mt-1.5 w-full rounded-lg border-slate-300 px-3 py-2.5 font-mono text-xs focus:border-indigo-500 focus:ring-indigo-500">
            </div>

            <div class="mt-4">
                <label for="config" class="block text-sm font-medium text-slate-700">Field mapping (JSON)</label>
                <textarea id="config" name="config" rows="8"
                          class="mt-1.5 w-full rounded-lg border-slate-300 px-3 py-2.5 font-mono text-xs focus:border-indigo-500 focus:ring-indigo-500">{{ old('config', json_encode($config['config'] ?? new stdClass, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES)) }}</textarea>
                <p class="mt-1.5 text-xs text-slate-500">
                    Which attribute holds what. For ArcGIS:
                    <code>{"fields": {"zoning_code": "ZONE", "address": "CIVIC_ADDRESS"}}</code>.
                    For Socrata and Opendatasoft, also <code>"dataset"</code>.
                </p>
            </div>

            {{-- The gate. Deliberately prominent and off by default. --}}
            <div class="mt-5 rounded-lg bg-amber-50 p-4 ring-1 ring-amber-200">
                <label class="flex items-start gap-3">
                    <input type="checkbox" name="verified" value="1" @checked($config['verified'] ?? false)
                           class="mt-0.5 rounded border-amber-400 text-amber-600 focus:ring-amber-500">
                    <span class="text-sm text-amber-900">
                        <strong class="font-semibold">I have verified this against the city's own service directory.</strong>
                        <span class="mt-1 block text-xs">
                            Until this is ticked the provider is never queried. A layer that responds
                            but carries a similar-looking field returns a confidently wrong zone —
                            and the service responds happily either way.
                        </span>
                    </span>
                </label>
            </div>

            <button type="submit"
                    class="mt-5 w-full rounded-lg bg-slate-900 px-5 py-3 text-sm font-semibold text-white transition hover:bg-slate-700">
                Save configuration
            </button>
        </form>

        @if ($config['preset'] ?? null)
            <div class="mt-6 rounded-xl border border-slate-200 p-5">
                <h2 class="text-sm font-semibold">Suggested starting point</h2>
                <p class="mt-1 text-xs text-slate-500">
                    A recorded suggestion, not a fallback — nothing reads this at runtime.
                    {{ $config['preset']['notes'] ?? '' }}
                </p>
                <pre class="mt-3 overflow-x-auto rounded-lg bg-slate-50 p-3 font-mono text-xs">{{ json_encode($config['preset'], JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES) }}</pre>
            </div>
        @endif
    @endif
</div>
@endsection
