{{--
    Application shell.

    Tailwind from a CDN, no build step, no Node. The only JavaScript in the
    whole application lives on the /ask page; this layout ships none.
--}}
<!DOCTYPE html>
<html lang="en" class="h-full">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>@yield('title', 'SignLaw AI') — AI assistant for Canadian sign bylaws</title>
    <meta name="description" content="Ask questions about Canadian municipal sign bylaws and get answers cited to the exact bylaw, section and page.">
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="flex h-full flex-col bg-white text-slate-900 antialiased">

<header class="border-b border-slate-200">
    <nav class="mx-auto flex max-w-5xl items-center justify-between px-5 py-4">
        <a href="{{ route('landing') }}" class="flex items-baseline gap-2">
            <span class="text-lg font-semibold tracking-tight">SignLaw<span class="text-indigo-600">&nbsp;AI</span></span>
        </a>

        <div class="flex items-center gap-1 text-sm">
            @php
                // route => [label, active-pattern]
                $links = [
                    'ask' => ['Ask', 'ask'],
                    'zoning-check' => ['Zoning check', 'zoning-check'],
                    // Points at the dashboard, which redirects to sign-in when
                    // the session is absent — so there is one Admin link
                    // whether or not the operator is already signed in.
                    'admin.dashboard' => ['Admin', 'admin.*'],
                ];
            @endphp
            @foreach ($links as $route => [$label, $pattern])
                <a href="{{ route($route) }}"
                   class="rounded-lg px-3 py-1.5 transition {{ request()->routeIs($pattern) ? 'bg-slate-100 font-medium text-slate-900' : 'text-slate-600 hover:text-slate-900' }}">
                    {{ $label }}
                </a>
            @endforeach
        </div>
    </nav>
</header>

<main class="flex-1">
    @yield('content')
</main>

<footer class="mt-20 border-t border-slate-200">
    <div class="mx-auto max-w-5xl px-5 py-8">
        {{--
            Not boilerplate. This product answers questions people act on when
            applying for permits and fabricating signage, and the disclaimer
            belongs on every page rather than only where an answer appears.
        --}}
        <p class="text-xs leading-relaxed text-slate-500">
            <strong class="font-medium text-slate-700">Informational only — not legal advice.</strong>
            Answers are generated from indexed bylaw text and may be incomplete or out of date.
            Confirm with the municipality before applying for a permit or fabricating signage.
        </p>
        <p class="mt-3 text-xs text-slate-400">
            SignLaw AI · British Columbia municipal sign bylaws
        </p>
    </div>
</footer>

</body>
</html>
