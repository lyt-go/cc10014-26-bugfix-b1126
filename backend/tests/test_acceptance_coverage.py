"""验收覆盖与承重复检回归：对应“北廊木作”签署场景。

覆盖要点：
1. 原来缺承重的一单签署不能通过；
2. 承重补为合格后签署通过，签署回执记录采用的复检结果；
3. 另一位检查员同时提交更正时，回执与工序完成结果能说明哪次复检被采用；
4. 复检必须追溯到本工序的整改记录，其他工序的同名承重不计入；
5. 工序交接凭已签验收继续，否则给出未签原因和补测要求；
6. 签署完成后重复提交只归档一次。
"""
from __future__ import annotations

import concurrent.futures

import pytest

from app.seed import SEED_ROWS
from app.services.acceptance_record import AcceptanceRecordService, recheck_queue
from app.services.quality_inspect import QualityInspectService
from app.services.work_sequence import WorkSequenceService
from app.store import store


@pytest.fixture(autouse=True)
def reset_store():
    store._idempotent_log.clear()
    store._resource_locks.clear()
    for name in list(store._tables.keys()):
        if name in SEED_ROWS:
            store._tables[name] = [dict(row) for row in SEED_ROWS[name]]
        else:
            store._tables[name] = []
    yield


acceptance = AcceptanceRecordService()
quality = QualityInspectService()
work = WorkSequenceService()

PROCESS = "北廊木作"
STANDARD = "榫卯、承重"


def seed_process_acceptance():
    ws = {
        "id": 9001,
        "工序名称": PROCESS,
        "前置工序": "无",
        "所需工匠": "木作",
        "施工区域": "北廊",
        "预计工时": "80小时",
        "编排人": "工程主管",
        "工序状态": "",
        "备注说明": "",
        "status": "施工中",
        "pending": True,
        "abnormal": False,
    }
    store.rows("work_sequence").append(ws)
    acc = {
        "id": 8001,
        "验收工序": PROCESS,
        "验收标准": STANDARD,
        "验收结论": "待验",
        "遗留问题": "",
        "整改期限": "",
        "验收人": "监理甲",
        "验收日期": "2026-11-28",
        "备注说明": "",
        "status": "待验收",
        "pending": True,
        "abnormal": False,
    }
    store.rows("acceptance_record").append(acc)
    return ws, acc


def add_inspection(entry_id, item, status, result="合格", **extra):
    row = {
        "id": entry_id,
        "检查工序": PROCESS,
        "检查项目": item,
        "技术标准": STANDARD,
        "检查结果": result,
        "不合格项": "" if result == "合格" else result,
        "检查人": "检查员乙",
        "检查日期": "2026-11-28",
        "备注说明": "",
        "status": status,
        "pending": status != "已通过",
        "abnormal": status == "需整改",
    }
    row.update(extra)
    store.rows("quality_inspect").append(row)
    return row


def test_sign_without_load_bearing_item_fails_then_passes_after_recheck():
    ws, acc = seed_process_acceptance()
    # 同一检查项目榫卯有两条通过记录，但不能顶替承重
    add_inspection(7001, "榫卯", "已通过")
    add_inspection(7002, "榫卯", "已通过")
    # 承重这条是待整改
    add_inspection(7003, "承重", "需整改", result="承重不足")

    entry, message = acceptance.run_action(acc["id"], "签署验收")
    assert entry is None, f"缺承重不应能签署：{message}"
    assert "承重" in message
    assert "榫卯" not in (entry or {}).get("验收工序", "")

    # 承重补检为合格：复检必须追溯到本工序那条整改，否则不算数
    wrong = add_inspection(
        7004, "承重", "待检查", result="合格",
        **{"复检来源": "7003", "检查工序": "南廊木作"},
    )
    entry, message = quality.run_action(wrong["id"], "出具报告")
    assert entry is None, f"其他工序的同名复检不应计入：{message}"

    recheck = add_inspection(
        7005, "承重", "待检查", result="合格",
        **{"复检来源": "7003"},
    )
    entry, message = quality.run_action(recheck["id"], "出具报告")
    assert entry is not None, message
    assert entry["status"] == "已通过"

    entry, message = acceptance.run_action(acc["id"], "签署验收")
    assert entry is not None, f"补检合格后应能签署：{message}"
    assert entry["status"] == "已签署"
    receipt = entry["签署回执"]
    assert receipt["采用检查记录"]["承重"] == 7005
    assert receipt["采用复检记录"]["承重"] == 7005
    assert receipt["采用检查记录"]["榫卯"] in (7001, 7002)
    assert receipt["判定口径"] == "逐项覆盖"

    ws_row = store.find("work_sequence", ws["id"])
    assert ws_row["status"] == "已完成"
    assert ws_row["验收签署"] == acc["id"]


def test_concurrent_correction_and_sign_receipt_is_consistent():
    ws, acc = seed_process_acceptance()
    add_inspection(7101, "榫卯", "已通过")
    add_inspection(7102, "承重", "已通过", 检查人="检查员乙")

    # 另一位检查员针对刚才的合格承重结果提交更正（承重仍判合格）
    correction = add_inspection(
        7103, "承重", "待检查", result="合格",
        **{"更正来源": "7102", "检查人": "检查员丙"},
    )

    results = {}

    def sign():
        results["sign"] = acceptance.run_action(acc["id"], "签署验收")

    def correct():
        results["correct"] = quality.run_action(correction["id"], "提交更正")

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(sign), pool.submit(correct)]
        concurrent.futures.wait(futures, timeout=10)

    entry, message = results["sign"]
    assert entry is not None, f"承重已合格，签署应成功：{message}"
    receipt = entry["签署回执"]
    # 回执说明这次签署采用的承重检查是原合格记录还是更正后的记录
    adopted = receipt["采用检查记录"]["承重"]
    assert adopted in (7102, 7103)
    if adopted == 7103:
        # 更正记录采用时，仍能说明它基于哪次复检（此处原记录不是复检，故为空）
        assert receipt["采用复检记录"] == {}
        assert receipt["复检追溯"]["承重"]["检查记录"] == 7103
        assert receipt["复检追溯"]["承重"]["更正来源"] == [7102]
    else:
        assert receipt["采用复检记录"] == {}
    total_checks = [
        row for row in store.rows("quality_inspect")
        if row.get("检查工序") == PROCESS and row.get("检查项目") == "承重"
    ]
    assert len(total_checks) == 2  # 原记录 + 更正，没有重复写入


