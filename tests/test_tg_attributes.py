"""Тесты get_audio_attributes (dedup.tg) на фейковых сообщениях."""

from fakes import make_message

from dedup.tg import get_audio_attributes


def test_none_message_returns_none():
    assert get_audio_attributes(None) is None


def test_empty_message_returns_none():
    assert get_audio_attributes(make_message(1, empty=True)) is None


def test_service_message_returns_none():
    assert get_audio_attributes(make_message(1, service=True)) is None


def test_message_without_media_returns_none():
    assert get_audio_attributes(make_message(1, kind="none")) is None


def test_audio_message_maps_all_fields():
    message = make_message(
        1,
        file_name="song.mp3",
        file_size=12345,
        duration=200,
        performer="Artist",
        title="Song",
        uid="AgAD123",
    )
    meta = get_audio_attributes(message)
    assert meta is not None
    assert meta.file_unique_id == "AgAD123"
    assert meta.file_name == "song.mp3"
    assert meta.file_size == 12345
    assert meta.duration == 200
    assert meta.performer == "Artist"
    assert meta.title == "Song"


def test_audio_with_none_duration_becomes_zero():
    message = make_message(1, duration=100)
    message.audio.duration = None
    meta = get_audio_attributes(message)
    assert meta is not None
    assert meta.duration == 0


def test_audio_document_with_audio_mime():
    message = make_message(1, kind="document", file_name="track.m4a", mime_type="audio/mp4")
    meta = get_audio_attributes(message)
    assert meta is not None
    assert meta.duration == 0
    assert meta.performer is None
    assert meta.title is None
    assert meta.file_name == "track.m4a"


def test_document_with_non_audio_mime_returns_none():
    message = make_message(1, kind="document", mime_type="image/png")
    assert get_audio_attributes(message) is None


def test_document_without_mime_returns_none():
    message = make_message(1, kind="document", mime_type=None)
    message.document.mime_type = None
    assert get_audio_attributes(message) is None
