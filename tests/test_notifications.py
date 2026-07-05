"""
tests/test_notifications.py — Mixtape

Regression tests for notification dispatch (Issue #4).
"""

import pytest
from app import create_app, db
from models import User, Song, Notification
from services.notification_service import rate_song


@pytest.fixture
def app():
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


def test_rating_a_song_notifies_the_sharer(app):
    """Regression test for Issue #4: rating a friend's song must create a
    'song_rated' notification for the original sharer. Against the buggy code,
    rate_song() saved the rating but never called create_notification()."""
    with app.app_context():
        sharer = User(username="sharer", email="sharer@example.com")
        rater = User(username="rater", email="rater@example.com")
        db.session.add_all([sharer, rater])
        db.session.commit()

        song = Song(title="Neon City", artist="The Glows", shared_by=sharer.id)
        db.session.add(song)
        db.session.commit()

        rate_song(user_id=rater.id, song_id=song.id, score=4)

        notes = Notification.query.filter_by(
            user_id=sharer.id, notification_type="song_rated"
        ).all()
        assert len(notes) == 1
        assert "rated your song" in notes[0].body


def test_rating_your_own_song_creates_no_notification(app):
    """A user rating their own shared song should not notify themselves."""
    with app.app_context():
        owner = User(username="owner", email="owner@example.com")
        db.session.add(owner)
        db.session.commit()

        song = Song(title="Solo Track", artist="Owner", shared_by=owner.id)
        db.session.add(song)
        db.session.commit()

        rate_song(user_id=owner.id, song_id=song.id, score=5)

        assert Notification.query.filter_by(user_id=owner.id).all() == []