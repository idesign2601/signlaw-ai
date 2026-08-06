# Split deployment — frontend on web hosting, AI on a GPU

Laravel on ordinary PHP hosting; FastAPI, Ollama, the embedding models and
Postgres on a Vast.ai GPU instance.

Companion to `VAST_RUNBOOK.md`, which covers the GPU box in isolation. This
document is about the wire between the two.

---

## Why the split works

Laravel calls the backend **server-side**, from PHP. The visitor's browser only
ever talks to your web host. Three consequences, all good:

- **No CORS.** Nothing to configure, nothing to get wrong.
- **No mixed content.** Your site can be HTTPS while you are still sorting out
  certificates on the GPU side.
- **The API key never reaches the browser.** It lives in the web host's `.env`
  and travels only between the two servers.

Shared hosting is fine. The frontend needs PHP 8.2+, Composer and outbound
HTTPS. No Node, no build step, no websockets.

---

## What is public and what is not

| Service | Port | Exposed? |
|---|---|---|
| FastAPI | 8000 | **Yes** — the only public door |
| Ollama | 11434 | **Never.** No authentication of any kind. An exposed 11434 is an open inference endpoint on your card. |
| Postgres | 5432 | No. Localhost only. |

---

## 1. Generate the API key

Before the split, `/ask` had no authentication. It does now: `X-API-Key`,
enforced on the whole `/api/v1` router so a route added later inherits it.

This is not about the bylaws being secret — they are published documents. It is
about who spends your GPU time. One question occupies the card for seconds, and
an open endpoint is a free inference service for whoever finds it.

```bash
openssl rand -hex 32
```

Keep the output. It goes in two places and nowhere else.

---

## 2. GPU host — backend

Follow `VAST_RUNBOOK.md` §1–§7 first. Then add to the backend `.env`:

```ini
ENVIRONMENT=staging
SECURITY__API_KEYS=<the key from step 1>
SECURITY__ADMIN_API_KEY=<a second, different key>
OBSERVABILITY__LOG_FORMAT=json
```

Several keys are supported, comma-separated, so a second client can be added or
revoked without disrupting the first.

`ENVIRONMENT=production` additionally refuses to boot with a default database
password, `DEBUG=true`, wildcard CORS or missing keys. Use it once the address
is stable.

Bind FastAPI to all interfaces so the port mapping can reach it:

```bash
cd backend && .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Confirm the guard is live — this must fail:

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8000/api/v1/municipalities
# 401
```

And this must succeed:

```bash
curl -s -H "X-API-Key: <key>" http://localhost:8000/api/v1/municipalities | head -c 200
```

If the first returns 200, the key did not load. Do not expose the port until it
returns 401.

---

## 3. A stable, encrypted address

Vast.ai gives you a host and a mapped port that **change when the instance is
recreated**. Hard-coding them into the frontend means editing the web host's
`.env` after every rebuild, and there is no TLS.

Use a Cloudflare Tunnel. It gives a fixed hostname, terminates TLS, and opens no
inbound ports on the instance — the tunnel dials out.

```bash
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o /usr/local/bin/cloudflared
chmod +x /usr/local/bin/cloudflared
cloudflared tunnel login
cloudflared tunnel create signlaw-api
cloudflared tunnel route dns signlaw-api api.yourdomain.com
nohup cloudflared tunnel run --url http://localhost:8000 signlaw-api > /tmp/tunnel.log 2>&1 &
```

`api.yourdomain.com` now points at the instance over HTTPS, and keeps pointing
there after a rebuild — you re-run the tunnel, you do not re-point the frontend.

**If you skip the tunnel** and use Vast's mapped port directly, the traffic is
plaintext HTTP: questions and answers in the clear, and the API key with them.
Acceptable for a private test, not for anything real.

---

## 4. Web host — frontend

Upload the Laravel app, then set in its `.env`:

```ini
APP_ENV=production
APP_DEBUG=false
SIGNLAW_API_URL=https://api.yourdomain.com
SIGNLAW_API_KEY=<the same key from step 1>
SIGNLAW_API_TIMEOUT=120
```

The 120-second timeout looks excessive and is not: a cold Ollama model load
costs 10–30 seconds and is paid on the first question after any idle period. A
conventional 30-second timeout fails that request and reads as a broken product.
`OLLAMA_KEEP_ALIVE=30m` on the GPU side is what makes it rare.

On shared hosting, point the domain's document root at `public/`. If you cannot
change the document root, most panels allow it per-domain under "Addon domains"
or "Document root".

Then:

```bash
composer install --no-dev --optimize-autoloader
php artisan config:cache && php artisan route:cache && php artisan view:cache
```

Re-run `config:cache` after any `.env` change, or the old values persist.

---

## 5. Verify end to end

```bash
curl -sI https://yourdomain.com | head -1          # 200 from the web host
curl -s -o /dev/null -w '%{http_code}\n' https://api.yourdomain.com/healthz   # 200
```

Then load the site. The coverage section is the useful signal: if it says
"Coverage is unavailable because the answering service could not be reached",
the frontend cannot talk to the backend — check `SIGNLAW_API_URL`, then the key.
If it lists municipalities, the wire is good.

---

## 6. What happens when the GPU instance is replaced

This is the case worth rehearsing before it happens unexpectedly.

Everything on the instance is lost. Of the four data classes, only one is
unrecoverable:

| Data | Recovery |
|---|---|
| Model weights (~15 GB) | `make fetch-models`, `ollama pull` |
| PDFs | `scp` again, or re-download |
| Code | `git clone` |
| **Postgres** | **Restore a dump, or re-ingest the whole corpus** |

Postgres holds the corpus, the embeddings, the amendment lineage and the
retrieval traces — the audit trail that lets a disputed answer be reconstructed
months later. For a legal tool that is the part whose loss actually hurts.

```bash
make backup
scp -P <port> backups/signlaw-*.dump you@somewhere-permanent:/backups/
```

Do it after every ingest, and keep the dumps somewhere that is not the GPU box.

The frontend needs no changes across a rebuild, as long as you kept the tunnel
hostname. That is the whole argument for step 3.

---

## 7. Known gaps

**Rate limiting is configured but not enforced.** `SECURITY__RATE_LIMIT_PER_MINUTE`
exists in settings and nothing reads it. The API key is currently the only thing
between a caller and unlimited GPU time, so treat the key as the control and do
not publish it. Enforcement belongs in Phase 6.

**Postgres is on the GPU instance.** This contradicts the standing decision that
Postgres must not depend on Vast.ai ephemeral storage. It is the documented
fallback (topology A) and is acceptable while backups are taken, but topology B
— Postgres on a persistent host — needs `OllamaEmbeddingProvider`, which does
not exist yet. See `DEPLOYMENT.md` §3.

**Source PDFs are not served.** Citations carry `source_url: null` because
serving documents is Phase 6. Municipality, bylaw number, section and page are
rendered, which is enough to find the passage by hand.
