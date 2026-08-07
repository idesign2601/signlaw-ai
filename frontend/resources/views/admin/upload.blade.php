@extends('layouts.app')

@section('title', 'Upload a bylaw')

@section('content')
@php
    $provinces = $coverage['provinces'] ?? [];

    // Every catalogued municipality, not only the available ones — uploading is
    // how a municipality becomes available in the first place.
    $catalogue = [];
    foreach ($provinces as $province) {
        foreach ($province['municipalities'] ?? [] as $municipality) {
            $catalogue[$province['code']][] = [
                'slug' => $municipality['slug'],
                'label' => $municipality['official_name'],
                'indexed' => $municipality['document_count'] ?? 0,
            ];
        }
    }
@endphp

<div class="mx-auto max-w-2xl px-5 py-12">

    <div class="flex items-center justify-between">
        <h1 class="text-2xl font-semibold tracking-tight">Upload a bylaw</h1>
        <a href="{{ route('admin.dashboard') }}" class="text-sm text-slate-600 hover:text-slate-900">← Dashboard</a>
    </div>

    <form method="POST" action="{{ route('admin.upload.submit') }}" enctype="multipart/form-data"
          class="mt-8 rounded-xl border border-slate-200 p-6" id="upload-form">
        @csrf

        <div class="grid gap-4 sm:grid-cols-2">
            <div>
                <label for="province" class="block text-sm font-medium text-slate-700">Province</label>
                <select id="province" name="province"
                        class="mt-1.5 w-full rounded-lg border-slate-300 bg-white px-3 py-2.5 text-sm focus:border-indigo-500 focus:ring-indigo-500">
                    @foreach ($provinces as $province)
                        <option value="{{ $province['code'] }}" @selected(old('province') === $province['code'])>
                            {{ $province['name'] }}
                        </option>
                    @endforeach
                </select>
            </div>

            <div>
                <label for="municipality" class="block text-sm font-medium text-slate-700">Municipality</label>
                <select id="municipality" name="municipality"
                        class="mt-1.5 w-full rounded-lg border-slate-300 bg-white px-3 py-2.5 text-sm focus:border-indigo-500 focus:ring-indigo-500">
                </select>
                <p class="mt-1.5 text-xs text-slate-500">
                    The City and Township of Langley are separate jurisdictions with separate bylaws. So are the City and District of North Vancouver.
                </p>
            </div>
        </div>

        <div class="mt-4 grid gap-4 sm:grid-cols-3">
            <div class="sm:col-span-2">
                <label for="title" class="block text-sm font-medium text-slate-700">Document title</label>
                <input type="text" id="title" name="title" required maxlength="500"
                       value="{{ old('title') }}" placeholder="Sign Bylaw"
                       class="mt-1.5 w-full rounded-lg border-slate-300 px-3 py-2.5 text-sm focus:border-indigo-500 focus:ring-indigo-500">
            </div>

            <div>
                <label for="year" class="block text-sm font-medium text-slate-700">Bylaw year</label>
                <input type="number" id="year" name="year" min="1900" max="2100"
                       value="{{ old('year') }}" placeholder="1972"
                       class="mt-1.5 w-full rounded-lg border-slate-300 px-3 py-2.5 text-sm focus:border-indigo-500 focus:ring-indigo-500">
            </div>
        </div>

        <div class="mt-4">
            <label for="document" class="block text-sm font-medium text-slate-700">PDF file</label>
            <input type="file" id="document" name="document" accept="application/pdf" required
                   class="mt-1.5 w-full rounded-lg border border-slate-300 px-3 py-2.5 text-sm file:mr-3 file:rounded file:border-0 file:bg-slate-900 file:px-3 file:py-1.5 file:text-xs file:font-semibold file:text-white">
            <p class="mt-1.5 text-xs text-slate-500">
                Upload the consolidated version where one exists — it folds in the amendments,
                so the indexed text is what is currently in force.
            </p>
        </div>

        @foreach ($errors->all() as $message)
            <p class="mt-3 rounded-lg bg-red-50 px-3 py-2 text-sm text-red-800 ring-1 ring-red-200">{{ $message }}</p>
        @endforeach

        <button type="submit" id="upload-button"
                class="mt-5 w-full rounded-lg bg-slate-900 px-5 py-3 text-sm font-semibold text-white transition hover:bg-slate-700 disabled:cursor-not-allowed disabled:opacity-60">
            Upload and index
        </button>

        <p class="mt-3 text-xs text-slate-500">
            Indexing runs in the background and takes a few minutes for a large or scanned
            bylaw. You will be returned to the dashboard, where its status updates.
        </p>
    </form>

    <div class="mt-6 rounded-xl border border-slate-200 p-5">
        <h2 class="text-sm font-semibold">What happens on upload</h2>
        <ol class="mt-2 list-inside list-decimal space-y-1 text-sm text-slate-600">
            <li>Text extracted page by page, with OCR for pages that have no text layer</li>
            <li>Tables lifted out with their structure preserved</li>
            <li>Sections parsed, so citations can name a section and a page</li>
            <li>Chunked without crossing section boundaries, then embedded and indexed</li>
        </ol>
        <p class="mt-3 text-xs text-slate-500">
            Whether the bylaw is in force is not set here. Currency is resolved across the
            whole corpus by the amendment lineage pass, because asserting it by hand is how
            confident citations to repealed law happen.
        </p>
    </div>
</div>

<script>
    (function () {
        const catalogue = @json($catalogue);
        const province = document.getElementById('province');
        const municipality = document.getElementById('municipality');
        const previous = @json(old('municipality'));

        function repopulate() {
            const options = catalogue[province.value] || [];
            municipality.innerHTML = '';
            for (const option of options) {
                const label = option.indexed > 0
                    ? option.label + ' (' + option.indexed + ' indexed)'
                    : option.label;
                const element = new Option(label, option.slug);
                element.selected = option.slug === previous;
                municipality.appendChild(element);
            }
        }

        if (previous) {
            for (const [code, options] of Object.entries(catalogue)) {
                if (options.some((option) => option.slug === previous)) {
                    province.value = code;
                    break;
                }
            }
        }

        province.addEventListener('change', repopulate);
        repopulate();

        const form = document.getElementById('upload-form');
        const button = document.getElementById('upload-button');
        form.addEventListener('submit', function () {
            button.disabled = true;
            button.textContent = 'UPLOADING…';
        });
    })();
</script>
@endsection
