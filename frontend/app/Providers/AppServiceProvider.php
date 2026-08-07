<?php

declare(strict_types=1);

namespace App\Providers;

use App\Services\SignLawClient;
use Illuminate\Support\Facades\URL;
use Illuminate\Support\ServiceProvider;

/**
 * Overlays the stock skeleton provider.
 *
 * Shipped in the repository rather than edited on the server: a binding added
 * by hand after every deploy is a binding that will eventually be forgotten,
 * and the failure — a container resolution error on the first page load — is
 * needlessly confusing.
 */
final class AppServiceProvider extends ServiceProvider
{
    public function register(): void
    {
        // Singleton: the client is stateless, and the coverage cache should be
        // shared across everything handling one request.
        $this->app->singleton(
            SignLawClient::class,
            static fn (): SignLawClient => new SignLawClient(
                (string) config('signlaw.api_url'),
                (int) config('signlaw.timeout'),
                (int) config('signlaw.coverage_cache_seconds'),
                config('signlaw.api_key'),
                config('signlaw.admin_key'),
            ),
        );
    }

    public function boot(): void
    {
        // Behind a shared-hosting proxy Laravel often sees http:// and builds
        // form actions and redirects on that scheme, which a browser on an
        // https:// page then refuses as mixed content. Forcing the scheme in
        // production avoids a class of bug that only appears once deployed.
        if ($this->app->environment('production')) {
            URL::forceScheme('https');
        }
    }
}
