# Mixtape — Codebase Map

## App structure at a glance

Mixtape is a Flask + SQLAlchemy app where friends share songs, build collaborative playlists, and track listening stats. The app follows a strict three-layer structure: `app.py` wires everything together, `routes/` handles HTTP concerns, and `services/` holds all business logic. `models.py` defines the data itself.

## Main files

**`app.py`** — the Flask application factory (`create_app`). Initializes the `db` (Flask-SQLAlchemy) object, sets config (DB URI, secret key, defaults to a local SQLite file), registers the four blueprints (`songs`, `playlists`, `users`, `feed`) under their URL prefixes, and calls `db.create_all()`. This is the only file that touches Flask app setup — nothing else imports `Flask` directly.

**`models.py`** — defines all SQLAlchemy models and three association tables. All primary keys are UUID strings (via `generate_uuid()`), not auto-increment ints.

- `User` — username/email, plus `listening_streak` and `last_listened_at` columns that back the streak feature. Has a self-referential many-to-many `friends` relationship through the `friendships` table (each direction stored as its own row — friendships are recorded symmetrically, not inferred).
- `Song` — title/artist/album/genre, who shared it (`shared_by`) and when, an optional `share_note`. Tags come through `song_tags` (plain many-to-many).
- `ListeningEvent` — one row per listen (`user_id`, `song_id`, `listened_at`). This is the source of truth for both streaks and feeds — there's no separate "feed" table.
- `Rating` — a 1–5 `score` per (user, song) pair, enforced unique via `UniqueConstraint`.
- `Playlist` — name/creator/`is_collaborative` flag. Songs attach via `playlist_entries`, which (unlike `song_tags`) is a table with *extra* columns: `position` (explicit ordering, not insertion order), `added_by`, and `added_at`.
- `Notification` — generic per-user record (`notification_type`, `body`, `read` flag). Nothing links a notification back to the event that caused it except the free-text `body`.

**`routes/songs.py`** — search (`GET /songs/search`), song detail (`GET /songs/<id>`), rating (`POST /songs/<id>/rate`), and listening (`POST /songs/<id>/listen`). Every handler just parses/validates the request and delegates to a service function, catching `ValueError` and turning it into a 4xx JSON error.

**`routes/playlists.py`** — create playlist, get playlist detail, get playlist's songs, add a song to a playlist. Same thin-wrapper pattern. Notably, adding a song delegates to `notification_service.add_to_playlist`, not `playlist_service` — the service that actually mutates `playlist.songs` also owns the "notify the original sharer" side effect.

**`routes/users.py`** — get user profile, get streak, list notifications (with `unread_only` filter), mark a notification read. `playlist_service.get_user_playlists` exists but has no route exposing it — it's currently dead code from the API's perspective.

**`routes/feed.py`** — "friends listening now" and "activity feed" endpoints. Both are pure reads over `services/feed_service.py`; there's no route that writes to a feed, because there's nothing to write — feeds are computed live from `ListeningEvent`.

**`services/streak_service.py`** — `record_listening_event` creates a `ListeningEvent` and calls `update_listening_streak`, then commits both together. `update_listening_streak` holds the actual streak math: first listen ever → streak = 1; already listened today → no-op; listened exactly yesterday → increment; bigger gap → reset to 1. `get_streak` is a plain lookup.

**`services/feed_service.py`** — `get_friends_listening_now` filters a user's friends' `ListeningEvent`s to the last 24 hours and dedupes to one (most recent) entry per friend. `get_activity_feed` is the same idea without the time filter or dedup — just the most recent N events across all friends. Both do a per-event lookup of `User` and `Song` (N+1 query pattern) rather than joining.

**`services/search_service.py`** — `search_songs` does a case-insensitive `ILIKE` match on title/artist, outer-joined to `song_tags` so tagged songs' tag names can be serialized. `get_song` is a plain lookup by ID.

