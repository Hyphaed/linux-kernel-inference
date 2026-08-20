"""Make `import hyphaed` work when running pytest from the repo root."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _clear_mainline_lru_caches():
    """_mainline_available_tags/_mainline_has_amd64_build are @lru_cache'd
    (hyphaed/phases/source.py) to avoid duplicate network calls within one
    real `source` phase invocation. Tests monkeypatch `run` differently per
    test and call these directly, so a stale cache from an earlier test
    would leak in — reset before every test."""
    from hyphaed.phases.source import _mainline_available_tags, _mainline_has_amd64_build
    _mainline_available_tags.cache_clear()
    _mainline_has_amd64_build.cache_clear()
    yield
