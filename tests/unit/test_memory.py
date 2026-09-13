"""Explicit remember-trigger persistence and local retrieval tests."""

from pathlib import Path

import pytest

from bmo.agent.memory import MemoryStore


@pytest.mark.asyncio
async def test_remember_trigger_persists_human_readable_log(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.sqlite3", tmp_path / "remembered.jsonl")
    await store.initialize()
    fact = store.extract_explicit_memory("Please remember that my soldering iron is in drawer three.")
    assert fact == "my soldering iron is in drawer three"
    item = await store.remember(fact)
    results = await store.search("soldering drawer")
    assert results and results[0].id == item.id
    text = (tmp_path / "remembered.jsonl").read_text(encoding="utf-8")
    assert "drawer three" in text


@pytest.mark.asyncio
async def test_memory_disabled_creates_no_persistent_files(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.sqlite3", tmp_path / "remembered.jsonl", enabled=False)
    await store.initialize()
    assert await store.search("anything") == []
    await store.add_turn("user", "hello")
    assert not (tmp_path / "memory.sqlite3").exists()
    with pytest.raises(RuntimeError, match="disabled"):
        await store.remember("do not write this")
