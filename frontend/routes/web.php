<?php

declare(strict_types=1);

use App\Http\Controllers\AdminController;
use App\Http\Controllers\AskController;
use App\Http\Controllers\PageController;
use App\Http\Middleware\RequireAdmin;
use Illuminate\Support\Facades\Route;

/*
| Public pages, then the admin area. Provinces, municipalities and coverage are
| data from the API, not routing — adding Alberta adds no route here.
*/

Route::get('/', [PageController::class, 'landing'])->name('landing');

Route::get('/ask', [AskController::class, 'index'])->name('ask');
Route::post('/ask', [AskController::class, 'ask'])->name('ask.submit');

Route::get('/zoning-check', [PageController::class, 'zoningCheck'])->name('zoning-check');

/*
| Admin. Throttled at the door: a single shared password with unlimited attempts
| is a password with no entropy budget. Six tries a minute per IP makes an
| online guess impractical without inconveniencing an operator who mistypes.
*/
Route::prefix('admin')->name('admin.')->group(function (): void {
    Route::get('/login', [AdminController::class, 'showLogin'])->name('login');
    Route::post('/login', [AdminController::class, 'login'])
        ->middleware('throttle:6,1')
        ->name('login.submit');

    Route::middleware(RequireAdmin::class)->group(function (): void {
        Route::get('/', [AdminController::class, 'dashboard'])->name('dashboard');
        Route::get('/upload', [AdminController::class, 'showUpload'])->name('upload');
        Route::post('/upload', [AdminController::class, 'upload'])->name('upload.submit');
        Route::get('/zoning', [AdminController::class, 'zoning'])->name('zoning');
        Route::post('/zoning', [AdminController::class, 'saveZoning'])->name('zoning.save');
        Route::post('/logout', [AdminController::class, 'logout'])->name('logout');
    });
});