def test_recheck_queue_and_handover_flow():
    ws, acc = seed_process_acceptance()
    add_inspection(7201, "榫卯", "已通过")
    add_inspection(7202, "承重", "需整改", result="承重不足")

    # 未签署时交接给出未签原因与补测要求
    evaluation = work.evaluate_handover(ws["id"])
    assert evaluation["ok"] is False
    assert any(item["检查记录"] == 7202 for item in evaluation["补测要求"])

    # 承重复检合格后签署
    add_inspection(7203, "承重", "待检查", result="合格", **{"复检来源": "7202"})
    quality.run_action(7203, "出具报告")
    entry, message = acceptance.run_action(acc["id"], "签署验收")
    assert entry is not None, message

    # 已签结果可以直接交接
    evaluation = work.evaluate_handover(ws["id"])
    assert evaluation["ok"] is True
    assert evaluation["依据验收"] == acc["id"]
    assert evaluation["采用检查记录"]["承重"] == 7203

    entry, message = work.run_action(ws["id"], "工序交接")
    assert entry is not None, message
    assert entry["status"] == "已完成"
    assert entry["交接结果"] == "已交接"


def test_sign_syncs_pending_defects_to_recheck_queue():
    ws, acc = seed_process_acceptance()
    add_inspection(7301, "榫卯", "已通过")
    add_inspection(7302, "承重", "已通过")
    # 遗留一条其它缺陷，不影响签署（覆盖项已齐），但需继续复查
    add_inspection(7303, "防腐", "需整改", result="涂层脱落")

    entry, message = acceptance.run_action(acc["id"], "签署验收")
    assert entry is not None, message
    queue = recheck_queue(PROCESS)
    assert any(item["检查记录"] == 7303 and item["待复检"] for item in queue)
    ws_row = store.find("work_sequence", ws["id"])
    assert ws_row.get("完成待复检") is True
    assert ws_row["status"] != "已完成"

    # 该缺陷复检合格后才解除
    add_inspection(7304, "防腐", "待检查", result="合格", **{"复检来源": "7303"})
    quality.run_action(7304, "出具报告")
    assert not recheck_queue(PROCESS)


def test_repeated_sign_archives_once():
    ws, acc = seed_process_acceptance()
    add_inspection(7401, "榫卯", "已通过")
    add_inspection(7402, "承重", "已通过")

    first, message = acceptance.run_action(acc["id"], "签署验收")
    assert first is not None, message
    count_after_first = len(store.rows("archive_mgmt"))

    second, message = acceptance.run_action(acc["id"], "签署验收")
    assert "已执行过" in message
    assert len(store.rows("archive_mgmt")) == count_after_first
    archived = [
        row for row in store.rows("archive_mgmt")
        if str(row.get("备注说明", "")).startswith(f"验收签署#{acc['id']}")
    ]
    assert len(archived) == 1


def test_correction_overturning_pass_blocks_sign():
    """同一检查项目的合格结果被后续更正推翻后，不能用它签署。"""
    ws, acc = seed_process_acceptance()
    add_inspection(7601, "榫卯", "已通过")
    add_inspection(7602, "承重", "已通过")
    correction = add_inspection(
        7603, "承重", "待检查", result="复测不合格",
        **{"更正来源": "7602", "检查人": "检查员丙"},
    )
    entry, message = quality.run_action(correction["id"], "提交更正")
    assert entry is not None, message
    assert entry["status"] == "需整改"

    entry, message = acceptance.run_action(acc["id"], "签署验收")
    assert entry is None, f"承重已被更正为不合格，不应签署：{message}"
    assert "承重" in message

    # 再补一次可追溯的合格复检后可以签署，回执采用复检
    recheck = add_inspection(
        7604, "承重", "待检查", result="合格", **{"复检来源": "7603"}
    )
    quality.run_action(recheck["id"], "出具报告")
    entry, message = acceptance.run_action(acc["id"], "签署验收")
    assert entry is not None, message
    assert entry["签署回执"]["采用复检记录"]["承重"] == 7604


def test_correction_must_target_existing_record():
    ws, acc = seed_process_acceptance()
    add_inspection(7701, "承重", "待检查", result="合格", **{"更正来源": "999999"})
    entry, message = quality.run_action(7701, "提交更正")
    assert entry is None
    assert "原检查记录" in message


def test_legacy_acceptance_with_standard_code_still_signs():
    """兼容既有记录：验收标准只写标准号时沿用“至少一条通过”的口径。"""
    ws, acc = seed_process_acceptance()
    acc["验收标准"] = "GB50203-2011"
    add_inspection(7501, "墙体强度", "已通过")
    entry, message = acceptance.run_action(acc["id"], "签署验收")
    assert entry is not None, message
    assert entry["签署回执"]["判定口径"] == "兼容既有合格记录"
