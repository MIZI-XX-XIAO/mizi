"""本文件测试耗时工艺关联在独立Qt工作线程中执行。"""

import numpy as np
import pandas as pd
from PySide6.QtCore import QThread

from gui.relationship_worker import RelationshipWorker


def test_relationship_worker_runs_job_in_background(qtbot) -> None:
    orders = np.arange(1, 21)
    products = pd.DataFrame({"global_order": orders})
    parameters = pd.DataFrame({"global_order": orders, "pressure": orders.astype(float)})
    defects = pd.DataFrame({"global_order": orders[orders % 2 == 0], "component_area": 1})
    worker = RelationshipWorker(
        [("5S", "图片算法检出", "IMAGE", "", products, defects)],
        parameters, (),
    )
    thread = QThread()
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    worker.finished.connect(thread.quit)

    with qtbot.waitSignal(worker.completed, timeout=10_000) as blocker:
        thread.start()
    thread.wait(10_000)

    assert len(blocker.args[0]) == 1
    assert blocker.args[0][0][4].summary["product_count"] == 20
