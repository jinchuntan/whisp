# Persephone rebrand — migration guide

The product formerly named **Whisp** is now **Persephone**. This is a rebrand, not
a redesign or a rewrite — all functionality is preserved. This guide covers what
changed and the steps to adopt it. The Faster-Whisper transcription engine is
unrelated to the product name and is unchanged (`faster_whisper_*` modes,
`FASTER_WHISPER_*` settings, `WhisperModel`, etc. all remain).

## What changed in code

- Python packages: `whisp_api` → `persephone_api`, `whisp_worker` → `persephone_worker`.
- Firmware: `firmware/whisp_badge/` → `firmware/persephone_badge/`, sketch
  `whisp_badge.ino` → `persephone_badge.ino`.
- Badge auth header: primary is now **`X-Persephone-Key`**. The old **`X-Whisp-Key`**
  is still accepted as a **deprecated** alias for one migration period (reject if
  both are sent with different values). Firmware sends the new header.
- Session cookies: `whisp_at`/`whisp_rt` → `persephone_at`/`persephone_rt`
  (you will be signed out once — see step 9).
- Default private Storage bucket: `whisp-audio` → **`persephone-audio`**
  (configurable via `SUPABASE_AUDIO_BUCKET`).
- Firmware config macros: `WHISP_ROOT_CA`/`WHISP_INSECURE_TLS` →
  `PERSEPHONE_ROOT_CA`/`PERSEPHONE_INSECURE_TLS`.
- Integration-test env vars: `WHISP_RUN_INTEGRATION` / `WHISP_SAMPLE_WAV` /
  `WHISP_TEST_SUPABASE` → `PERSEPHONE_*`.
- Wordmark/branding, docs, logger names, OpenAPI metadata → Persephone.

Generic names (`badge`, `question`, `round`, `cluster`, `event`, `worker`,
`audio`, `transcript`) and non-product env vars (`BADGE_API_KEY`, `SUPABASE_*`,
`ADMIN_EMAIL_ALLOWLIST`, `TRANSCRIPTION_MODE`, `AGORA_*`) are unchanged.

## Adoption steps

1. **Pull the rebrand changes** (this branch/commit).
2. **Run the new Supabase migration** `supabase/migrations/002_rebrand_persephone.sql`
   in the Supabase SQL editor. It is non-destructive: it only creates the new
   bucket and never touches `whisp-audio`, existing tables, or RLS.
3. **Confirm the `persephone-audio` bucket** exists and is **private** (Storage →
   Buckets). Create it manually (Public = OFF) if your project blocks
   `storage.buckets` inserts.
4. **Update local `.env`** (root): no product-named vars changed, but if you set
   `SUPABASE_AUDIO_BUCKET` explicitly, point it at `persephone-audio` (or leave it
   on `whisp-audio` temporarily — both work; the default is now `persephone-audio`).
5. **Update `worker/.env`**: same bucket note as above. If you use the optional
   integration tests, rename `WHISP_RUN_INTEGRATION` / `WHISP_SAMPLE_WAV` /
   `WHISP_TEST_SUPABASE` to their `PERSEPHONE_*` equivalents.
6. **Update Vercel environment variables**: review `SUPABASE_AUDIO_BUCKET` (set to
   `persephone-audio` once the bucket exists). No other var names changed. Redeploy
   so the new values take effect.
7. **Move the firmware `config.h` safely**: it lives at
   `firmware/persephone_badge/config.h` (it was moved with the directory rename and
   remains gitignored). If you kept a copy elsewhere, place it there. Do not commit it.
8. **Reflash the firmware** from `firmware/persephone_badge/persephone_badge.ino`.
   It now sends `X-Persephone-Key` and shows the `PERSEPHONE` wordmark. Keep your
   existing `API_BASE_URL` — it does not need to change even if it still contains
   the old domain (see "External services" below).
9. **Sign in again**: the session cookie was renamed, so existing dashboard
   sessions are invalidated once. Log in again with your host email/password. (No
   sensitive values are migrated through JavaScript; tokens stay in HttpOnly cookies.)
10. **Rename external services manually** (optional, cosmetic) — see the checklist below.

## Deprecation timeline

- `X-Whisp-Key` badge header: honored now, remove after all badges are reflashed
  to send `X-Persephone-Key`. Tracked in `persephone_api/auth.py` (`LEGACY_BADGE_HEADER`).
- `whisp-audio` bucket: keep as long as you need read access to historical audio.
  New uploads go to `persephone-audio`.

## External services to rename manually

The source rebrand cannot rename external/hosted resources. Rename these yourself
if you want them to read "Persephone" (none are required for the app to work):

- [ ] **GitHub repository** display name / description (the clone URL
      `github.com/jinchuntan/whisp` still works; renaming the repo also updates its URL).
- [ ] **Local clone directory** (e.g. `…/GitHub/whisp` → `…/GitHub/persephone`) —
      optional; nothing depends on the folder name.
- [ ] **Vercel project name**.
- [ ] **Production domain** (e.g. `whispspace.vercel.app`). The firmware keeps using
      whatever `API_BASE_URL` is configured until you change it.
- [ ] **Supabase project display name**.
- [ ] **Supabase Storage bucket**: create `persephone-audio` (migration 002); the
      old `whisp-audio` can remain for historical objects.
- [ ] **Agora project display name** (optional; `AGORA_*` config is unchanged).
- [ ] **Environment variables in Vercel** — review `SUPABASE_AUDIO_BUCKET`.
- [ ] **Environment variables in `worker/.env`** — bucket + any `PERSEPHONE_*` test vars.
- [ ] **Firmware `API_BASE_URL`** — only if/when the production domain changes.

Do not assume the deployment URL changes immediately; the badge must keep working
against its configured `API_BASE_URL` even if that still contains the old name.
