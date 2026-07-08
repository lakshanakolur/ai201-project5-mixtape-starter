# Mixtape — Codebase Map

## AI Usage

I used an AI assistant throughout this project, mostly for codebase orientation and as a second pair of eyes during debugging — not for blind bug-finding or code generation I didn't verify myself.

**Milestone 1 (orientation):** I gave the assistant each service file and asked what it was responsible for and what its main functions did — that's what produced the file-by-file breakdown in "Main files" below. I also asked it to trace full data flows across files (e.g. "how does a song reach a friend's feed"), which is where I first noticed the read-only nature of the feed and the structural similarity between `add_to_playlist` and `rate_song` that turned into Issue #4 later. I treated these summaries as a starting map, not ground truth — I still opened and read every file myself before trusting a summary.

**Milestone 2 (reproduction):** For Issue #1, I asked it to walk through exactly what app *state* was needed to hit the Sunday-specific code path, since I wasn't sure how to reproduce a day-of-week bug without literally waiting for a Sunday. It suggested calling `update_listening_streak()` directly with fixed datetimes instead of relying on the real clock, and pointed out that `tests/test_streaks.py` already had a test encoding that exact scenario. That was useful specifically because I'd already narrowed the problem down first — I wouldn't have thought to check whether an existing test already modeled the bug.

**Milestone 3 (diagnosis and fixes):** For each bug, I had the assistant read the suspicious function and explain what could make it return the wrong value, then I checked that explanation against the actual code myself before accepting it. For Issue #1, its first pass at explaining the Sunday condition was directionally right but vague — it only tightened up once I gave it the actual bug report text. For Issue #4, I gave it `add_to_playlist` and `rate_song` side by side and asked what was structurally different between them, which is what cleanly surfaced the missing `create_notification()` call. For Issue #5, it read the query and the `[:-1]` slice together and connected the slice to the seeded "Friday Energy" playlist (7 songs), which is what made me confident the reproduction and root cause actually lined up before I touched any code.

**Where I had to verify things myself, or where the AI fell short:**
- The assistant couldn't actually run `pytest` in its own environment (no network access to install Flask/SQLAlchemy there), so for every fix it traced test cases by hand against the new code and I ran the real suite myself and reported back pass/fail each time, rather than trusting hand-traced reasoning alone.
- While investigating Issue #5, it flagged — but was explicit that it hadn't confirmed — that `add_to_playlist()`'s `playlist.songs.append(song)` might not correctly populate `position`/`added_by` on `playlist_entries`, since those columns are `nullable=False` with no defaults on a plain `secondary=` relationship. It recommended checking that separately rather than folding an unverified claim into the Issue #5 fix. I haven't verified this independently yet.
- I made the final call on which 3 of the 5 bugs to fix myself; the assistant gave a recommendation (favoring distinct root-cause categories and existing test coverage) but I checked it against the actual bug reports before deciding, rather than taking the recommendation at face value.

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

**How I reproduced it:** No timing dependency, so this reproduces deterministically through the live API on any day, using seed data as-is. Used two distinct users where the rater isn't the song's sharer — nova (`shared_by` on "Midnight Drive") and darius as the rater, since a self-rating shouldn't notify anyway. Sequence: baseline `GET /users/<nova_id>/notifications`, then `POST /songs/<midnight_drive_id>/rate` with `{"user_id": "<darius_id>", "score": 5}` (returned `201`, confirming the rating itself saved), then re-checked notifications — unchanged. As a contrast check, `POST /playlists/<id>/songs` with the same song and darius as `added_by` did produce a new `song_added_to_playlist` entry, confirming the asymmetry was isolated to the rating path.

**How I found the root cause:** Followed `routes/songs.py`'s `rate()` handler into its one call, `notification_service.rate_song()`, and read the whole function top to bottom. It validates the score, looks up the song and user, upserts the `Rating` row, and commits — nothing else. Comparing it line-by-line against `add_to_playlist()` right above it in the same file is what pinned down the cause: both functions follow the same lookup → mutate → commit shape, but `add_to_playlist()` has one more step afterward — a guarded call to `create_notification()` — that `rate_song()` simply doesn't have. `create_notification()`'s own docstring even lists `'song_rated'` as an example type, confirming the notification was meant to exist here and just never got wired up.

**The root cause:** `rate_song()` never calls `create_notification()` anywhere in its body. `add_to_playlist()`, which performs a structurally identical lookup/mutate/commit sequence, has an explicit follow-up call — `create_notification(user_id=song.shared_by, notification_type="song_added_to_playlist", ...)` — guarded by `song.shared_by != added_by_user_id`. `rate_song()` has no equivalent call at all: the rating gets saved correctly, but no notification is ever created for the sharer, regardless of who does the rating.

