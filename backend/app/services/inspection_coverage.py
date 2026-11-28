"""验收覆盖判定：把验收标准拆成必须覆盖的检查项目，并逐项核对质量检查记录。

这个模块只做纯计算，不修改数据（除了给调用方返回记录引用）。验收服务和质量检查
服务都依赖它，保证“签署审批时实际采用的检查项目”和“检查清单”是同一套口径：

* 验收标准里列出的每一个项目都必须有一条属于本工序的合格记录；
* 同一个项目的多条合格记录只算一条，不能拿去顶替另一个项目；
* 复检记录必须能追溯回本工序对应的那条整改记录，其他工序的同名记录不计入；
* 已被后续“更正”记录推翻的合格记录不再作为覆盖依据。
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable

PASS_STATUSES = {"已通过", "合格", "通过", "复检合格", "已复检"}
BLOCKING_STATUSES = {"待检查", "检查中", "需整改", "待复检", "已退回", "不合格"}
PASS_WORDS = ("合格", "通过", "符合")

# 复检记录里可能用来指向“原整改记录”的字段名。
RECHECK_SOURCE_FIELDS = (
    "复检来源", "复检原单", "复检原记录", "原检查", "原检查记录", "原记录", "来源检查",
    "关联整改", "复检对应整改", "对应整改", "整改记录", "整改来源", "整改单号", "整改记录号",
)
# 更正记录里可能用来指向“被更正结果”的字段名。
CORRECTION_SOURCE_FIELDS = (
    "更正来源", "更正对象", "更正原记录", "被更正记录", "纠正来源", "纠正对象",
)
RECHECK_FLAG_FIELDS = ("复检", "补测", "复检标志", "是否需要复检")
RECHECK_TYPE_FIELDS = ("检查类型", "检查类别", "记录类型", "复检状态")

_SPLIT_RE = re.compile(r"[、,，;；/|+&\n\r\t\s]+")
_NUMBER_PREFIX_RE = re.compile(r"^\s*(?:\d+|[一二三四五六七八九十]+)\s*[.、)）:：]\s*")
_BRACKET_RE = re.compile(r"[（(【\[][^）)】\]]*[）)】\]]")
_CODE_RE = re.compile(
    r"^(?:GB|GBT|JGJ|DB|CJJ|WW|SL|TJ|QB|ISO|EN|ASTM|JC|JG)[\w\-./]*$", re.I
)
_GENERIC_TOKENS = {
    "", "合格", "不合格", "验收", "验收标准", "检查", "检查项目", "检查清单", "清单",
    "质量", "标准", "全部", "所有", "各", "项", "项目", "要求", "结果", "记录", "工序",
}
# 看起来像标准号/描述而不是具体检查项目的词，不当作缺项。
_REFERENCE_WORDS = (
    "标准", "规范", "样例", "验收", "合格", "通过", "检查", "质量",
    "按设计", "图纸", "要求", "满足", "符合", "试验", "报告", "记录",
)


def _looks_like_item(token: str) -> bool:
    """判断一个词是否像具体的检查项目名（而不是一句验收描述）。"""
    text = str(token or "").strip()
    if not text or len(normalize(text)) > 6:
        return False
    # 描述性文字常见的连接词、否定词和语气片段
    descriptive = (
        "的", "了", "应", "须", "需", "并", "且", "或", "与", "及", "其", "该",
        "每", "不", "无", "未", "有", "按", "满", "符", "要", "求", "试", "验",
    )
    return not any(part in text for part in descriptive)


def normalize(value: Any) -> str:
    """用于比较项目名称的归一化：去空白、括号、常见分隔符，统一大小写。"""
    text = str(value or "").strip().casefold()
    text = _BRACKET_RE.sub("", text)
    text = re.sub(r"[\s\-_/#:：()（）\[\]【】]+", "", text)
    return text


def as_int(value: Any) -> int | None:
    try:
        if isinstance(value, bool):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def ids_from_value(value: Any) -> list[int]:
    """从 "质量检查#12"、[12]、12 等写法里取出记录 id。"""
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple, set)):
        result: list[int] = []
        for item in value:
            result.extend(ids_from_value(item))
        return result
    direct = as_int(value)
    if direct is not None:
        return [direct]
    if isinstance(value, dict):
        result = []
        for item in value.values():
            result.extend(ids_from_value(item))
        return result
    return [int(match) for match in re.findall(r"\d+", str(value))]


def _source_ids(row: dict[str, Any], fields: Iterable[str]) -> list[int]:
    result: list[int] = []
    for field in fields:
        result.extend(ids_from_value(row.get(field)))
    return list(dict.fromkeys(result))


def recheck_source_ids(row: dict[str, Any]) -> list[int]:
    return _source_ids(row, RECHECK_SOURCE_FIELDS)


def correction_source_ids(row: dict[str, Any]) -> list[int]:
    return _source_ids(row, CORRECTION_SOURCE_FIELDS)


def is_recheck(row: dict[str, Any]) -> bool:
    if recheck_source_ids(row):
        return True
    for field in RECHECK_FLAG_FIELDS:
        value = row.get(field)
        if value is True or str(value).strip() in {"1", "是", "true", "True", "复检", "补测"}:
            return True
    for field in RECHECK_TYPE_FIELDS:
        value = str(row.get(field) or "")
        if "复检" in value or "补测" in value or "复查" in value:
            return True
    # 结果/备注里明确写了“复检/补测”的记录，也算复检，必须能追溯来源
    result_text = str(row.get("检查结果") or "")
    if "复检" in result_text or "补测" in result_text:
        return True
    try:
        if int(row.get("复检轮次") or 0) > 1:
            return True
    except (TypeError, ValueError):
        pass
    return False


def is_correction(row: dict[str, Any]) -> bool:
    if correction_source_ids(row):
        return True
    return bool(row.get("更正") or row.get("已更正") or row.get("纠正"))


def result_passes(row: dict[str, Any]) -> bool:
    """只看检查结果文本，用于“出具报告”时决定落到通过还是需整改。"""
    result = str(row.get("检查结果") or row.get("核验结果") or "")
    if any(word in result for word in ("不合格", "不符合", "未通过", "不通过")):
        return False
    if any(word in result for word in PASS_WORDS):
        return True
    status = str(row.get("status") or "").strip()
    return status in PASS_STATUSES


def is_passed(row: dict[str, Any]) -> bool:
    """兼容既有记录：状态是合格类，或状态未被卡住且检查结果写了合格。"""
    status = str(row.get("status") or "").strip()
    if status in PASS_STATUSES:
        return True
    if status in BLOCKING_STATUSES:
        return False
    result = str(row.get("检查结果") or row.get("核验结果") or "")
    return any(word in result for word in PASS_WORDS)


def _item_names(rows: Iterable[dict[str, Any]]) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for row in rows:
        item = str(row.get("检查项目") or "").strip()
        key = normalize(item)
        if key and key not in seen:
            seen.add(key)
            names.append(item)
    return names


def _match_item_token(token: str, item_names: list[str]) -> str | None:
    """把一个标准里的项目词映射到记录里实际使用的项目名称。"""
    key = normalize(token)
    if not key:
        return None
    for item in item_names:
        if normalize(item) == key:
            return item
    containment = [
        item for item in item_names
        if len(normalize(item)) >= 2
        and (normalize(item) in key or key in normalize(item))
    ]
    if containment:
        # 取匹配得最长、最具体的项目名。
        return max(containment, key=lambda item: len(normalize(item)))
    return None


def _split_standard(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        raw_parts = [str(part) for part in value]
    elif isinstance(value, dict):
        raw_parts = [str(part) for part in value.values()]
    else:
        text = str(value).strip()
        if not text:
            return []
        if text.startswith("[") or text.startswith("{"):
            try:
                parsed = json.loads(text)
            except (TypeError, ValueError):
                parsed = None
            if isinstance(parsed, (list, tuple, dict)):
                return _split_standard(parsed)
        raw_parts = _SPLIT_RE.split(text)
    tokens: list[str] = []
    for part in raw_parts:
        cleaned = _NUMBER_PREFIX_RE.sub("", str(part))
        cleaned = _BRACKET_RE.sub("", cleaned)
        cleaned = cleaned.strip(" .。:：,，;；、")
        if not cleaned:
            continue
        if _CODE_RE.match(cleaned):
            continue
        if normalize(cleaned) in _GENERIC_TOKENS:
            continue
        tokens.append(cleaned)
    # 去重但保持顺序
    deduped: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        key = normalize(token)
        if key not in seen:
            seen.add(key)
            deduped.append(token)
    return deduped


def parse_required_items(
    entry: dict[str, Any], rows: list[dict[str, Any]]
) -> tuple[list[str], bool]:
    """返回 (标准要求的项目列表, 是否按项目清单判定)。

    验收标准和显式检查清单会合并取并集，避免清单过期漏掉标准新增的项目。
    验收标准没有列举项目（例如只写了一个标准号）时返回第二个值 False，
    继续沿用既有的“至少一条质量检查通过”口径，避免历史验收单无法签署。
    """
    sources: list[Any] = []
    for field in ("检查清单", "要求检查项目", "应检查项目", "验收标准"):
        value = entry.get(field)
        if value not in (None, "", []):
            sources.append(value)
    item_names = _item_names(rows)
    required: list[str] = []
    all_tokens: list[str] = []
    matched_any = False
    for source in sources:
        for token in _split_standard(source):
            if normalize(token) in {normalize(item) for item in all_tokens}:
                continue
            all_tokens.append(token)
            matched = _match_item_token(token, item_names)
            if matched:
                matched_any = True
                if normalize(matched) not in {normalize(item) for item in required}:
                    required.append(matched)
                continue
            # 没有对应记录的项目仍然要算“缺项”，但只接受像项目名的短词，
            # 避免把一句验收描述拆成很多不存在的项目。
            if (
                len(normalize(token)) <= 12
                and not re.search(r"[。！？!?]", token)
                and not any(word in token for word in _REFERENCE_WORDS)
                and _looks_like_item(token)
            ):
                if normalize(token) not in {normalize(item) for item in required}:
                    required.append(token)

    if not required:
        # 兜底：从描述性文字里找出记录中出现过的项目名。
        source_text = normalize("".join(str(source) for source in sources))
        for item in item_names:
            if normalize(item) and normalize(item) in source_text:
                required.append(item)
                matched_any = True

    if not required:
        return [], False

    if matched_any:
        return required, True

    # 一个项目都没匹配上：只有当所有词都像项目名时才按缺项判定，
    # 否则视为描述性标准，回退到兼容口径（既有的合格记录）。
    item_like = [
        token for token in all_tokens
        if len(normalize(token)) <= 6
        and not any(word in token for word in _REFERENCE_WORDS)
        and _looks_like_item(token)
    ]
    if item_like:
        return required, True
    return [], False


def item_matches_record(item: str, row: dict[str, Any]) -> bool:
    item_key = normalize(item)
    record_key = normalize(row.get("检查项目"))
    if not item_key or not record_key:
        return False
    if item_key == record_key:
        return True
    return len(item_key) >= 2 and len(record_key) >= 2 and (
        item_key in record_key or record_key in item_key
    )


def correction_is_effective(row: dict[str, Any]) -> bool:
    """更正只有实际出具后才推翻原结果；仅登记未提交的更正不参与判定。"""
    status = str(row.get("status") or "").strip()
    return status not in {"", "待检查", "检查中", "待复检"} or bool(row.get("已出具"))


def resolved_by_recheck(
    row: dict[str, Any], all_rows: list[dict[str, Any]], invalidated: set[int] | None = None
) -> bool:
    """该整改记录是否已被后续可追溯的合格结果解决。

    复检记录必须能追溯到本工序的这条整改才算数；同一工序后续重新检查并
    出具合格的普通记录也视为整改完成（兼容既有的补检方式）。
    """
    row_id = as_int(row.get("id"))
    if row_id is None:
        return False
    if invalidated is None:
        invalidated = _later_correction_ids(all_rows)
    process = str(row.get("检查工序") or "")
    item = str(row.get("检查项目") or "")
    for other in all_rows:
        other_id = as_int(other.get("id"))
        if other_id is None or other_id <= row_id or other_id in invalidated:
            continue
        if not is_passed(other):
            continue
        if str(other.get("检查工序") or "") != process:
            continue
        if not item_matches_record(item, other):
            continue
        if is_recheck(other) and record_trace_error(other, process, item, all_rows):
            continue
        return True
    return False


def _later_correction_ids(rows: list[dict[str, Any]]) -> set[int]:
    """找出被后续已出具的更正记录推翻的检查记录 id。"""
    invalidated: set[int] = set()
    for row in rows:
        if not is_correction(row) or not correction_is_effective(row):
            continue
        row_id = as_int(row.get("id"))
        for target in correction_source_ids(row):
            if row_id is None or target < row_id:
                invalidated.add(target)
    for row in rows:
        if (row.get("已更正") or row.get("更正") is True) and correction_is_effective(row):
            row_id = as_int(row.get("id"))
            if row_id is not None:
                invalidated.add(row_id)
    return invalidated


def record_trace_error(
    row: dict[str, Any], process: str, item: str, all_rows: list[dict[str, Any]]
) -> str | None:
    """复检记录追溯校验；返回 None 表示可计入，否则返回原因。"""
    if not is_recheck(row):
        return None
    sources = recheck_source_ids(row)
    if not sources:
        return "复检记录未关联原整改记录"
    targets = [target for target in all_rows if as_int(target.get("id")) in sources]
    if not targets:
        return "复检记录关联的原整改记录不存在"
    same_process = [
        target for target in targets
        if str(target.get("检查工序") or "") == str(process or "")
    ]
    if not same_process:
        return "复检记录关联的整改记录不属于本工序"
    if not any(item_matches_record(item, target) for target in same_process):
        return "复检记录关联的整改记录不是本检查项目"
    return None


def _record_sort_key(row: dict[str, Any]) -> tuple[int, int]:
    rid = as_int(row.get("id")) or 0
    return (rid, 1 if is_recheck(row) else 0)


def _underlying_recheck(
    row: dict[str, Any], all_rows: list[dict[str, Any]]
) -> int | None:
    """adopted 记录本身是复检，或它更正的那条是复检时，返回那次复检的 id。"""
    if is_recheck(row):
        return as_int(row.get("id"))
    if is_correction(row):
        ids = set(correction_source_ids(row))
        for other in all_rows:
            if as_int(other.get("id")) in ids and is_recheck(other):
                return as_int(other.get("id"))
    return None


def build_coverage(
    entry: dict[str, Any], all_rows: list[dict[str, Any]]
) -> dict[str, Any]:
    """计算一张验收单的覆盖情况。

    返回的 coverage 中保存的是 store 里的原始记录引用，调用方可直接读取 id。
    """
    process = str(entry.get("验收工序") or "")
    rows = [
        row for row in all_rows
        if str(row.get("检查工序") or "") == process
    ]
    required, structured = parse_required_items(entry, rows)
    invalidated = _later_correction_ids(rows)
    ignored: list[dict[str, Any]] = []
    valid_rows: list[dict[str, Any]] = []
    for row in rows:
        row_id = as_int(row.get("id"))
        if row_id is not None and row_id in invalidated:
            ignored.append({"id": row_id, "检查项目": row.get("检查项目"), "原因": "已被更正记录推翻"})
            continue
        if is_recheck(row):
            error = record_trace_error(row, process, str(row.get("检查项目") or ""), all_rows)
            if error:
                ignored.append({"id": row_id, "检查项目": row.get("检查项目"), "原因": error})
                continue
        valid_rows.append(row)

    coverage: dict[str, dict[str, Any]] = {}
    failed: list[str] = []
    used_ids: set[int] = set()

    if structured:
        items = required
        for item in items:
            candidates = [
                row for row in valid_rows
                if item_matches_record(item, row)
                and (as_int(row.get("id")) not in used_ids)
            ]
            passed = [row for row in candidates if is_passed(row)]
            if passed:
                chosen = max(passed, key=_record_sort_key)
                coverage[item] = chosen
                chosen_id = as_int(chosen.get("id"))
                if chosen_id is not None:
                    used_ids.add(chosen_id)
            else:
                failed.append(item)
    else:
        # 兼容模式：标准没有逐项列举，只要求至少一条本工序的合格记录。
        items = []
        passed = [row for row in valid_rows if is_passed(row)]
        if passed:
            for row in sorted(passed, key=_record_sort_key):
                item = str(row.get("检查项目") or "").strip() or f"检查记录#{row.get('id')}"
                if item not in coverage:
                    coverage[item] = row
                    row_id = as_int(row.get("id"))
                    if row_id is not None:
                        used_ids.add(row_id)
            items = list(coverage.keys())
        else:
            failed.append("质量检查")

    adopted_checks = {item: as_int(row.get("id")) for item, row in coverage.items()}
    adopted_rechecks = {
        item: _underlying_recheck(row, all_rows)
        for item, row in coverage.items()
        if _underlying_recheck(row, all_rows) is not None
    }
    adopted_recheck_ids = [rid for rid in adopted_rechecks.values() if rid is not None]
    trace_details: dict[str, dict[str, Any]] = {}
    for item, row in coverage.items():
        detail = {
            "检查记录": as_int(row.get("id")),
            "检查项目": row.get("检查项目"),
            "检查结果": row.get("检查结果"),
            "状态": row.get("status"),
            "复检": is_recheck(row),
        }
        if is_recheck(row):
            detail["复检来源"] = recheck_source_ids(row)
        if is_correction(row):
            detail["更正来源"] = correction_source_ids(row)
        trace_details[item] = detail

    pending_records: list[dict[str, Any]] = []
    for row in rows:
        status = str(row.get("status") or "")
        is_open = status in {"需整改", "待复检", "不合格"} or bool(row.get("待复检"))
        if not is_open:
            continue
        # 已被可追溯的合格复检结果解决的整改，不再挂在待复检清单里
        if status in {"需整改", "待复检", "不合格"} and resolved_by_recheck(row, rows):
            continue
        pending_records.append({
            "检查记录": as_int(row.get("id")),
            "检查项目": row.get("检查项目"),
            "状态": status,
            "不合格项": row.get("不合格项", ""),
        })

    if structured:
        ok = not failed
    else:
        ok = bool(coverage)

    return {
        "ok": ok,
        "process": process,
        "structured": structured,
        "required_items": items,
        "covered_items": list(coverage.keys()),
        "coverage": coverage,
        "adopted_checks": adopted_checks,
        "adopted_rechecks": adopted_rechecks,
        "adopted_recheck_ids": adopted_recheck_ids,
        "trace_details": trace_details,
        "missing_items": failed if structured else [],
        "failed_items": failed if structured else [],
        "pending_records": pending_records,
        "ignored_rechecks": ignored,
        "legacy": not structured,
    }


def coverage_reason(coverage: dict[str, Any]) -> str:
    process = coverage.get("process") or "当前工序"
    if coverage.get("ok"):
        return ""
    if coverage.get("legacy"):
        return f"工序「{process}」关联质量检查未通过，无法签署验收"
    missing = coverage.get("missing_items") or []
    detail = "、".join(str(item) for item in missing)
    return (
        f"工序「{process}」验收未通过：验收标准要求的检查项目未全部覆盖，"
        f"缺项/未通过：{detail}"
    )
