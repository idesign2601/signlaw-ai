@extends('layouts.app')

@section('title', 'Admin sign in')

@section('content')
<div class="mx-auto max-w-sm px-5 py-20">
    <h1 class="text-xl font-semibold tracking-tight">Admin sign in</h1>
    <p class="mt-2 text-sm text-slate-600">Document management for indexed bylaws.</p>

    <form method="POST" action="{{ route('admin.login.submit') }}" class="mt-8">
        @csrf

        <label for="password" class="block text-sm font-medium text-slate-700">Password</label>
        <input type="password" id="password" name="password" required autofocus autocomplete="current-password"
               class="mt-1.5 w-full rounded-lg border-slate-300 px-3 py-2.5 text-sm focus:border-indigo-500 focus:ring-indigo-500">

        @error('password')
            <p class="mt-2 text-sm text-red-600">{{ $message }}</p>
        @enderror

        <button type="submit"
                class="mt-4 w-full rounded-lg bg-slate-900 px-5 py-3 text-sm font-semibold text-white transition hover:bg-slate-700">
            Sign in
        </button>
    </form>
</div>
@endsection
