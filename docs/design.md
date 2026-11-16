# training-api: Design Note

## 1. Problem and audience

Runners who log every session to Strava can see each workout in detail, but they can't easily
answer the questions that matter across weeks: *Am I ramping up too fast? How does this week
compare to what I'm used to? Did I just set a personal best?* Strava's free tier shows little
of this, and the paid analytics are fixed views you can't query or extend.

training-api pulls a user's activities from Strava, normalises them into one data model, and
computes training-load metrics: weekly load, the acute:chronic workload ratio (ACWR) and
personal records. It serves them through an authenticated REST API that a dashboard, a
notebook or another service can build on.

ACWR compares the last 7 days of load with the rolling 28-day average. A value well above 1.0
means a sharp ramp-up. The research on it as an injury predictor is disputed, so the API
presents it as a signal of sudden change, not a risk score.

This note is for reviewers of the repository and for me as a reference in later phases.

## 2. Scope

### In scope (v1)

- User registration and login (JWT).
- Connecting a Strava account through OAuth, with encrypted token storage and automatic refresh.
- Historical backfill of a user's activities, respecting Strava's rate limits.
- Near-real-time updates through Strava webhooks (create, update and delete events), handled
  by background jobs.
- Activity CRUD endpoints with pagination and filtering (by date range and sport type).
- Metrics endpoints: weekly load, ACWR and personal records for common distances.
- One consistent JSON error format across the API.

### Out of scope

- A frontend. The API is the product, and `/docs` is the interface.
- Data sources other than Strava (Garmin, Apple Health). The data model shouldn't rule them
  out, but v1 doesn't build them.
- Coaching features: training plans, recommendations, social or team views.
- GPS streams and route maps. v1 stores activity summaries, not second-by-second data.
- Multi-region deployment and horizontal scaling beyond a single API instance and worker.

## 3. Core entities

```
User 1 ── 1 StravaConnection
User 1 ── * Activity
User 1 ── * SyncJob
WebhookEvent (standalone log, linked to a user by Strava athlete ID)
```

- **User**: an account in this system: email, password hash, created timestamp.
- **StravaConnection**: one per user. Stores the Strava athlete ID (unique), the access and
  refresh tokens (encrypted at rest), the token expiry and the granted scopes. It's kept
  separate from User so that auth for this API and auth with Strava stay independent.
- **Activity**: one workout, normalised. Fields include `strava_activity_id` (**unique**, which
  is the duplicate-detection backstop), sport type, start time in UTC, the athlete's local start
  time, moving time, distance, elevation gain, average heart rate if present, a computed `load`
  value, and the original Strava payload as `raw_payload` (JSONB). Keeping the raw payload means
  metrics can be recomputed or extended later without fetching from Strava again.
- **SyncJob**: tracks a backfill or manual sync: status, progress cursor (the last page or
  timestamp fetched), error message and timestamps. Because the cursor is stored, an
  interrupted backfill can resume instead of starting over.
- **WebhookEvent**: every incoming Strava event, stored before it's processed. A unique
  constraint on (object ID, aspect type, event time) makes processing idempotent: a duplicate
  delivery is recorded once and ignored after that.

Metrics (weekly load, ACWR, PRs) are **derived** from Activity, not stored as their own
entities in v1. See open questions.

## 4. Data flows

**Connect Strava (OAuth)**

1. The logged-in user calls `GET /strava/connect` and is redirected to Strava's authorise page.
2. Strava redirects back to our callback with a one-time code.
3. The service exchanges the code for an access token and a refresh token, encrypts them and
   stores them in StravaConnection.
4. A backfill SyncJob is queued.

**Historical backfill**

1. A worker picks up the SyncJob and pages through the athlete's activity list from Strava.
2. Each activity is normalised and upserted on `strava_activity_id`, so re-running a page is safe.
3. The job saves its cursor after each page. If it's close to the rate limit, it reschedules
   itself for the next limit window instead of failing.

**Webhook → job → metric**

1. Strava POSTs an event (`object_type`, `object_id`, `aspect_type`, `owner_id`, `event_time`).
2. The endpoint checks that the subscription ID matches ours, stores the WebhookEvent (a
   duplicate hits the unique constraint and is skipped), queues a job and returns `200`
   immediately. Strava expects a response within about two seconds, so no work happens inline.
3. The worker fetches the activity from Strava and upserts it, or deletes it for a `delete`
   event.
4. The worker invalidates that user's cached metrics in Redis.
5. The next metrics request recomputes from Postgres (window functions over daily load) and
   caches the result.

## 5. Risks

- **Rate limits.** Strava enforces per-15-minute and daily request limits per application. A
  large backfill can exhaust them. Mitigation: read the rate-limit headers on every response,
  pause and reschedule jobs near the limit, and use exponential backoff on `429` responses.
- **Duplicate and out-of-order webhooks.** Strava can deliver the same event more than once,
  and an update can arrive before its create. Mitigation: the idempotency constraint on
  WebhookEvent, upserts keyed on `strava_activity_id`, and always fetching the current state
  from Strava instead of trusting event order.
- **Token expiry and rotation.** Access tokens expire after about six hours, and refreshing can
  return a *new* refresh token. Mitigation: refresh shortly before expiry, always store the
  newly returned refresh token, and mark the connection as needing re-authorisation if refresh
  fails.
- **Webhook authenticity.** Strava validates the subscription once, with a challenge, but
  doesn't sign individual events. Mitigation: check the subscription ID, treat every event as
  a hint, and fetch the real data from Strava with the user's token.
- **Deauthorisation and data deletion.** Users can revoke access from Strava, and Strava's API
  terms require removing their data when that happens. Mitigation: handle the athlete
  deauthorisation webhook by deleting the connection and the user's Strava-sourced activities.
- **Time zones.** "This week" depends on the athlete's local time, not UTC. Mitigation: store
  the local start time and bucket weeks by it.

## 6. Stack choices

- **Python 3.12**: stable, full library support, modern typing syntax.
- **uv**: fast, reproducible installs from a committed lockfile.
- **FastAPI**: validation from type hints, auto-generated OpenAPI docs, async for I/O-bound work.
- **SQLAlchemy 2.0 (async) + Alembic**: typed models, non-blocking queries, versioned migrations.
- **Postgres 16**: relational data, unique constraints for deduplication, JSONB for raw
  payloads, window functions for rolling load.
- **Redis 7**: job queue and metrics cache in one service.
- **Docker + Compose**: one-command local environment that matches production.

Layering: `api → services → db`, never the reverse. HTTP concerns stay in `api/`, business
logic in `services/`, and persistence in `db/`. The database enforces the critical guarantees
(uniqueness) as a backstop to the service-level checks.

## Open questions

- **How is per-activity load defined?** Options: duration × perceived effort (session RPE), a
  heart-rate-based score (TRIMP), or Strava's relative effort (not available on every
  activity). Leaning: store `load` on each Activity with the formula version, so the formula
  can change and old activities can be recomputed.
- **Compute metrics on request or precompute them?** Leaning: compute on request with Redis
  caching in v1, and precompute only if profiling shows it's needed.
- **Job queue: arq or Celery?** arq is async-native and lightweight; Celery is more mature
  and has more features. To be decided in Phase 4.
- **Which distances count for PRs** (5k, 10k, half, full), and does v1 use whole-activity
  distance or best efforts within an activity?
