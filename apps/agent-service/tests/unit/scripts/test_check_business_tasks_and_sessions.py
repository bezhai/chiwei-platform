"""CI 规则：业务代码不自己起任务、不起线程，也够不着事务里的数据库会话。

规则本身在 ``scripts/check_business_tasks_and_sessions.py``，CI（``.github/workflows/grep-gate.yml``）
直接跑它。这里用临时目录造出每一种写法的违规，证明它报出文件和行；造出允许的写法（等着的
``gather``、``to_thread``，事务函数 ``tx``），证明它们不算；再对真实代码树跑一遍，证明现在是干净的。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import app as app_pkg
from scripts.check_business_tasks_and_sessions import BUSINESS, find_violations

SERVICE_DIR = Path(__file__).resolve().parents[3]


def _tree(tmp_path: Path, files: dict[str, str], *, without: tuple[str, ...] = ()) -> Path:
    root = tmp_path / "app"
    base = {"__init__.py": "", **{f"{d}/__init__.py": "" for d in BUSINESS}}
    for rel, content in {**base, **files}.items():
        if rel.split("/")[0] in without:
            continue
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


def _sites(problems: list[str]) -> list[str]:
    return [p.split(": ")[0] for p in problems]


STARTING_A_TASK = [
    ("import asyncio\nasyncio.create_task(work())", 2),
    ("from asyncio import create_task\ncreate_task(work())", 1),
    ("import asyncio\nloop = asyncio.get_running_loop()\nloop.create_task(work())", 3),
    ("import asyncio\nasyncio.ensure_future(work())", 2),
    ("from asyncio import ensure_future\nensure_future(work())", 1),
    ("import asyncio\nasync def f():\n    async with asyncio.TaskGroup() as tg:\n        pass", 3),
    ("from asyncio import TaskGroup", 1),
    ("import threading\nthreading.Thread(target=print).start()", 2),
    ("from threading import Thread\nThread(target=print)", 1),
    ("import threading\nthreading.Timer(1, print).start()", 2),
    ("def later():\n    import asyncio\n    asyncio.create_task(work())", 3),
    # 换了名字的模块和名字：import ... as、from ... import ... as、asyncio 的子模块、import *。
    ("import threading as th\nth.Thread(target=print).start()", 2),
    ("import threading as th\nth.Timer(1, print).start()", 2),
    ("from threading import Thread as T\nT(target=print)", 1),
    ("from threading import Timer as T\nT(1, print)", 1),
    ("from threading import *\nThread(target=print)", 2),
    ("import asyncio as aio\naio.create_task(work())", 2),
    ("import asyncio as aio\naio.ensure_future(work())", 2),
    ("import asyncio as aio\nasync def f():\n    async with aio.TaskGroup() as tg:\n        pass", 3),
    ("import asyncio as aio\nloop = aio.get_running_loop()\nloop.create_task(work())", 3),
    ("from asyncio import create_task as spawn\nspawn(work())", 1),
    ("from asyncio import ensure_future as later\nlater(work())", 1),
    ("from asyncio import TaskGroup as Group", 1),
    ("from asyncio.tasks import create_task as spawn\nspawn(work())", 1),
    ("from asyncio.tasks import ensure_future as later\nlater(work())", 1),
    ("from asyncio.taskgroups import TaskGroup as Group", 1),
    ("import asyncio.tasks as t\nt.create_task(work())", 2),
    ("from asyncio import *\nasync def f():\n    async with TaskGroup() as tg:\n        pass", 3),
]


@pytest.mark.parametrize(("source", "line"), STARTING_A_TASK)
def test_starting_a_task_or_a_thread_is_reported_with_its_file_and_line(tmp_path, source, line):
    root = _tree(tmp_path, {"living/a/tools.py": source + "\n"})

    problems = find_violations(root)

    assert _sites(problems)[0] == f"app/living/a/tools.py:{line}", problems
    assert "starts" in problems[0]


REACHING_THE_SESSION = [
    ("from app.data.tx import current_session", 1),
    ("from app.data.tx import auto_tx, current_session", 1),
    ("import app.data.tx as t\nt.current_session()", 1),
    ("import app.data as d\nd.tx.current_session()", 2),
    ("from app.data import tx", 1),
    ("import app.data.tx", 1),
    ("from ...data import tx", 1),
    # 换了名字的。
    ("from app.data.tx import current_session as cs\ncs()", 1),
    ("from app.data import tx as t", 1),
    ("from app import data\ndata.tx.current_session()", 2),
    ("from app import data as d\nd.tx.current_session()", 2),
    ("from ...data.tx import current_session as cs", 1),
    ("from app.data.tx import *\ncurrent_session()", 2),
    # 动态 import 整个 app.data.tx 模块，和 ``import app.data.tx`` 一样。
    ("import importlib\nimportlib.import_module('app.data.tx')", 2),
    ("from importlib import import_module as load\nload('.tx', 'app.data')", 2),
    ("__import__('app.data', fromlist=['tx'])", 1),
]


@pytest.mark.parametrize(("source", "line"), REACHING_THE_SESSION)
def test_reaching_the_transactions_session_is_reported_with_its_file_and_line(
    tmp_path, source, line
):
    """``current_session`` 交出事务里的会话，拿到它就能在 ``tx()`` 里跑任意语句，绕开存储的读写函数；
    ``app.data.tx`` 整个模块同样够得着它。"""
    root = _tree(tmp_path, {"living/a/store.py": source + "\n"})

    problems = find_violations(root)

    assert _sites(problems)[0] == f"app/living/a/store.py:{line}", problems
    assert "session" in problems[0]


@pytest.mark.parametrize("directory", BUSINESS)
def test_every_business_directory_is_covered(tmp_path, directory):
    root = _tree(tmp_path, {f"{directory}/x.py": "import asyncio\nasyncio.create_task(work())\n"})

    assert _sites(find_violations(root)) == [f"app/{directory}/x.py:2"]


def test_waiting_on_work_and_the_transaction_function_are_allowed(tmp_path):
    root = _tree(
        tmp_path,
        {
            "world/a/tools.py": "\n".join(
                [
                    '"""Never asyncio.create_task(...) here; see current_session."""',
                    "import asyncio",
                    "import threading",
                    "from app.data.tx import tx",
                    "from app.data.persist import fetch_all",
                    "import threading as th",
                    "import asyncio as aio",
                    "from threading import Event as Flag",
                    "from app.data.tx import tx as transaction",
                    "lock = threading.Lock()",
                    "other = th.Lock()",
                    "flag = Flag()",
                    "# asyncio.ensure_future(x)",
                    "async def f():",
                    "    await asyncio.gather(a(), b())",
                    "    await asyncio.to_thread(print)",
                    "    await asyncio.wait_for(a(), 1)",
                    "    await aio.gather(a(), b())",
                    "    async with tx():",
                    "        await fetch_all('SELECT 1')",
                ]
            )
        },
    )

    assert find_violations(root) == []


def test_the_framework_is_not_business(tmp_path):
    """宿主替插件起任务（``ctx.task``、``ctx.clock``），存储和消息机制开会话：它们不在这条规则里。"""
    root = _tree(
        tmp_path,
        {
            "host/__init__.py": "",
            "host/host.py": "import asyncio\nasyncio.create_task(work())\n",
            "messaging/__init__.py": "",
            "messaging/claims.py": "from app.data.tx import current_session\n",
            "agent/__init__.py": "",
            "agent/core.py": "import asyncio\nasyncio.ensure_future(work())\n",
        },
    )

    assert find_violations(root) == []


@pytest.mark.parametrize("gone", BUSINESS)
def test_a_listed_business_directory_that_does_not_exist_is_reported(tmp_path, gone):
    root = _tree(tmp_path, {}, without=(gone,))

    problems = find_violations(root)

    assert _sites(problems) == [f"app/{gone}"]
    assert "does not exist" in problems[0]


def test_the_real_code_tree_is_clean():
    assert find_violations(Path(app_pkg.__file__).parent) == []


def test_the_command_fails_with_the_file_and_line_and_passes_when_clean(tmp_path):
    dirty = _tree(tmp_path / "dirty", {"plugins/ops.py": "\n\nfrom app.data import tx\n"})
    clean = _tree(tmp_path / "clean", {"plugins/ops.py": "from app.data.tx import tx\n"})

    def run(root: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "scripts.check_business_tasks_and_sessions", str(root)],
            cwd=SERVICE_DIR,
            capture_output=True,
            text=True,
        )

    failed = run(dirty)
    passed = run(clean)

    assert failed.returncode == 1, failed.stdout + failed.stderr
    assert "app/plugins/ops.py:3" in failed.stdout
    assert passed.returncode == 0, passed.stdout + passed.stderr
    real = run(SERVICE_DIR / "app")
    assert real.returncode == 0, real.stdout + real.stderr
