# -*- coding: utf-8 -*-
"""
线程安全工具类
"""
from __future__ import annotations
import threading
from typing import Generic, Iterator, List, Optional, TypeVar

T = TypeVar("T")


class LockedList(Generic[T]):
    """
    线程安全列表封装
    替换原来 self._batch_items（普通 list 被多线程无锁访问）
    """

    def __init__(self):
        self._data: List[T] = []
        self._lock = threading.RLock()

    def append(self, item: T) -> None:
        with self._lock:
            self._data.append(item)

    def extend(self, items: List[T]) -> None:
        with self._lock:
            self._data.extend(items)

    def remove_by(self, predicate) -> int:
        """删除满足条件的元素，返回删除数量"""
        with self._lock:
            before = len(self._data)
            self._data = [x for x in self._data if not predicate(x)]
            return before - len(self._data)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def snapshot(self) -> List[T]:
        """返回当前数据的线程安全快照"""
        with self._lock:
            return list(self._data)

    def update_item(self, predicate, updater) -> bool:
        """找到第一个满足条件的元素并更新，返回是否找到"""
        with self._lock:
            for item in self._data:
                if predicate(item):
                    updater(item)
                    return True
        return False

    def ids(self, id_getter) -> set:
        with self._lock:
            return {id_getter(x) for x in self._data}

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    def __iter__(self) -> Iterator[T]:
        # 注意：迭代快照，不持有锁迭代
        return iter(self.snapshot())
