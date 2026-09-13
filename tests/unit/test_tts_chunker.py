"""Streaming text segmentation tests for natural TTS boundaries."""

from bmo.audio.elevenlabs_tts import TextChunker


def test_tts_chunker_flushes_sentence_boundaries_without_token_dribble() -> None:
    chunker = TextChunker(min_chars=12, max_chars=45)
    assert chunker.add("Hello tiny ") == []
    chunks = chunker.add("human. I have robot business to do, obviously. ")
    assert chunks
    assert chunks[0].strip().endswith("human.")
    assert all(len(chunk.strip()) >= 5 for chunk in chunks)
    remainder = chunker.flush()
    assert isinstance(remainder, str)


def test_tts_chunker_forces_word_boundary_at_max_length() -> None:
    chunker = TextChunker(min_chars=10, max_chars=24)
    chunks = chunker.add("one two three four five six seven eight")
    assert chunks
    assert len(chunks[0]) <= 25
