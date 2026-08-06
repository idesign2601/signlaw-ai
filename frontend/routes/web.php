<?php

declare(strict_types=1);

use App\Http\Controllers\AskController;
use Illuminate\Support\Facades\Route;

/*
| One page, two routes: render it, and post a question to it. Everything else
| — provinces, municipalities, coverage — is data from the API, not routing.
*/

Route::get('/', [AskController::class, 'index'])->name('ask.index');
Route::post('/', [AskController::class, 'ask'])->name('ask.submit');
