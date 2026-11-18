"""ooo_hop_insert 测试夹具：隔离的临时 DATA_DIR / FIXTURES_DIR。"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent.parent
_TMP = tempfile.mkdtemp(prefix="hoptrace-test-")
_DATA = Path(_TMP) / "data"
_FIX = Path(_TMP) / "fixtures"
_DATA.mkdir(parents=True, exist_ok=True)
_FIX.mkdir(parents=True, exist_ok=True)

# 复制真实夹具（只读引用，不改动仓库 fixtures）
import shutil  # noqa: E402

for name in ("seed_hops.json", "ingest_batch.json", "ingest_batch_ooo.json"):
    shutil.copy(_REPO / "fixtures" / name, _FIX / name)

os.environ["DATA_DIR"] = str(_DATA)
os.environ["FIXTURES_DIR"] = str(_FIX)
sys.path.insert(0, str(_REPO / "backend"))


@pytest.fixture()
def client():
    # 每个用例独立库文件，保证互不污染
    db_file = _DATA / f"test-{os.getpid()}-{id(object())}.db"
    if db_file.exists():
        db_file.unlink()

    import app.db as db_mod
    original = db_mod.DB_PATH
    db_mod.DB_PATH = db_file
    try:
        from fastapi.testclient import TestClient
        from app.main import app
        with TestClient(app) as c:
            yield c
    finally:
        db_mod.DB_PATH = original
        if db_file.exists():
            db_file.unlink()
