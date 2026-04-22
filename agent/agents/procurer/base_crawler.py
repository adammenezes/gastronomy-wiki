"""
BaseCrawler — interface all source crawlers must implement.

To add a new source type:
  1. Subclass BaseCrawler
  2. Implement discover(topics) → list[Lead]
  3. Register the class name in CRAWLER_REGISTRY inside agents/procurer/__init__.py
  4. Add a source entry in agent/sources.yaml with crawler: YourClassName
"""

from abc import ABC, abstractmethod
from .lead import Lead


class BaseCrawler(ABC):
    def __init__(self, source_config: dict):
        self.config = source_config

    @abstractmethod
    def discover(self, topics: list[str]) -> list[Lead]:
        """
        Discover leads from this source.

        Args:
            topics: list of gap topic strings from GapAnalyzer — use these
                    to filter or prioritise what you fetch.

        Returns:
            list of Lead objects (unsorted, unscored).
        """
        ...
