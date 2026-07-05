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

**Where I course-corrected the AI (important):** The AI initially diagnosed Issue #3
("same song shows up twice in search") as an `outerjoin(song_tags)` duplication bug and
proposed a fix. When I reproduced it in `flask shell` before touching code, the multi-tag
song appeared **only once**, not multiple times. The reason: the code uses SQLAlchemy's
legacy `db.session.query(Song)` API, which automatically de-duplicates entity results by
primary key, so the join's extra rows collapse back to one Song. The shipped test
`tests/test_search.py::test_search_no_duplicates_multi_tag_song` also passes against the
unmodified code, confirming there is no user-visible duplicate. Because the project says
to switch bugs if one can't be reproduced after a genuine attempt, I dropped Issue #3
rather than document a bug I couldn't reproduce, and fixed Issue #2 instead. This is a
direct example of verifying (and overriding) the AI rather than trusting its first guess.

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
`song.shared_by`, mirroring `add_to_playlist()`.

### Patterns
Routes always delegate to a service; timestamps are UTC-aware
(`datetime.now(timezone.utc)`); notifications target `song.shared_by`, guarded by
`if song.shared_by != actor_id` to avoid self-notification.

---

## Root Cause Analysis Entries

### Issue #1 — My listening streak keeps resetting
**How I reproduced it:** Running `python -m pytest tests/test_streaks.py` showed the
shipped test `test_streak_increments_on_sunday` failing with `assert 1 == 2`. Today is a
Sunday, so I also reproduced it live in `flask shell`: a user who listened yesterday with
a streak of 5, updated with a Sunday `now`, dropped to 1 instead of climbing to 6.

**How I found the root cause:** The symptom showed on `GET /users/<id>/streak`, but
`get_streak()` only reads the stored value, so corruption had to occur on the write path.
I traced `POST /songs/<id>/listen` → `record_listening_event()` →
`update_listening_streak()` in streak_service.py. The branch
`elif days_since_last == 1 and today.weekday() != 6:` stood out — the docstring's rules
never mention weekdays. A REPL confirmed `datetime.weekday()` returns 6 for Sunday.

**The root cause:** `datetime.weekday()` returns 6 for Sunday. The streak only increments
when `days_since_last == 1 AND today.weekday() != 6`. On any Sunday, a user who listened
yesterday fails the `elif`, falls into `else`, and is reset to 1. The correct behavior
requires only three cases (same day = no change, next day = increment, gap > 1 = reset);
the weekday clause has no basis in the documented rules and is a pure defect.

**Fix and side-effect check:** Removed `and today.weekday() != 6`, leaving
`elif days_since_last == 1:`. The pre-existing `test_streak_increments_on_sunday` now
passes, and the other four streak tests (new user, consecutive day, same-day no-op,
skipped-day reset) still pass — confirming the new-user, mid-week increment, no-op, and
reset branches on both sides of the boundary are intact.

### Issue #5 — The last song in a playlist never shows up
**How I reproduced it:** In `flask shell`, `get_playlist_songs()` for the playlist "Late
Night Vibes" returned 6 songs, but the join table showed the playlist actually contains 7
(`Songs returned: 6` vs `Actual: 7`). The returned titles stopped one short of the full
list.

**How I found the root cause:** `GET /playlists/<id>/songs` → `get_playlist_songs()` in
playlist_service.py. The query correctly orders all songs by `position` ascending, but the
return statement is `return [song.to_dict() for song in songs[:-1]]`.

**The root cause:** The slice `[:-1]` excludes the last element of the ordered list every
time, so the final song is always dropped (and a single-song playlist returns `[]`). This
contradicts the function's own docstring, which promises to return all songs.

**Fix and side-effect check:** Changed `songs[:-1]` to `songs`. Verified in `flask shell`
that the count now matches (7 == 7) with the previously missing title present. Ordering is
preserved because `order_by(asc(position))` runs on the query before the return. The
shipped tests `test_playlist_returns_all_songs`, `test_playlist_returns_songs_in_order`,
and `test_empty_playlist_returns_empty_list` all pass after the fix — the first would have
failed against the buggy slice.

### Issue #4 — Notified when a friend added my song to a playlist but not when they rated it
**How I reproduced it:** In `flask shell`, I counted `song_rated` notifications for a
song's sharer, then had a different user rate the song via `rate_song()`. The count stayed
at 0 (`BEFORE: 0 | AFTER: 0`), even though the `Rating` was saved — so only the
notification was missing.

**How I found the root cause:** `POST /songs/<id>/rate` → `rate_song()` in
notification_service.py. Reading it beside its sibling `add_to_playlist()` in the same
file: `add_to_playlist()` ends with `if song.shared_by != added_by_user_id:` +
`create_notification(...)`; `rate_song()` commits and returns with no such call.

**The root cause:** An architectural omission, not a typo. `rate_song()` is missing the
entire notification-dispatch step its sibling `add_to_playlist()` performs. The rating
logic and route are correct; the notification was never wired in. The correct behavior
requires the same sharer-notification the playlist path already implements.

**Fix and side-effect check:** Added the parallel dispatch after the commit, guarded by
`if song.shared_by != user_id:`, with type `"song_rated"`. Verified in `flask shell` that
a different user rating the song produces exactly one notification
(`BEFORE: 0 | AFTER: 1`, body "darius rated your song 'Midnight Drive' 5 stars."), and
that a user rating their OWN song produces none (`Self-rate BEFORE: 1 | AFTER: 1`) —
confirming the self-notification guard works. My regression test in
`tests/test_notifications.py` locks in both behaviors.

### Issue #2 — Friends Listening Now shows people from yesterday  *(stretch — 4th bug)*
**How I reproduced it:** In `flask shell`, I seeded a `ListeningEvent` for a friend dated
20 hours ago, then called `get_friends_listening_now()`. The friend appeared
(`Friend appears in 'Listening Now'? True`), despite having listened the previous day.

**How I found the root cause:** `get_friends_listening_now()` in feed_service.py uses
`RECENT_THRESHOLD = timedelta(hours=24)` and filters `listened_at >= now - RECENT_THRESHOLD`.
A 24-hour rolling window puts the cutoff at the same clock time yesterday.

**The root cause:** For a feature named "Listening **Now**," a 24-hour window is the
defect: anyone who listened at any point in the previous day satisfies
`listened_at >= cutoff` and is shown as currently listening. "Now" requires a window
measured in minutes, not a full day; the constant value, not the query mechanics, is wrong.

**Fix and side-effect check:** Changed `RECENT_THRESHOLD` to `timedelta(minutes=15)`, a
window that reflects "currently listening" (a few songs). This is a product judgment; the
essential point is that 24h is wrong for "Now." Verified in `flask shell` that a listen
from 20 hours ago no longer appears (False), a listen from 2 minutes ago does appear
(True), and that `get_activity_feed()` — which does not use `RECENT_THRESHOLD` — still
returns events, confirming the other feed code path was unaffected.

---

## Regression Test
I added `tests/test_notifications.py`, containing
`test_rating_a_song_notifies_the_sharer` for Issue #4. It seeds a sharer, a rater, and one
shared song, calls `rate_song()`, and asserts exactly one `song_rated` notification exists
for the sharer with the expected body. Against the buggy `rate_song()` — which saved the
rating but never called `create_notification()` — the count is 0 and the assertion fails;
against the fix it passes. A companion test, `test_rating_your_own_song_creates_no_notification`,
verifies the self-rating guard. The full suite is 15 passing tests
(`python -m pytest tests/`).

## Commit History
[git log --oneline on bugfix/mixtape](docs/git-log.png)