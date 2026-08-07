# Deploying the frontend to cPanel

Laravel Blade on shared PHP hosting, talking to the FastAPI backend on a GPU
host. No Node, no build step, no queue worker, no cron.

Assumes SSH or cPanel Terminal, and that you can set the domain's document root.

---

## 0. Check the host first

```bash
php -v          # 8.2 or newer
composer -V     # any 2.x
```

If `php -v` reports 8.1 or older, switch it in cPanel under **MultiPHP Manager**
before going further — Laravel 11 and 12 both require 8.2.

If `composer` is missing:

```bash
curl -sS https://getcomposer.org/installer | php
alias composer='php ~/composer.phar'
```

---

## 1. Build the application

The repository holds only the application-specific files — controllers, views,
routes, the API client. Laravel's framework is not vendored. So the skeleton is
created once and the repository files are laid over it.

```bash
cd ~
git clone https://github.com/idesign2601/signlaw-ai.git signlaw-src
composer create-project laravel/laravel signlaw-app
cp -r signlaw-src/frontend/* signlaw-app/
cd signlaw-app
composer install --no-dev --optimize-autoloader
```

`--no-dev` matters: it omits PHPUnit and the debug tooling, which have no place
on a public host.

Updating later is `git pull` in `signlaw-src`, then the `cp -r` again. The
overlay is deliberately one-directional — nothing in `signlaw-app` that the
repository does not own gets touched.

---

## 2. Configure

```bash
php artisan key:generate
```

Then edit `.env`:

```ini
APP_NAME="SignLaw AI"
APP_ENV=production
APP_DEBUG=false
APP_URL=https://yourdomain.com

# Laravel's own database is unused — this application stores nothing.
DB_CONNECTION=sqlite
SESSION_DRIVER=file
CACHE_STORE=file

SIGNLAW_API_URL=https://api.yourdomain.com
SIGNLAW_API_KEY=
SIGNLAW_ADMIN_KEY=
SIGNLAW_API_TIMEOUT=120

ADMIN_PASSWORD=
```

**`APP_DEBUG=false` is not optional.** With it true, any unhandled exception
renders a stack trace including environment variables — your API keys — to
whoever triggered it.

Leave the three secrets empty until the backend exists. The site runs without
them; coverage degrades to "the answering service could not be reached", which
is the intended behaviour rather than a crash.

`DB_CONNECTION=sqlite` is there because Laravel wants a default configured. This
application has no database and never migrates: documents, municipalities and
sessions all live in the backend, so two operators on two machines see the same
thing.

---

## 3. Permissions

```bash
chmod -R 775 storage bootstrap/cache
```

Skipping this produces a 500 on the first request that tries to write a session
or a log, with nothing useful in the browser.

---

## 4. Point the domain at `public/`

In cPanel → **Domains** → your domain → **Document Root**, set:

```
/home/<cpanel-user>/signlaw-app/public
```

Not `signlaw-app`. Everything outside `public/` must stay unreachable over HTTP
— `.env` sits one level up, and a document root one directory too high makes it
downloadable.

Verify after the DNS or Apache reload:

```bash
curl -sI https://yourdomain.com | head -1              # 200
curl -sI https://yourdomain.com/.env | head -1         # 403 or 404, never 200
```

The second check is the one worth doing. A 200 there means the API keys are
public.

---

## 5. Cache the configuration

```bash
php artisan config:cache
php artisan route:cache
php artisan view:cache
```

**Re-run `config:cache` after every `.env` change.** Cached configuration
ignores the file, and "my change did nothing" is almost always this.

---

## 6. Uploads

Admin PDF upload is capped at 50 MB by the form. PHP is usually lower. In cPanel
→ **MultiPHP INI Editor**:

```ini
upload_max_filesize = 64M
post_max_size = 64M
max_execution_time = 300
```

Without this a large scanned bylaw fails before Laravel sees it, and the browser
shows an empty response rather than an error.

---

## 7. Connect the backend

Once the GPU host is up (`docs/VAST_RUNBOOK.md`), generate a key and put the
same value on both sides:

```bash
openssl rand -hex 32
```

| Where | Setting |
|---|---|
| Backend `.env` | `SECURITY__API_KEYS=<key>` |
| Backend `.env` | `SECURITY__ADMIN_API_KEY=<a second, different key>` |
| Frontend `.env` | `SIGNLAW_API_KEY=<key>` |
| Frontend `.env` | `SIGNLAW_ADMIN_KEY=<the second key>` |
| Frontend `.env` | `SIGNLAW_API_URL=https://api.yourdomain.com` |

Then `php artisan config:cache` again.

The backend needs a stable HTTPS address, which Vast does not provide — its host
and port change on every rebuild. Use a Cloudflare Tunnel; `docs/SPLIT_DEPLOYMENT.md`
§3 has the commands.

---

## 8. Verify

Load the site. The coverage section is the useful signal:

| What you see | Meaning |
|---|---|
| A list of municipalities | Frontend and backend are talking |
| "Coverage is unavailable…" | Wrong `SIGNLAW_API_URL`, or the backend is down |
| "The answering service rejected this application" | `SIGNLAW_API_KEY` does not match |

Then sign in at `/admin/login` with `ADMIN_PASSWORD` and confirm the dashboard
lists documents.

---

## Notes

**Nothing here is stateful.** No migrations, no queue, no cron, no storage
beyond sessions and logs. Redeploying is `cp -r` and `config:cache`, and losing
the host loses nothing.

**The admin password is convenience, not the control.** The backend requires
`X-Admin-Key` on every admin route and this application is the only thing
holding it. A forged session here still cannot reach the API.