**`services/playlist_service.py`** — `create_playlist`, `get_playlist` (metadata only), `get_playlist_songs` (songs joined through `playlist_entries`, ordered by `position`), and `get_user_playlists` (all playlists a user created).

**`services/notification_service.py`** — `create_notification` is the generic writer. `add_to_playlist` appends a song to a playlist (if not already present) and then calls `create_notification` to tell the song's original sharer, unless they added it themselves. `rate_song` creates or updates a `Rating` row (upsert via unique constraint lookup) but does **not** call `create_notification` anywhere. `get_notifications` and `mark_as_read` round out CRUD on notifications.

**`seed_data.py`** — drops and recreates all tables, then seeds 5 users with friendships, 25 songs (a deliberate mix of 0/1/3+ tags), listening events split into "recent" (should show in listening-now) and "older" (1–14 days back, should not), pre-set streak values, 3 playlists of 5–7 songs each, and one working "song added to playlist" notification as a reference example.

**`tests/`** — `test_streaks.py`, `test_search.py`, `test_playlists.py` (haven't dug into these in depth yet — flagged for the debugging phase).

## Data flow traces

**A user rates a song:** `POST /songs/<song_id>/rate` → `routes/songs.py: rate()` reads `user_id`/`score` from the JSON body, validates presence → calls `notification_service.rate_song(user_id, song_id, score)` → that function validates the score range (1–5), looks up the song and user, checks for an existing `Rating` for that (user, song) pair and either updates its `score` or inserts a new `Rating`, then commits. Nothing downstream is notified — the function returns the `Rating` object, and the route serializes it with `201`. This is Issue #4: contrast with `add_to_playlist` below, which *does* notify.

**A user adds a song to a playlist:** `POST /playlists/<id>/songs` → `routes/playlists.py: add_song()` reads `song_id`/`added_by` → calls `notification_service.add_to_playlist(playlist_id, song_id, added_by)` → that function looks up the song, adder, and playlist; appends the song to `playlist.songs` if not already present and commits; then, if the adder isn't the song's original sharer, calls `create_notification()` to write a `song_added_to_playlist` notification for `song.shared_by`. So "add to playlist" and "rate" hit structurally similar code paths, but only one of the two calls `create_notification` — that asymmetry is exactly what Issue #4 is about.

**A song reaches a friend's feed (read-only, no explicit "push"):** `POST /songs/<id>/listen` → `routes/songs.py: listen()` → `streak_service.record_listening_event()` writes a `ListeningEvent` row and updates the user's streak, then commits. Later, a friend hits `GET /feed/<user_id>/listening-now` or `/activity` → `routes/feed.py` → `feed_service.get_friends_listening_now()` / `get_activity_feed()`, which query that same `ListeningEvent` table filtered by friend IDs. The two paths are connected only through shared table state, not a direct function call — there's no fan-out step comparable to the notification created in `add_to_playlist`.

## Patterns noticed

Every route is a thin wrapper: parse/validate the request, call exactly one service function, translate a `ValueError` into a 4xx response, otherwise serialize the result. No business logic lives in `routes/`.

Almost every service function starts with the same guard-clause shape: look up the relevant row(s) by ID, raise `ValueError(f"... not found")` if missing, then do the actual work — that's the idiom routes rely on to produce 404s.

Side effects (like notifications) are attached inline inside the service function that performs the primary action, rather than through any shared event/hook system — which is presumably why it's easy for one action (`add_to_playlist`) to remember the side effect while a structurally similar one (`rate_song`) forgets it (Issue #4).

There's no dedicated "feed" or "notification-trigger" table — feeds are computed on read from `ListeningEvent`, and notifications are only ever created explicitly inside a service function, never derived automatically from other tables.

`models.py` mixes plain many-to-many association tables (`song_tags`, `friendships` — no extra columns) with one "rich" association table (`playlist_entries` — carries `position`, `added_by`, `added_at`). That extra state is what lets `playlist_service.get_playlist_songs` preserve explicit ordering instead of relying on insertion order.

## Root Cause Analysis

### Issue #1 — My listening streak keeps resetting

**Affected file:** `services/streak_service.py`

**How I reproduced it:** Called `update_listening_streak(user, now)` directly with fixed datetimes rather than through the live `/listen` endpoint, since `record_listening_event` sources `now` from the real system clock internally and the bug only triggers on a specific weekday. Used a fresh `User` with no prior `last_listened_at`, then two calls: `update_listening_streak(user, saturday)` with `saturday = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)` (first listen ever, sets the streak to 1), followed by `update_listening_streak(user, sunday)` with `sunday = datetime(2024, 6, 16, 12, 0, 0, tzinfo=timezone.utc)`, exactly one calendar day later. `tests/test_streaks.py` already had a test encoding this exact scenario, `test_streak_increments_on_sunday`, which was failing before the fix.

Observed: `user.listening_streak` was `1` after the second call instead of `2`, matching kenji's report of a 12-day streak collapsing to 1 after a Sunday listen. Reproducing this through the live API instead would require either waiting for a real Saturday→Sunday pair or freezing the system clock, since `record_listening_event` doesn't accept `now` as an argument.

**How I found the root cause:** Started at `routes/songs.py`'s `listen()` handler and followed its single call into `streak_service.record_listening_event()`, which calls `update_listening_streak()` — that's where the actual streak math lives, so that's where I focused. Reading the function's docstring against its body is what narrowed it down: the docstring states the rule is purely "consecutive calendar days increment, gaps reset," with no day-of-week exception, but the `elif` branch checked `days_since_last == 1 and today.weekday() != 6`. That extra clause had no basis in the stated rules, and running `test_streak_increments_on_sunday` (Saturday → Sunday) confirmed it was exactly that clause causing the failure, not some other part of the function.

**The root cause:** The increment condition was `days_since_last == 1 and today.weekday() != 6`, where `today` is the date of the *new* listen. Python's `weekday()` returns `6` for Sunday, so any listen landing on a Sunday with a one-day gap since the last listen made `today.weekday() != 6` evaluate to `False` — sending execution to the `else` branch and resetting the streak to `1`, even though the day gap was exactly the "consecutive day" case the function is supposed to handle. Every other weekday satisfied the condition correctly; Sunday alone was silently carved out with no justification in the function's own stated rules.

**My fix and side-effect check:** Removed the `and today.weekday() != 6` clause so the branch reads `elif days_since_last == 1:` — the increment now fires purely on the one-day gap, matching the documented rule with no day-of-week exception. Checked for side effects by grepping the repo for `weekday`/`isoweekday`, which turned up no usage outside this function, its test file, and this doc, so the change is fully contained. Manually traced all five cases in `tests/test_streaks.py` against the new code (new user, consecutive day, same-day no-op, skipped-day reset, and the Saturday→Sunday case) to confirm none regressed and the previously-failing case now passes — then confirmed by actually running the suite, which passed.

### Issue #4 — I got notified when a friend added my song to a playlist but not when they rated it

**Affected file:** `services/notification_service.py`

**How I reproduced it:** Unlike Issue #1, this one has no time dependency, so it reproduces deterministically through the live API on any day, using the seeded data as-is.

Inputs: two distinct users where the rater is not the song's original sharer — nova (`shared_by` on "Midnight Drive") and darius as the rater. This distinction matters because `rate_song()` has no self-rating guard, but a self-rating shouldn't produce a notification anyway (mirroring the `song.shared_by != added_by_user_id` check in `add_to_playlist()`), so the rater has to be someone other than the sharer to cleanly expose the bug rather than a false negative.

Sequence: (1) baseline `GET /users/<nova_id>/notifications` — seed data pre-populates exactly one notification for nova, a `song_added_to_playlist` entry, unrelated to this song. (2) `POST /songs/<midnight_drive_id>/rate` with `{"user_id": "<darius_id>", "score": 5}` — returns `201` with the serialized `Rating`, confirming the rating itself saved correctly. (3) re-check `GET /users/<nova_id>/notifications` — the list is identical to step 1, no new entry, same count. (4) as a contrast check, `POST /playlists/<some_playlist_id>/songs` with `{"song_id": "<midnight_drive_id>", "added_by": "<darius_id>"}`, then a third `GET /users/<nova_id>/notifications` — this time a new `song_added_to_playlist` entry does appear, confirming the asymmetry is real and isolated to the rating path rather than notifications being broken generally.

Observed: rating a song never produces a notification for the song's sharer. Expected: a `song_rated`-style notification, matching the pattern already used for playlist adds.

Root cause, traced: `routes/songs.py: rate()` calls `notification_service.rate_song(user_id, song_id, score)`, which validates the score, looks up the song and user, upserts the `Rating` row, and commits — there is no call to `create_notification()` anywhere in the function body. `add_to_playlist()` performs the analogous lookup/mutate/commit sequence but explicitly calls `create_notification(user_id=song.shared_by, notification_type="song_added_to_playlist", ...)` afterward; `rate_song()` simply has no equivalent line.

This also reproduces at the function level without running the server: build an in-memory app/DB, create two `User`s and a `Song` with `shared_by` set to one of them, call `rate_song(other_user.id, song.id, 5)`, then call `get_notifications(sharer.id)` and confirm the list length is unchanged.

### Issue #5 — The last song in a playlist never shows up

**Affected file:** `services/playlist_service.py`

**How I reproduced it:** No timing dependency here, so this reproduces unconditionally through the live API, and the seed data already happens to set up almost the exact scenario from the report. `seed_data.py` creates a playlist named "Friday Energy" (`created_by` darius) with exactly 7 songs at positions 1–7 — the same playlist name and song count darius describes.

Sequence: (1) seed the DB and start the app. (2) Look up "Friday Energy"'s `playlist_id` directly in the DB (there's no list-all-playlists endpoint exposed to find it another way). (3) `GET /playlists/<friday_energy_id>/songs` and count the results. (4) Cross-check which song is missing against the seed script's insertion order.

