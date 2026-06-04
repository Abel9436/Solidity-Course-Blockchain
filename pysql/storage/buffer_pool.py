"""
Buffer Pool Manager — LRU Page Cache.

The buffer pool sits between the executor and the disk manager, caching
frequently accessed pages in memory. This is critical for performance
because disk I/O is orders of magnitude slower than memory access.

Design:
- Fixed-size pool of page frames (configurable, default 1024 frames)
- LRU (Least Recently Used) eviction policy with a clock-sweep approximation
- Pin counting: pages in active use cannot be evicted
- Dirty page tracking: only modified pages are written back to disk

The clock-sweep algorithm is used instead of a pure LRU list because:
1. It's O(1) amortized for eviction decisions
2. It avoids the overhead of maintaining a doubly-linked list
3. It's what PostgreSQL uses in production

This implementation also integrates with the WAL (Write-Ahead Logging)
system: dirty pages cannot be flushed to disk until their WAL records
have been flushed first (the WAL protocol).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Optional

from .page import DiskManager, Page, PageType


@dataclass
class FrameHeader:
    """Metadata for a single frame (slot) in the buffer pool.

    Each frame holds one page and tracks its usage state.
    """
    page_id: int = -1           # The page currently in this frame (-1 = empty)
    pin_count: int = 0          # Number of active users (cannot evict if > 0)
    is_dirty: bool = False      # Has the page been modified since loading?
    ref_bit: bool = False       # Clock-sweep reference bit
    lsn: int = 0                # Latest LSN of modifications (for WAL protocol)


class BufferPool:
    """LRU buffer pool with clock-sweep eviction.

    Manages a fixed number of page frames in memory. Pages are identified
    by page_id and can be pinned (preventing eviction), unpinned, and
    marked dirty.

    Thread Safety:
        All operations are protected by a global lock. In a production
        system, you'd use page-level latches, but a global lock is
        sufficient for correctness and simplicity here.

    Usage:
        pool = BufferPool(disk_manager, pool_size=256)
        page = pool.fetch_page(page_id)
        # ... use page ...
        pool.unpin_page(page_id, is_dirty=True)
    """

    def __init__(self, disk_manager: DiskManager, pool_size: int = 1024) -> None:
        self._disk = disk_manager
        self._pool_size = pool_size
        self._lock = threading.Lock()

        # Frame storage
        self._frames: list[Optional[Page]] = [None] * pool_size
        self._headers: list[FrameHeader] = [FrameHeader() for _ in range(pool_size)]

        # Page-to-frame mapping for O(1) lookup
        self._page_table: dict[int, int] = {}  # page_id → frame_index

        # Clock hand for eviction
        self._clock_hand = 0

        # Statistics
        self._hits = 0
        self._misses = 0
        self._evictions = 0

    def fetch_page(self, page_id: int) -> Page:
        """Fetch a page into the buffer pool and pin it.

        If the page is already in the pool, increment its pin count.
        If not, load it from disk (potentially evicting another page).

        Returns the Page object. The caller MUST call unpin_page() when done.

        Raises RuntimeError if all frames are pinned and no eviction is possible.
        """
        with self._lock:
            # Check if page is already in the pool
            if page_id in self._page_table:
                frame_idx = self._page_table[page_id]
                self._headers[frame_idx].pin_count += 1
                self._headers[frame_idx].ref_bit = True
                self._hits += 1
                return self._frames[frame_idx]  # type: ignore

            # Page not in pool — need to load from disk
            self._misses += 1

            # Find a frame to use (empty or evict)
            frame_idx = self._find_victim_frame()

            # If the victim frame has a dirty page, flush it first
            victim_header = self._headers[frame_idx]
            if victim_header.page_id >= 0:
                if victim_header.is_dirty:
                    self._disk.write_page(self._frames[frame_idx])  # type: ignore
                del self._page_table[victim_header.page_id]
                self._evictions += 1

            # Load the requested page from disk
            page = self._disk.read_page(page_id)

            # Install in frame
            self._frames[frame_idx] = page
            self._headers[frame_idx] = FrameHeader(
                page_id=page_id,
                pin_count=1,
                is_dirty=False,
                ref_bit=True,
            )
            self._page_table[page_id] = frame_idx

            return page

    def new_page(self, page_type: PageType = PageType.HEAP_DATA) -> Page:
        """Allocate a new page on disk and bring it into the buffer pool.

        Returns the new Page object (already pinned).
        """
        with self._lock:
            # Allocate on disk
            page = self._disk.allocate_page(page_type)

            # Find a frame
            frame_idx = self._find_victim_frame()

            # Evict if necessary
            victim_header = self._headers[frame_idx]
            if victim_header.page_id >= 0:
                if victim_header.is_dirty:
                    self._disk.write_page(self._frames[frame_idx])  # type: ignore
                del self._page_table[victim_header.page_id]
                self._evictions += 1

            # Install
            self._frames[frame_idx] = page
            self._headers[frame_idx] = FrameHeader(
                page_id=page.header.page_id,
                pin_count=1,
                is_dirty=True,  # New pages are dirty by definition
                ref_bit=True,
            )
            self._page_table[page.header.page_id] = frame_idx

            return page

    def unpin_page(self, page_id: int, is_dirty: bool = False) -> bool:
        """Unpin a page, allowing it to be evicted.

        Args:
            page_id: The page to unpin
            is_dirty: If True, mark the page as dirty (needs flush)

        Returns True if the page was found and unpinned.
        """
        with self._lock:
            if page_id not in self._page_table:
                return False

            frame_idx = self._page_table[page_id]
            header = self._headers[frame_idx]

            if header.pin_count <= 0:
                return False  # Already fully unpinned

            header.pin_count -= 1
            if is_dirty:
                header.is_dirty = True

            return True

    def flush_page(self, page_id: int) -> bool:
        """Force a page to be written to disk.

        This is called by the WAL system to ensure durability.
        """
        with self._lock:
            if page_id not in self._page_table:
                return False

            frame_idx = self._page_table[page_id]
            header = self._headers[frame_idx]
            page = self._frames[frame_idx]

            if page is not None and header.is_dirty:
                self._disk.write_page(page)
                header.is_dirty = False

            return True

    def flush_all(self) -> None:
        """Flush all dirty pages to disk.

        Called during checkpoint or shutdown.
        """
        with self._lock:
            for frame_idx, header in enumerate(self._headers):
                if header.page_id >= 0 and header.is_dirty:
                    page = self._frames[frame_idx]
                    if page is not None:
                        self._disk.write_page(page)
                        header.is_dirty = False

    def delete_page(self, page_id: int) -> bool:
        """Remove a page from the buffer pool (does not deallocate on disk).

        The page must not be pinned.
        """
        with self._lock:
            if page_id not in self._page_table:
                return True  # Not in pool, nothing to do

            frame_idx = self._page_table[page_id]
            header = self._headers[frame_idx]

            if header.pin_count > 0:
                return False  # Cannot delete a pinned page

            del self._page_table[page_id]
            self._frames[frame_idx] = None
            self._headers[frame_idx] = FrameHeader()
            return True

    def get_stats(self) -> dict:
        """Return buffer pool statistics."""
        with self._lock:
            total_requests = self._hits + self._misses
            hit_rate = self._hits / total_requests if total_requests > 0 else 0.0
            return {
                "pool_size": self._pool_size,
                "pages_in_pool": len(self._page_table),
                "hits": self._hits,
                "misses": self._misses,
                "evictions": self._evictions,
                "hit_rate": f"{hit_rate:.2%}",
                "dirty_pages": sum(
                    1 for h in self._headers if h.is_dirty
                ),
                "pinned_pages": sum(
                    1 for h in self._headers if h.pin_count > 0
                ),
            }

    # ─── Clock-Sweep Eviction ────────────────────────────────────────────

    def _find_victim_frame(self) -> int:
        """Find a frame to evict using the clock-sweep algorithm.

        The clock hand sweeps through frames:
        1. If a frame is empty, use it immediately
        2. If a frame is pinned, skip it
        3. If ref_bit is set, clear it and move on (second chance)
        4. If ref_bit is clear and unpinned, evict it

        This is an approximation of LRU that runs in O(1) amortized time.
        """
        # First pass: look for empty frames
        for i in range(self._pool_size):
            if self._headers[i].page_id < 0:
                return i

        # Clock sweep
        max_sweeps = 2 * self._pool_size  # Limit to prevent infinite loop
        for _ in range(max_sweeps):
            header = self._headers[self._clock_hand]

            if header.pin_count == 0:
                if not header.ref_bit:
                    # Found a victim!
                    victim = self._clock_hand
                    self._clock_hand = (self._clock_hand + 1) % self._pool_size
                    return victim
                else:
                    # Give it a second chance
                    header.ref_bit = False

            self._clock_hand = (self._clock_hand + 1) % self._pool_size

        raise RuntimeError(
            "Buffer pool exhausted: all frames are pinned. "
            "This usually indicates a pin leak (fetch without unpin)."
        )
