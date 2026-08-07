<?php

declare(strict_types=1);

namespace App\Http\Controllers;

use App\Services\SignLawClient;
use Illuminate\View\View;

/**
 * Public pages.
 *
 * The landing page is the only one that touches the API, and only for the
 * coverage list. The admin area lives in its own controller behind its own
 * middleware.
 */
final class PageController extends Controller
{
    public function __construct(private readonly SignLawClient $client)
    {
    }

    public function landing(): View
    {
        return view('landing', ['coverage' => $this->client->coverage()]);
    }

    public function zoningCheck(): View
    {
        return view('zoning-check');
    }
}