Observed: 6 songs returned instead of 7. The missing one is specifically the entry with the highest `position` (the last one inserted for that playlist in `seed_data.py`'s loop), not a random one — confirming it's a highest-position drop, not an arbitrary omission.

This also reproduces at the function level, and there's already a test for it: `tests/test_playlists.py`'s `seed_playlist` fixture inserts 5 songs directly into `playlist_entries` at positions 1–5, and `test_playlist_returns_all_songs` asserts `len(songs) == 5` with a comment already noting `# Bug causes this to return 4`. That test currently fails for the reason below.

Root cause, traced: `get_playlist_songs()` queries `Song` joined through `playlist_entries`, correctly ordered by `position` ascending, then returns `[song.to_dict() for song in songs[:-1]]`. That `[:-1]` slice unconditionally drops the last element of an already-correctly-ordered list — i.e. whichever song has the highest `position`, which is always the most recently added one. This matches "the missing one is always whatever was added most recently" and "adding a new song frees the previous one" exactly: each new addition shifts which entry holds the highest position, and the slice always discards whatever that current highest is.

**Note for follow-up, not part of this bug's root cause:** I did not verify the second half of darius's report (adding a song via `POST /playlists/<id>/songs`) end-to-end. That path goes through `add_to_playlist()` → `playlist.songs.append(song)`, and `playlist_entries` declares `position` and `added_by` as `nullable=False` with no defaults — a plain `relationship(secondary=...)` append only populates the two foreign-key columns automatically, not those extra ones. This could mean the add itself fails for a reason unrelated to Issue #5, and is worth verifying separately before relying on it during the fix/test phase.