**My fix and side-effect check:** Added the missing call at the end of `rate_song()`, right after the rating commits, mirroring `add_to_playlist()`'s exact pattern:

```python
if song.shared_by != user_id:
    create_notification(
        user_id=song.shared_by,
        notification_type="song_rated",
        body=f"{rater.username} rated your song '{song.title}' {score} stars.",
    )
```

The `song.shared_by != user_id` guard prevents self-notifications when someone rates their own shared song, matching the guard already used in `add_to_playlist()`. This fires on every successful rating, including updates to an existing rating, keeping parity with `add_to_playlist()`'s own behavior (which also notifies on every call, not just the first).

Checked side effects: `rate_song()` is only called from `routes/songs.py`'s `rate()` handler and its return value/type is unchanged, so nothing downstream breaks. `Notification.to_dict()` and `get_notifications()` are generic over `notification_type`, with no special-casing that would choke on the new `"song_rated"` value. Wrote `tests/test_notifications.py` (no prior test file existed for this service) covering: rating someone else's song notifies the sharer with the right type and body, rating your own song notifies no one, and updating an existing rating notifies again — all three pass.

### Issue #5 — The last song in a playlist never shows up

**Affected file:** `services/playlist_service.py`

**How I reproduced it:** No timing dependency, so this reproduces unconditionally through the live API. Used seed data as-is: `seed_data.py` creates a playlist named "Friday Energy" (`created_by` darius) with exactly 7 songs at positions 1–7 — the same playlist name and count darius describes. Sequence: seeded the DB, looked up "Friday Energy"'s `playlist_id` directly in the DB (no list-all-playlists endpoint exists), called `GET /playlists/<friday_energy_id>/songs`, and counted the results.

Observed: 6 songs returned instead of 7, and specifically the entry with the highest `position` (the last one inserted in the seed script's loop for that playlist) was the one missing — not a random one.

**How I found the root cause:** Followed `routes/playlists.py`'s `get_songs()` handler into its one call, `playlist_service.get_playlist_songs()`, and read the query and return statement together. The query itself looked correct — joined through `playlist_entries` and ordered by `position` ascending, exactly what's needed to preserve explicit ordering. The return line is what stood out: `[song.to_dict() for song in songs[:-1]]` slices the already-correctly-sorted list before serializing it. `tests/test_playlists.py` already had `test_playlist_returns_all_songs`, with a comment reading `# Bug causes this to return 4` right next to `assert len(songs) == 5` — that comment, plus seeing the slice sitting right next to an otherwise-correct query, is what confirmed this was the exact line, not just a suspicious area.

**The root cause:** `get_playlist_songs()` builds a correctly-ordered list of songs (ascending by `position`), then returns `[song.to_dict() for song in songs[:-1]]`. The `[:-1]` slice drops the last element of that list unconditionally, regardless of how many songs are in the playlist. Since the list is already sorted ascending by `position`, the dropped element is always whichever song has the highest position — i.e. the most recently added one. That's exactly why "the missing one is always whatever was added most recently," and why adding a new song shifts which song holds that highest position and gets cut, "freeing" the previously-hidden one.

**My fix and side-effect check:** Removed the slice, changing the return line to `[song.to_dict() for song in songs]` — the query was already correct, so no other change was needed. Traced this against all cases in `tests/test_playlists.py`: `test_playlist_returns_all_songs` (5 songs) and `test_playlist_returns_songs_in_order` (5 songs) were both actually failing before the fix, not just the count one — with the bug, the ordered-titles list would only have had 4 entries, so it wouldn't have matched the 5-item expected list either. `test_empty_playlist_returns_empty_list` was unaffected on either side of the fix, since `[][:-1]` is still `[]`. To cover the boundary the existing tests missed, I added `test_playlist_with_one_song_returns_that_song`, since `[x][:-1]` returns `[]` — a single-song playlist would have shown up as completely empty before the fix, the sharpest version of this bug. All four tests pass now.

Separately, unrelated to this bug: while reading this area I noticed `add_to_playlist()` in `notification_service.py` imports `get_playlist_songs` but never calls it, and its `playlist.songs.append(song)` call may not correctly populate `position`/`added_by` on `playlist_entries`, since those columns are `nullable=False` with no defaults and a plain `relationship(secondary=...)` append only sets the two foreign-key columns automatically. Worth verifying separately — it's outside this function and not part of Issue #5's fix.