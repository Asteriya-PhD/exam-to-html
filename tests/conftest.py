"""
exam-to-html conftest — 隔离 topic_garden DB, 防测试污染生产库

策略 (对齐 topic_garden_app/tests/conftest.py v0.12.1):
- 必须在 import topic_garden 之前设 TOPIC_GARDEN_DB_PATH env var
- 否则 db._resolve_db_path 已冻结到生产路径
- 用 per-pid 路径避免并行 pytest 撞库
"""
import os

# v0.1: 强制测试 DB 隔离 — 必须在任何 topic_garden import 前生效
_test_db_path = f"/tmp/exam_to_html_test_{os.getpid()}.db"
os.environ.setdefault("TOPIC_GARDEN_DB_PATH", _test_db_path)

# pytest-xdist worker 用 worker_id 区分; 单进程 pytest 用 pid 即可
_worker_id = os.environ.get("PYTEST_XDIST_WORKER")
if _worker_id:
    os.environ["TOPIC_GARDEN_DB_PATH"] = f"/tmp/exam_to_html_test_{_worker_id}.db"

# 强制惰性初始化 — 不要在 conftest import 期 import topic_garden
# 让各测试文件自己 import, 此时 env 已生效