"""Regression tests for MapReader block-cache eviction (unbounded-growth leak).

_land_cache and _statics_cache were plain dicts populated with ``cache[key]=...``
writes inside the A* hot path (_load_land_block / _load_statics_block) and read
with ``key in cache`` — there was no eviction anywhere. As the agent roams it
visits hundreds of thousands of distinct 8×8 blocks over a soak, so both dicts
grew without bound. They are now LRU-bounded to MAP_BLOCK_CACHE_MAX: the least-
recently-used block is evicted past the cap, and a recently-used block stays hot.
"""

from __future__ import annotations

import pytest

from anima.map import MAP_BLOCK_CACHE_MAX, MapReader


class _NullUop:
    """Stand-in UopReader: every chunk lookup misses (graphic-0 fallback path)."""

    def get_by_pattern(self, pattern: str, idx: int):  # noqa: ARG002
        return None


@pytest.fixture
def reader() -> MapReader:
    r = MapReader(resource_dir="/nonexistent", data_dir="/nonexistent")
    # Bypass real map/statics file I/O: the chunk-missing branch still exercises
    # the real cache-write path (_cache_put), which is what we are testing.
    r._uop = _NullUop()  # type: ignore[assignment]
    r._staidx_data = b""  # empty staidx -> _load_statics_block hits the
    r._statics_data = b""  # "idx_offset + 12 > len(staidx)" empty-block path
    return r


def test_land_cache_stays_bounded_and_keeps_recent(reader: MapReader) -> None:
    # Pin a "recently used" block, then flood the cache with far more than the
    # cap of *distinct* blocks. bx is bounded by MAP_WIDTH//8 (=896), so vary by
    # to guarantee distinct (bx<<16)|by keys without colliding.
    hot_bx, hot_by = 0, 0
    reader._load_land_block(hot_bx, hot_by)
    assert ((hot_bx << 16) | hot_by) in reader._land_cache

    flood = MAP_BLOCK_CACHE_MAX * 3
    for by in range(1, flood + 1):
        reader._load_land_block(0, by)
        # Touch the hot block on every iteration so it remains most-recent and
        # is never the eviction victim.
        reader._load_land_block(hot_bx, hot_by)
        # Invariant must hold at *every* step, not just at the end.
        assert len(reader._land_cache) <= MAP_BLOCK_CACHE_MAX

    # Cache is capped...
    assert len(reader._land_cache) <= MAP_BLOCK_CACHE_MAX
    # ...far below the number of distinct blocks visited...
    assert len(reader._land_cache) < flood
    # ...and the recently-used block survived the flood.
    assert ((hot_bx << 16) | hot_by) in reader._land_cache


def test_statics_cache_stays_bounded_and_keeps_recent(reader: MapReader) -> None:
    hot_bx, hot_by = 0, 0
    reader._load_statics_block(hot_bx, hot_by)
    assert ((hot_bx << 16) | hot_by) in reader._statics_cache

    flood = MAP_BLOCK_CACHE_MAX * 3
    for by in range(1, flood + 1):
        reader._load_statics_block(0, by)
        reader._load_statics_block(hot_bx, hot_by)
        assert len(reader._statics_cache) <= MAP_BLOCK_CACHE_MAX

    assert len(reader._statics_cache) <= MAP_BLOCK_CACHE_MAX
    assert len(reader._statics_cache) < flood
    assert ((hot_bx << 16) | hot_by) in reader._statics_cache


def test_lru_evicts_least_recently_used_first(reader: MapReader) -> None:
    # Fill exactly to the cap, then insert one more: the oldest (by=1) must be
    # the one evicted, while the most-recent stays.
    for by in range(1, MAP_BLOCK_CACHE_MAX + 1):
        reader._load_land_block(0, by)
    assert len(reader._land_cache) == MAP_BLOCK_CACHE_MAX
    oldest_key = (0 << 16) | 1
    assert oldest_key in reader._land_cache

    # One more distinct block tips over the cap.
    reader._load_land_block(0, MAP_BLOCK_CACHE_MAX + 1)
    assert len(reader._land_cache) == MAP_BLOCK_CACHE_MAX
    assert oldest_key not in reader._land_cache  # least-recently-used evicted
    assert ((0 << 16) | (MAP_BLOCK_CACHE_MAX + 1)) in reader._land_cache


def test_cache_hit_refreshes_recency(reader: MapReader) -> None:
    # A read should mark the block most-recently-used so it is not evicted next.
    for by in range(1, MAP_BLOCK_CACHE_MAX + 1):
        reader._load_land_block(0, by)
    oldest_key = (0 << 16) | 1

    # Re-read the oldest block: this is now a cache *hit* and must move it to the
    # most-recent end.
    reader._load_land_block(0, 1)
    # Insert a fresh block -> the victim is now the *new* oldest (by=2), not by=1.
    reader._load_land_block(0, MAP_BLOCK_CACHE_MAX + 1)
    assert oldest_key in reader._land_cache
    assert ((0 << 16) | 2) not in reader._land_cache
