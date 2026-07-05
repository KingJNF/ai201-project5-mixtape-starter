# Project 5: Mixtape Bug Hunt — Submission

## AI Usage

I used an AI assistant (Claude via You.com) for codebase navigation and diagnosis
verification, not blind code generation. Specific uses:

1. **File-role summaries.** I pasted each service file and asked what the module was
   responsible for and what each function did, to build my codebase map faster. I
   verified every summary against the code before recording it.

2. **Comparing parallel code paths (Issue #4).** I gave the AI `add_to_playlist()` and
   `rate_song()` and asked for the structural difference. It correctly identified that
   `add_to_playlist` ends with a `create_notification()` call and `rate_song` does not.
   I confirmed by reading both functions myself.

3. **datetime.weekday() semantics (Issue #1).** I asked what `datetime.weekday()` returns
   for Sunday (6), confirming that `today.weekday() != 6` singled out Sunday.

**Where I course-corrected the AI:** For Issue #3, the AI first suggested adding
`.distinct()`. That masks the symptom but leaves the pointless `outerjoin` in place. I
read the code and saw the join contributes nothing to the filter (which only touches
title/artist; tags load separately in `to_dict()`), so I removed the join instead —
fixing the root cause. For Issue #2, the AI initially treated the 24-hour window as
intentional; I pointed out the feature is called "Listening Now," which reframes the 24h
constant as the defect.

The AI initially diagnosed Issue #3 as an outerjoin duplication bug; when I reproduced it, the song appeared only once because SQLAlchemy's legacy Query de-duplicates entities. I dropped #3 rather than document a bug I couldn't reproduce.

---

## Codebase Map
*(Written during Milestone 1, before any bug work.)*

### Main files and their roles
- **app.py** — Flask app factory (`create_app`) and SQLAlchemy `db` setup.
- **models.py** — 6 entities (`User`, `Song`, `ListeningEvent`, `Rating`, `Playlist`,
  `Notification`) and 3 association tables (`friendships`, `song_tags`,
  `playlist_entries`). `playlist_entries` carries an explicit **`position`** integer, so
  playlist songs have a real order. `User` holds `listening_streak` + `last_listened_at`.
  `Rating` has a unique constraint on `(user_id, song_id)`.
- **routes/** — thin controllers: parse request → call service → return JSON. No logic.
  - songs.py (`/search`, `/<id>`, `/<id>/rate`, `/<id>/listen`), users.py
    (`/<id>/streak`, `/<id>/notifications`), playlists.py, feed.py.
- **services/** — all business logic; every bug lives here (streak, feed, search,
  notification, playlist).
- **tests/** — test_streaks.py, test_search.py, test_playlists.py (plus my new
  test_notifications.py).

### Data flow: rating a song and its notification
`POST /songs/<id>/rate` (routes/songs.py) parses `user_id` + `score` and calls
`notification_service.rate_song()`, which validates the score, looks up song + rater,
upserts a `Rating`, and commits. It is intended to then create a `Notification` for
`song.shared_by`, mirroring `add_to_playlist()` (the missing step is Issue #4).

### Patterns
Routes always delegate to a service; timestamps are UTC-aware
(`datetime.now(timezone.utc)`); notifications target `song.shared_by`, guarded by
`if song.shared_by != actor_id` to avoid self-notification.

---

## Root Cause Analysis Entries

### Issue #1 — My listening streak keeps resetting
**How I reproduced it:** Running `pytest tests/test_streaks.py` showed the shipped test
`test_streak_increments_on_sunday` failing with `assert 1 == 2`. I then reproduced it
live: today is Sunday, so `POST /songs/<id>/listen` for a user who listened Saturday
dropped the streak to 1 instead of incrementing. On a non-Sunday the increment worked.

**How I found the root cause:** The symptom showed on `GET /users/<id>/streak`, but
`get_streak()` only reads the stored value, so the corruption had to occur on the write
path. I traced `POST /songs/<id>/listen` → `record_listening_event()` →
`update_listening_streak()` in streak_service.py. The branch
`elif days_since_last == 1 and today.weekday() != 6:` stood out — the docstring's rules
never mention weekdays. A REPL confirmed `datetime.weekday()` returns 6 for Sunday.

**The root cause:** `datetime.weekday()` returns 6 for Sunday. The streak only increments
when `days_since_last == 1 AND today.weekday() != 6`. On any Sunday, a user who listened
yesterday fails the `elif`, falls into `else`, and is reset to 1. The weekday clause has
no basis in the documented rules.

**Fix and side-effect check:** Removed `and today.weekday() != 6`. The pre-existing
`test_streak_increments_on_sunday` now passes, and the other four streak tests (new user,
consecutive day, same-day no-op, skipped-day reset) still pass — confirming the other
branches are intact.

### Issue #3 — 

### Issue #4 — Notified on playlist-add but not on rating
**How I reproduced it:** Shared a song as user A; rated it as user B via
`POST /songs/<id>/rate`; A received no notification, though the rating saved (201).

**How I found the root cause:** `POST /songs/<id>/rate` → `rate_song()` in
notification_service.py. Reading it beside its sibling `add_to_playlist()` in the same
file: `add_to_playlist()` ends with `if song.shared_by != added_by_user_id:` +
`create_notification(...)`; `rate_song()` commits and returns with no such call.

**The root cause:** An architectural omission, not a typo. `rate_song()` is missing the
entire notification-dispatch step its sibling performs. Rating logic and route are
correct; the notification was never wired in.

**Fix and side-effect check:** Added the parallel dispatch after commit, guarded by
`if song.shared_by != user_id:`, type `"song_rated"`. Verified via new tests: rating a
friend's song creates exactly one notification; self-rating creates none; the
existing-rating update path still works without violating the `(user_id, song_id)` unique
constraint.

### Issue #5 — The last song in a playlist never shows up  *(stretch)*
**How I reproduced it:** Added 3 songs to a playlist; `GET /playlists/<id>/songs`
returned 2. A single-song playlist returned `[]`.

**How I found the root cause:** `GET /playlists/<id>/songs` → `get_playlist_songs()` in
playlist_service.py. The query correctly orders by `position` ascending, but the return
is `[song.to_dict() for song in songs[:-1]]`.

**The root cause:** The slice `[:-1]` drops the last ordered element every time (and
returns `[]` for one-song playlists), contradicting the docstring's promise to return all
songs.

**Fix and side-effect check:** Changed `songs[:-1]` to `songs`. Verified a 3-song playlist
returns 3, a 1-song playlist returns 1, an empty playlist returns `[]`, and `position`
ordering is preserved (the `order_by(asc(position))` is untouched).

### Issue #2 — Friends Listening Now shows people from yesterday  *(stretch)*
**How I reproduced it:** Seeded a friend with a `ListeningEvent` ~20 hours old; the
Friends-Listening-Now feed still listed them.

**How I found the root cause:** `get_friends_listening_now()` in feed_service.py uses
`RECENT_THRESHOLD = timedelta(hours=24)` and filters `listened_at >= now - RECENT_THRESHOLD`.
A 24-hour rolling window puts the cutoff at the same clock time yesterday.

**The root cause:** For a feature called "Listening **Now**," a 24-hour window is the
defect: anyone who listened in the past day satisfies the cutoff and is shown as currently
listening. The constant value, not the query mechanics, is wrong.

**Fix and side-effect check:** Changed `RECENT_THRESHOLD` to `timedelta(minutes=15)` (a
"currently listening" window; a product judgment). Verified a friend from 2 minutes ago
still appears, one from 20 hours ago no longer does, and `get_activity_feed()` — which
doesn't use `RECENT_THRESHOLD` — is unaffected.

---

## Regression Test
I added `tests/test_notifications.py::test_rating_a_song_notifies_the_sharer` for Issue #4.
It seeds a sharer, a rater, and one shared song, calls `rate_song()`, and asserts exactly
one `song_rated` notification exists for the sharer. Against the buggy `rate_song()` (which
saved the rating but never called `create_notification()`) the count is 0 and it fails;
after the fix it passes. A companion test verifies the self-rating guard. Separately, the
repo already ships `tests/test_streaks.py::test_streak_increments_on_sunday`, which fails
against the buggy streak code — I used it as my primary reproduction signal for Issue #1.

## Commit History
*(Screenshot of `git log --oneline` on bugfix/mixtape goes here.)*