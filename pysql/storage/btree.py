"""
B+Tree Index Implementation.

A B+Tree is the standard index structure used by virtually all relational
databases (PostgreSQL, MySQL, SQLite, Oracle, SQL Server). It provides:
- O(log n) point lookups
- O(log n + k) range scans (where k = number of results)
- O(log n) insertions and deletions
- Sequential access through leaf-level linked list

This implementation features:
- Configurable order (max keys per node)
- Leaf-level doubly-linked list for efficient range scans
- Bulk loading for initial index construction
- Support for duplicate keys (non-unique indexes)
- Serialization/deserialization for persistent storage

B+Tree Structure:
    ┌──────────────────────────────┐
    │     Internal Node (keys)     │
    │  [key1] [key2] ... [keyN]    │
    │  /    |      \\   ...  \\      │
    └──────────────────────────────┘
           ↓       ↓         ↓
    ┌──────────┐ ┌──────────┐ ┌──────────┐
    │ Leaf Node│→│ Leaf Node│→│ Leaf Node│  ← linked list
    │ (k,v)... │ │ (k,v)... │ │ (k,v)... │
    └──────────┘ └──────────┘ └──────────┘

Key difference from B-Tree:
- All data lives in leaf nodes (internal nodes only store routing keys)
- Leaves form a doubly-linked list for efficient range scans
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator, Optional


@dataclass
class BPlusTreeNode:
    """A node in the B+Tree (either internal or leaf).

    Internal nodes contain keys and child pointers.
    Leaf nodes contain keys, values, and sibling pointers.
    """
    keys: list[Any] = field(default_factory=list)
    children: list["BPlusTreeNode"] = field(default_factory=list)  # Internal nodes
    values: list[Any] = field(default_factory=list)  # Leaf nodes: parallel to keys
    is_leaf: bool = True
    next_leaf: Optional["BPlusTreeNode"] = None   # Right sibling (for range scans)
    prev_leaf: Optional["BPlusTreeNode"] = None   # Left sibling
    parent: Optional["BPlusTreeNode"] = None

    def __repr__(self) -> str:
        node_type = "Leaf" if self.is_leaf else "Internal"
        return f"{node_type}Node(keys={self.keys})"


class BPlusTree:
    """B+Tree index with support for point lookups, range scans, and modifications.

    The tree maintains the following invariants:
    1. All leaves are at the same depth
    2. Each internal node has between ⌈order/2⌉ and order children
    3. Each leaf has between ⌈(order-1)/2⌉ and order-1 keys
    4. The root can have fewer children (minimum 2 if not a leaf)
    5. Keys in internal nodes are separator keys (copies of leaf keys)
    6. Leaves form a doubly-linked list

    Args:
        order: Maximum number of children for internal nodes (minimum 3).
               Leaf nodes can hold order-1 key-value pairs.
    """

    def __init__(self, order: int = 128) -> None:
        if order < 3:
            raise ValueError("B+Tree order must be at least 3")
        self._order = order
        self._root: BPlusTreeNode = BPlusTreeNode(is_leaf=True)
        self._size = 0
        self._height = 1
        self._min_keys = (order - 1) // 2  # Minimum keys for non-root nodes

    @property
    def order(self) -> int:
        return self._order

    @property
    def size(self) -> int:
        """Total number of key-value pairs in the tree."""
        return self._size

    @property
    def height(self) -> int:
        """Height of the tree (1 = root only)."""
        return self._height

    # ─── Point Lookup ────────────────────────────────────────────────────

    def search(self, key: Any) -> Optional[Any]:
        """Look up a single key. Returns the value if found, None otherwise.

        Time complexity: O(log n) where n = number of keys.
        """
        leaf = self._find_leaf(key)
        for i, k in enumerate(leaf.keys):
            if k == key:
                return leaf.values[i]
        return None

    def search_all(self, key: Any) -> list[Any]:
        """Find all values associated with a key (for non-unique indexes).

        Returns a list of all matching values.
        """
        results = []
        leaf = self._find_leaf(key)

        while leaf is not None:
            for i, k in enumerate(leaf.keys):
                if k == key:
                    results.append(leaf.values[i])
                elif k > key:
                    return results
            leaf = leaf.next_leaf

        return results

    def contains(self, key: Any) -> bool:
        """Check if a key exists in the tree."""
        return self.search(key) is not None

    # ─── Range Scan ──────────────────────────────────────────────────────

    def range_scan(
        self,
        low: Optional[Any] = None,
        high: Optional[Any] = None,
        include_low: bool = True,
        include_high: bool = True,
    ) -> Iterator[tuple[Any, Any]]:
        """Scan a range of keys, yielding (key, value) pairs in sorted order.

        Args:
            low: Lower bound (None = no lower bound, scan from start)
            high: Upper bound (None = no upper bound, scan to end)
            include_low: Whether to include the lower bound
            include_high: Whether to include the upper bound

        Time complexity: O(log n + k) where k = number of results.
        """
        # Find the starting leaf
        if low is not None:
            leaf = self._find_leaf(low)
        else:
            # Start from the leftmost leaf
            leaf = self._root
            while not leaf.is_leaf:
                leaf = leaf.children[0]

        # Scan through leaves
        while leaf is not None:
            for i, key in enumerate(leaf.keys):
                # Check lower bound
                if low is not None:
                    if include_low and key < low:
                        continue
                    elif not include_low and key <= low:
                        continue

                # Check upper bound
                if high is not None:
                    if include_high and key > high:
                        return
                    elif not include_high and key >= high:
                        return

                yield (key, leaf.values[i])

            leaf = leaf.next_leaf

    def scan_all(self) -> Iterator[tuple[Any, Any]]:
        """Scan all key-value pairs in sorted order."""
        return self.range_scan()

    # ─── Insertion ───────────────────────────────────────────────────────

    def insert(self, key: Any, value: Any) -> None:
        """Insert a key-value pair into the B+Tree.

        If the key already exists, the value is added (duplicate keys
        are supported for non-unique indexes). Use `insert_unique()`
        if you want to enforce uniqueness.

        Time complexity: O(log n) amortized.
        """
        leaf = self._find_leaf(key)
        self._insert_into_leaf(leaf, key, value)
        self._size += 1

    def insert_unique(self, key: Any, value: Any) -> bool:
        """Insert a key-value pair, enforcing key uniqueness.

        Returns True if inserted, False if the key already exists.
        """
        leaf = self._find_leaf(key)
        for k in leaf.keys:
            if k == key:
                return False

        self._insert_into_leaf(leaf, key, value)
        self._size += 1
        return True

    def _insert_into_leaf(self, leaf: BPlusTreeNode, key: Any, value: Any) -> None:
        """Insert a key-value pair into a leaf node, splitting if necessary."""
        # Find insertion position (maintain sorted order)
        pos = self._bisect_right(leaf.keys, key)
        leaf.keys.insert(pos, key)
        leaf.values.insert(pos, value)

        # Check if the leaf overflows
        max_keys = self._order - 1
        if len(leaf.keys) > max_keys:
            self._split_leaf(leaf)

    def _split_leaf(self, leaf: BPlusTreeNode) -> None:
        """Split an overflowing leaf node into two leaf nodes.

        The split point is at ⌈(order-1)/2⌉. The right half gets a new node.
        The first key of the right node is promoted (copied) to the parent.
        """
        mid = (len(leaf.keys) + 1) // 2

        # Create new right leaf
        new_leaf = BPlusTreeNode(
            keys=leaf.keys[mid:],
            values=leaf.values[mid:],
            is_leaf=True,
        )

        # Truncate original leaf
        leaf.keys = leaf.keys[:mid]
        leaf.values = leaf.values[:mid]

        # Update linked list
        new_leaf.next_leaf = leaf.next_leaf
        new_leaf.prev_leaf = leaf
        if leaf.next_leaf is not None:
            leaf.next_leaf.prev_leaf = new_leaf
        leaf.next_leaf = new_leaf

        # Promote the separator key to the parent
        separator = new_leaf.keys[0]
        self._insert_into_parent(leaf, separator, new_leaf)

    def _insert_into_parent(
        self, left: BPlusTreeNode, key: Any, right: BPlusTreeNode
    ) -> None:
        """Insert a separator key into the parent node after a split.

        If the current node is the root, create a new root.
        If the parent overflows, recursively split it.
        """
        if left.parent is None:
            # Left is the root — create a new root
            new_root = BPlusTreeNode(
                keys=[key],
                children=[left, right],
                is_leaf=False,
            )
            left.parent = new_root
            right.parent = new_root
            self._root = new_root
            self._height += 1
            return

        parent = left.parent
        right.parent = parent

        # Find position in parent
        pos = parent.children.index(left) + 1
        parent.keys.insert(pos - 1, key)
        parent.children.insert(pos, right)

        # Check if parent overflows
        if len(parent.keys) >= self._order:
            self._split_internal(parent)

    def _split_internal(self, node: BPlusTreeNode) -> None:
        """Split an overflowing internal node.

        Unlike leaf splits, the middle key is pushed up (not copied).
        """
        mid = len(node.keys) // 2
        push_up_key = node.keys[mid]

        # Create new right internal node
        new_node = BPlusTreeNode(
            keys=node.keys[mid + 1:],
            children=node.children[mid + 1:],
            is_leaf=False,
        )

        # Update children's parent pointers
        for child in new_node.children:
            child.parent = new_node

        # Truncate original node
        node.keys = node.keys[:mid]
        node.children = node.children[:mid + 1]

        # Promote the middle key to the parent
        self._insert_into_parent(node, push_up_key, new_node)

    # ─── Deletion ────────────────────────────────────────────────────────

    def delete(self, key: Any) -> bool:
        """Delete a key from the B+Tree.

        Returns True if the key was found and deleted, False otherwise.
        Handles underflow via redistribution and merging.

        Time complexity: O(log n) amortized.
        """
        leaf = self._find_leaf(key)

        # Find the key in the leaf
        idx = -1
        for i, k in enumerate(leaf.keys):
            if k == key:
                idx = i
                break

        if idx == -1:
            return False  # Key not found

        # Remove the key-value pair
        leaf.keys.pop(idx)
        leaf.values.pop(idx)
        self._size -= 1

        # Handle underflow (root leaf or sufficient keys = no action needed)
        if leaf == self._root or len(leaf.keys) >= self._min_keys:
            # Update parent separator keys if needed
            if leaf.parent and idx == 0 and len(leaf.keys) > 0:
                self._update_parent_key(leaf)
            return True

        # Underflow — try redistribution, then merging
        self._handle_underflow(leaf)
        return True

    def _handle_underflow(self, node: BPlusTreeNode) -> None:
        """Handle a node with too few keys after deletion.

        Strategy:
        1. Try to redistribute from left sibling
        2. Try to redistribute from right sibling
        3. Merge with a sibling
        """
        parent = node.parent
        if parent is None:
            return  # Root — nothing to do

        idx = parent.children.index(node)

        # Try left sibling redistribution
        if idx > 0:
            left_sibling = parent.children[idx - 1]
            if len(left_sibling.keys) > self._min_keys:
                self._redistribute_from_left(node, left_sibling, parent, idx)
                return

        # Try right sibling redistribution
        if idx < len(parent.children) - 1:
            right_sibling = parent.children[idx + 1]
            if len(right_sibling.keys) > self._min_keys:
                self._redistribute_from_right(node, right_sibling, parent, idx)
                return

        # Must merge
        if idx > 0:
            # Merge with left sibling
            left_sibling = parent.children[idx - 1]
            self._merge_nodes(left_sibling, node, parent, idx - 1)
        else:
            # Merge with right sibling
            right_sibling = parent.children[idx + 1]
            self._merge_nodes(node, right_sibling, parent, idx)

    def _redistribute_from_left(
        self, node: BPlusTreeNode, left: BPlusTreeNode,
        parent: BPlusTreeNode, idx: int
    ) -> None:
        """Borrow a key from the left sibling."""
        if node.is_leaf:
            # Move last key-value from left sibling to front of node
            node.keys.insert(0, left.keys.pop())
            node.values.insert(0, left.values.pop())
            # Update parent separator
            parent.keys[idx - 1] = node.keys[0]
        else:
            # For internal nodes, pull down parent key, push up sibling's last key
            node.keys.insert(0, parent.keys[idx - 1])
            parent.keys[idx - 1] = left.keys.pop()
            child = left.children.pop()
            child.parent = node
            node.children.insert(0, child)

    def _redistribute_from_right(
        self, node: BPlusTreeNode, right: BPlusTreeNode,
        parent: BPlusTreeNode, idx: int
    ) -> None:
        """Borrow a key from the right sibling."""
        if node.is_leaf:
            node.keys.append(right.keys.pop(0))
            node.values.append(right.values.pop(0))
            parent.keys[idx] = right.keys[0]
        else:
            node.keys.append(parent.keys[idx])
            parent.keys[idx] = right.keys.pop(0)
            child = right.children.pop(0)
            child.parent = node
            node.children.append(child)

    def _merge_nodes(
        self, left: BPlusTreeNode, right: BPlusTreeNode,
        parent: BPlusTreeNode, separator_idx: int
    ) -> None:
        """Merge right node into left node."""
        if left.is_leaf:
            left.keys.extend(right.keys)
            left.values.extend(right.values)
            left.next_leaf = right.next_leaf
            if right.next_leaf:
                right.next_leaf.prev_leaf = left
        else:
            left.keys.append(parent.keys[separator_idx])
            left.keys.extend(right.keys)
            left.children.extend(right.children)
            for child in right.children:
                child.parent = left

        # Remove separator from parent
        parent.keys.pop(separator_idx)
        parent.children.pop(separator_idx + 1)

        # Handle parent underflow
        if parent == self._root:
            if len(parent.keys) == 0:
                self._root = left
                left.parent = None
                self._height -= 1
        elif len(parent.keys) < self._min_keys:
            self._handle_underflow(parent)

    def _update_parent_key(self, node: BPlusTreeNode) -> None:
        """Update parent separator keys when the leftmost key of a node changes."""
        parent = node.parent
        if parent is None:
            return

        idx = parent.children.index(node)
        if idx > 0:
            parent.keys[idx - 1] = node.keys[0]

    # ─── Bulk Loading ────────────────────────────────────────────────────

    def bulk_load(self, items: list[tuple[Any, Any]]) -> None:
        """Build the tree from a sorted list of (key, value) pairs.

        This is much faster than individual insertions for initial
        index construction: O(n) vs O(n log n).

        The items MUST be sorted by key.
        """
        if not items:
            return

        max_keys = self._order - 1

        # Build leaf level
        leaves: list[BPlusTreeNode] = []
        current_keys: list[Any] = []
        current_values: list[Any] = []

        for key, value in items:
            current_keys.append(key)
            current_values.append(value)

            if len(current_keys) == max_keys:
                leaf = BPlusTreeNode(
                    keys=current_keys,
                    values=current_values,
                    is_leaf=True,
                )
                leaves.append(leaf)
                current_keys = []
                current_values = []

        # Don't forget the last partial leaf
        if current_keys:
            leaf = BPlusTreeNode(
                keys=current_keys,
                values=current_values,
                is_leaf=True,
            )
            leaves.append(leaf)

        # Link leaves
        for i in range(len(leaves) - 1):
            leaves[i].next_leaf = leaves[i + 1]
            leaves[i + 1].prev_leaf = leaves[i]

        self._size = len(items)

        if len(leaves) == 1:
            self._root = leaves[0]
            self._height = 1
            return

        # Build internal levels bottom-up
        level = leaves
        self._height = 1

        while len(level) > 1:
            self._height += 1
            parents: list[BPlusTreeNode] = []
            i = 0

            while i < len(level):
                # Determine how many children this parent gets
                end = min(i + self._order, len(level))
                children = level[i:end]

                keys = [children[j].keys[0] for j in range(1, len(children))]

                parent = BPlusTreeNode(
                    keys=keys,
                    children=children,
                    is_leaf=False,
                )
                for child in children:
                    child.parent = parent

                parents.append(parent)
                i = end

            level = parents

        self._root = level[0]

    # ─── Utility Methods ─────────────────────────────────────────────────

    def _find_leaf(self, key: Any) -> BPlusTreeNode:
        """Navigate from root to the leaf node that should contain `key`.

        Time complexity: O(log n) — one comparison per tree level.
        """
        node = self._root
        while not node.is_leaf:
            # Binary search for the correct child pointer
            idx = self._bisect_right(node.keys, key)
            node = node.children[idx]
        return node

    @staticmethod
    def _bisect_right(sorted_list: list[Any], target: Any) -> int:
        """Binary search for the rightmost insertion point.

        Returns the index where `target` should be inserted to maintain
        sorted order (after any existing equal elements).
        """
        lo, hi = 0, len(sorted_list)
        while lo < hi:
            mid = (lo + hi) // 2
            if sorted_list[mid] <= target:
                lo = mid + 1
            else:
                hi = mid
        return lo

    def get_min(self) -> Optional[tuple[Any, Any]]:
        """Return the minimum key-value pair."""
        node = self._root
        while not node.is_leaf:
            node = node.children[0]
        if node.keys:
            return (node.keys[0], node.values[0])
        return None

    def get_max(self) -> Optional[tuple[Any, Any]]:
        """Return the maximum key-value pair."""
        node = self._root
        while not node.is_leaf:
            node = node.children[-1]
        if node.keys:
            return (node.keys[-1], node.values[-1])
        return None

    def pretty_print(self, node: Optional[BPlusTreeNode] = None, level: int = 0) -> str:
        """Generate a text visualization of the tree structure."""
        if node is None:
            node = self._root

        indent = "  " * level
        result = f"{indent}{'Leaf' if node.is_leaf else 'Internal'}: {node.keys}\n"

        if not node.is_leaf:
            for child in node.children:
                result += self.pretty_print(child, level + 1)

        return result
