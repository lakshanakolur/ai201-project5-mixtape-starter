"""
tests/test_notifications.py — Mixtape

Tests for notification creation logic (Issue #4: rating a song should
notify the song's sharer, the same way adding it to a playlist does).
"""

import pytest
from app import create_app, db
from models import User, Song
from services.notification_service import rate_song, get_notifications


@pytest.fixture
def app():
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


@pytest.fixture
def sharer_and_song(app):
    """A user who shared a song, plus a separate user who can rate it."""
    with app.app_context():
        sharer = User(username="sharer", email="sharer@example.com")
        rater = User(username="rater", email="rater@example.com")
        db.session.add_all([sharer, rater])
        db.session.flush()

        song = Song(title="Test Song", artist="Test Artist", shared_by=sharer.id)
        db.session.add(song)
        db.session.commit()

        yield {"sharer": sharer, "rater": rater, "song": song}


def test_rating_a_song_notifies_the_sharer(app, sharer_and_song):
    """Rating someone else's shared song should create a notification for the sharer."""
    with app.app_context():
        sharer = sharer_and_song["sharer"]
        rater = sharer_and_song["rater"]
        song = sharer_and_song["song"]

        rate_song(rater.id, song.id, 5)

        notifications = get_notifications(sharer.id)
        assert len(notifications) == 1
        assert notifications[0]["type"] == "song_rated"
        assert rater.username in notifications[0]["body"]
        assert song.title in notifications[0]["body"]


def test_rating_your_own_song_does_not_notify_yourself(app, sharer_and_song):
    """Rating your own shared song should not create a self-notification."""
    with app.app_context():
        sharer = sharer_and_song["sharer"]
        song = sharer_and_song["song"]

        rate_song(sharer.id, song.id, 4)

        notifications = get_notifications(sharer.id)
        assert notifications == []


def test_updating_a_rating_still_notifies(app, sharer_and_song):
    """
    Updating an existing rating fires another notification too.

    This matches add_to_playlist's existing behavior, which also notifies
    unconditionally rather than only on the first call — kept consistent
    rather than inventing new "only notify once" logic that doesn't exist
    elsewhere in this file.
    """
    with app.app_context():
        sharer = sharer_and_song["sharer"]
        rater = sharer_and_song["rater"]
        song = sharer_and_song["song"]

        rate_song(rater.id, song.id, 3)
        rate_song(rater.id, song.id, 5)

        notifications = get_notifications(sharer.id)
        assert len(notifications) == 2
