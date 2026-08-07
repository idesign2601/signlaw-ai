<?php

declare(strict_types=1);

namespace App\Http\Middleware;

use Closure;
use Illuminate\Http\Request;
use Symfony\Component\HttpFoundation\Response;

/**
 * Gate for the admin area.
 *
 * A single shared password held in the environment, checked once and remembered
 * in the session. No users table, no registration, no password reset — this
 * application has no database of its own, and adding one to authenticate a
 * single operator would be the larger risk.
 *
 * The real protection is one layer down: the backend requires X-Admin-Key on
 * every admin route, and this application is the only thing that holds it. A
 * session forged here still cannot reach the API without that key.
 */
final class RequireAdmin
{
    public function handle(Request $request, Closure $next): Response
    {
        if ($request->session()->get('admin_authenticated') !== true) {
            return redirect()
                ->route('admin.login')
                ->with('intended', $request->fullUrl());
        }

        return $next($request);
    }
}
