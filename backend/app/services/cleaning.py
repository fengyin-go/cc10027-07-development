"""数据清洗业务规则：状态流转、字段校验与筛选口径都收在这里。

清洗后数据集的执行语义（对应 CLEA-0018 场景）也收在本模块：

- 批次状态机：草稿 → 运行 → 完成 / 失败，单向流转；失败可恢复运行
  （恢复不重置游标与临时结果，不回到草稿，恢复后保持当前状态继续推进）；
  完成 / 失败可归档，归档是终态，归档后不允许再切换规则版本。
- 规则版本冲突：同一清洗编号存在多个已启用版本时，采用生效日期最新的一版
  （生效日期相同取规则版本号较大者），决策理由写入批次日志。
- 断点续做：每处理完一条记录即推进游标；表达式错误中断时，已成功的记录
  保留在临时结果中不发布，修复规则版本后从失败行继续，前序成功记录不重复
  清洗、不重复导出。
- 落库一致性：结果发布与质量报告通过同一条落库日志提交；恢复时重放
  「待提交」日志，保证结果与报告两侧一致。
"""
from __future__ import annotations

import copy
import re
from datetime import datetime
from typing import Any

from app.store import store

MODULE = "cleaning"
LIST_FIELDS = ["清洗编号", "输入数据集", "空值策略", "格式表达式", "异常阈值", "输出字段", "规则版本", "生效日期"]
REQUIRED_FIELDS = ["清洗编号", "输入数据集", "空值策略"]
STATUS_ORDER = ["草稿", "待校验", "已启用", "已停用"]
ACTION_RULES = {"提交校验": "待校验", "启用配置": "已启用", "停用配置": "已停用"}
NEGATIVE_ACTIONS = ["停用配置"]

# 清洗执行的运行态内部表（下划线前缀：不进入运营概览的模块卡片）
INPUT_TABLE = "_cleaning_input"      # 输入数据集，按行存放
DATASET_TABLE = "_cleaning_dataset"  # 清洗后数据集：临时 / 已发布 / 已替换 / 失败留存
BATCH_TABLE = "_cleaning_batch"      # 清洗批次
REPORT_TABLE = "_cleaning_report"    # 质量报告
JOURNAL_TABLE = "_cleaning_journal"  # 落库日志：结果与报告的一致性保障

# 批次状态机：只允许沿箭头单向流转；失败 → 运行是恢复分支，不重置进度。
BATCH_STATUS_FLOW: dict[str, list[str]] = {
    "草稿": ["运行"],
    "运行": ["完成", "失败"],
    "失败": ["运行", "归档"],
    "完成": ["归档"],
    "归档": [],
}

NULL_FILL_DEFAULT = "1900-01-01"  # 空值补全策略的填充值
DATE_FORMATS = {"YYYY-MM-DD": "%Y-%m-%d", "YYYY/MM/DD": "%Y/%m/%d", "YYYYMMDD": "%Y%m%d"}
CHAIN_STEPS = ["读取输入数据集", "空值补全", "格式校验", "结果发布", "质量报告", "去重重算"]


class CleaningService:
    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        for field, expected in (filters or {}).items():
            rows = [row for row in rows if expected.casefold() in str(row.get(field, "")).casefold()]
        if keyword:
            rows = [row for row in rows if keyword.casefold() in str(row.get("清洗编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in LIST_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"清洗规则 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于数据清洗可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"清洗规则已{action}"


class ExpressionError(Exception):
    """格式表达式本身执行失败（区别于数据不合法）：触发清洗流程中断。"""

    def __init__(self, message: str, *, 行号: int, 原始值: Any, 原始行标识: str) -> None:
        super().__init__(message)
        self.行号 = 行号
        self.原始值 = 原始值
        self.原始行标识 = 原始行标识


def _next_id(table: str) -> int:
    return max((int(row.get("id", 0)) for row in store.rows(table)), default=0) + 1


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _fresh_chain() -> list[dict[str, Any]]:
    return [{"步骤": name, "状态": "待执行", "说明": ""} for name in CHAIN_STEPS]


def _parse_date(value: str, fmt: str) -> bool:
    template = DATE_FORMATS.get(fmt.upper())
    if template is None:
        return False
    try:
        datetime.strptime(value, template)
    except ValueError:
        return False
    return True


def _evaluate_format(expression: str, value: str, *, 行号: int, 原始行标识: str) -> tuple[bool, str]:
    """校验非空值是否满足格式表达式，返回 (是否合法, 异常原因)。

    strict_ 前缀表示严格模式：表达式无法处理该值时抛 ExpressionError 中断流程
    （CLEA-0018 的 V1 即 strict_date，遇到无法解析的日期直接中断）。
    """
    expr = expression.strip()
    strict = expr.startswith("strict_")
    body = expr[len("strict_"):] if strict else expr
    if body.startswith("date:"):
        fmt = body[len("date:"):].strip() or "YYYY-MM-DD"
        if _parse_date(value, fmt):
            return True, ""
        reason = "无法解析日期" if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) else "格式错误"
        if strict:
            raise ExpressionError(
                f"格式表达式 strict_date:{fmt} 无法解析日期值「{value}」",
                行号=行号, 原始值=value, 原始行标识=原始行标识,
            )
        return False, reason
    pattern = body[len("re:"):] if body.startswith("re:") else body
    try:
        matched = re.fullmatch(pattern, value) is not None
    except re.error as exc:
        raise ExpressionError(
            f"格式表达式语法错误：{exc}", 行号=行号, 原始值=value, 原始行标识=原始行标识,
        )
    return (True, "") if matched else (False, "格式错误")


class CleaningRunService:
    """清洗批次执行：读取输入数据集逐行清洗，产出清洗后数据集与质量报告。"""

    # ---------- 查询 ----------

    def list_batches(self, *, 清洗编号: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        rows = list(store.rows(BATCH_TABLE))
        if 清洗编号:
            rows = [row for row in rows if 清洗编号.casefold() in str(row.get("清洗编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        return sorted(rows, key=lambda row: int(row.get("id", 0)))

    def get_batch(self, batch_id: int) -> dict[str, Any] | None:
        return store.find(BATCH_TABLE, batch_id)

    def batch_records(self, batch_id: int, *, current_segment_only: bool = True) -> list[dict[str, Any]] | None:
        """批次输出记录：默认只看当前执行段（恢复后的新执行批次不含前序记录）。"""
        batch = store.find(BATCH_TABLE, batch_id)
        if batch is None:
            return None
        rows = [row for row in store.rows(DATASET_TABLE) if row.get("批次号") == batch.get("批次号")]
        if current_segment_only:
            rows = [row for row in rows if int(row.get("执行段", 0)) == int(batch.get("执行段", 0))]
        return sorted(rows, key=lambda row: (int(row.get("行号", 0)), int(row.get("执行段", 0))))

    def dataset_records(self, 清洗编号: str, *, 发布状态: str | None = "已发布") -> list[dict[str, Any]]:
        """清洗后数据集：默认只给已发布部分（与去重模块同口径）。"""
        rows = [row for row in store.rows(DATASET_TABLE) if row.get("清洗编号") == 清洗编号]
        if 发布状态:
            rows = [row for row in rows if row.get("发布状态") == 发布状态]
        return sorted(rows, key=lambda row: (int(row.get("行号", 0)), int(row.get("执行段", 0))))

    def dataset_anomalies(self, 清洗编号: str) -> list[dict[str, Any]]:
        """异常与失败记录回查：保留原始值、规则版本和行号，供质量人员回查。"""
        rows = [
            row for row in store.rows(DATASET_TABLE)
            if row.get("清洗编号") == 清洗编号 and row.get("异常处理结果") in ("异常", "失败")
        ]
        return sorted(rows, key=lambda row: (int(row.get("行号", 0)), int(row.get("执行段", 0))))

    def list_reports(self, *, 清洗编号: str | None = None) -> list[dict[str, Any]]:
        rows = list(store.rows(REPORT_TABLE))
        if 清洗编号:
            rows = [row for row in rows if row.get("清洗编号") == 清洗编号]
        return sorted(rows, key=lambda row: int(row.get("id", 0)))

    def list_input_rows(self, 数据集名称: str) -> list[dict[str, Any]]:
        rows = [row for row in store.rows(INPUT_TABLE) if row.get("数据集名称") == 数据集名称]
        return sorted(rows, key=lambda row: int(row.get("行号", 0)))

    # ---------- 规则版本取舍 ----------

    def resolve_rule_version(self, 清洗编号: str) -> tuple[dict[str, Any] | None, str | None]:
        """两个规则版本冲突时，采用生效日期最新的一版（相同则取版本号较大者）。"""
        candidates = [
            row for row in store.rows(MODULE)
            if row.get("清洗编号") == 清洗编号 and row.get("status") == "已启用"
        ]
        if not candidates:
            return None, None
        ordered = sorted(
            candidates,
            key=lambda row: (str(row.get("生效日期") or ""), str(row.get("规则版本") or "")),
        )
        chosen = ordered[-1]
        if len(ordered) == 1:
            return chosen, None
        discarded = "、".join(str(row.get("规则版本")) for row in ordered[:-1])
        return chosen, (
            f"清洗编号 {清洗编号} 存在 {len(ordered)} 个已启用规则版本（{discarded}、{chosen.get('规则版本')}）冲突，"
            f"采用生效日期最新的 {chosen.get('规则版本')}（生效日期 {chosen.get('生效日期')}）"
        )

    # ---------- 批次动作 ----------

    def create_run(self, 清洗编号: str) -> tuple[dict[str, Any] | None, str]:
        清洗编号 = (清洗编号 or "").strip()
        if not 清洗编号:
            return None, "缺少清洗编号"
        active = [
            row for row in store.rows(BATCH_TABLE)
            if row.get("清洗编号") == 清洗编号 and row.get("status") != "归档"
        ]
        if active:
            return None, f"清洗编号 {清洗编号} 存在未归档批次 {active[0].get('批次号')}，请先归档再新建批次"
        rule, decision = self.resolve_rule_version(清洗编号)
        if rule is None:
            return None, f"清洗编号 {清洗编号} 没有已启用的规则版本，无法创建批次"
        batch_id = _next_id(BATCH_TABLE)
        batch = {
            "id": batch_id,
            "批次号": f"RUN-{batch_id:04d}",
            "清洗编号": 清洗编号,
            "输入数据集": rule.get("输入数据集"),
            "输出数据集": f"cleaned_{rule.get('输入数据集')}",
            "空值策略": rule.get("空值策略"),
            "格式表达式": rule.get("格式表达式"),
            "异常阈值": rule.get("异常阈值"),
            "规则版本": rule.get("规则版本"),
            "生效日期": rule.get("生效日期"),
            "status": "草稿",
            "游标": 1,
            "执行段": 0,
            "失败行号": None,
            "失败原因": "",
            "版本决策": [decision] if decision else [f"采用已启用规则版本 {rule.get('规则版本')}"],
            "调用链": _fresh_chain(),
            "pending": True,
            "abnormal": False,
            "创建时间": _now(),
            "完成时间": None,
        }
        store.rows(BATCH_TABLE).append(batch)
        return batch, f"清洗批次 {batch['批次号']} 已创建（规则版本 {batch['规则版本']}，状态：草稿）"

    def start_run(self, batch_id: int) -> tuple[dict[str, Any] | None, str]:
        batch = store.find(BATCH_TABLE, batch_id)
        if batch is None:
            return None, f"清洗批次 {batch_id} 不存在"
        message = self._transition(batch, "运行")
        if message:
            return None, message
        batch["执行段"] = int(batch.get("执行段", 0)) + 1
        return self._execute(batch)

    def resume_run(self, batch_id: int) -> tuple[dict[str, Any] | None, str]:
        """从失败记录续做：恢复不重置游标与临时结果，前序成功记录不重复清洗。"""
        batch = store.find(BATCH_TABLE, batch_id)
        if batch is None:
            return None, f"清洗批次 {batch_id} 不存在"
        if batch.get("status") == "归档":
            return None, "批次已归档，不允许切换规则版本或恢复运行"
        if batch.get("status") != "失败":
            return None, f"只有失败批次才能恢复运行（当前状态：{batch.get('status')}）"
        rule, decision = self.resolve_rule_version(str(batch.get("清洗编号")))
        if rule is None:
            return None, f"清洗编号 {batch.get('清洗编号')} 没有已启用的规则版本，无法恢复"
        if rule.get("规则版本") != batch.get("规则版本"):
            batch["版本决策"].append(
                decision
                or f"恢复时切换规则版本：{batch.get('规则版本')} → {rule.get('规则版本')}（生效日期最新）"
            )
            batch["规则版本"] = rule.get("规则版本")
            batch["格式表达式"] = rule.get("格式表达式")
            batch["空值策略"] = rule.get("空值策略")
            batch["异常阈值"] = rule.get("异常阈值")
            batch["生效日期"] = rule.get("生效日期")
        else:
            batch["版本决策"].append(f"恢复运行，沿用规则版本 {batch.get('规则版本')}")
        self._transition(batch, "运行")  # 失败 → 运行：保持当前进度，不回到草稿
        batch["执行段"] = int(batch.get("执行段", 0)) + 1
        batch["失败行号"] = None
        batch["失败原因"] = ""
        return self._execute(batch)

    def archive_run(self, batch_id: int) -> tuple[dict[str, Any] | None, str]:
        batch = store.find(BATCH_TABLE, batch_id)
        if batch is None:
            return None, f"清洗批次 {batch_id} 不存在"
        message = self._transition(batch, "归档")
        if message:
            return None, f"{message}；归档后不允许切换规则版本"
        batch["pending"] = False
        return batch, f"清洗批次 {batch['批次号']} 已归档，规则版本 {batch['规则版本']} 已冻结"

    # ---------- 状态机 ----------

    @staticmethod
    def _transition(batch: dict[str, Any], target: str) -> str | None:
        current = str(batch.get("status", "草稿"))
        if target not in BATCH_STATUS_FLOW.get(current, []):
            return f"批次状态不允许从「{current}」流转到「{target}」（单向流转：草稿→运行→完成/失败→归档）"
        batch["status"] = target
        return None

    # ---------- 执行引擎 ----------

    def _execute(self, batch: dict[str, Any]) -> tuple[dict[str, Any], str]:
        segment = int(batch.get("执行段", 0))
        start_cursor = int(batch.get("游标", 1))
        stats = {"处理": 0, "成功": 0, "异常": 0, "跳过": start_cursor - 1}
        input_rows = self.list_input_rows(str(batch.get("输入数据集")))
        self._chain_set(batch, "读取输入数据集", "完成", f"读取 {len(input_rows)} 行")
        self._chain_set(batch, "空值补全", "运行", "")
        self._chain_set(batch, "格式校验", "运行", "")
        try:
            for row in input_rows:
                行号 = int(row.get("行号", 0))
                if 行号 < start_cursor:
                    continue  # 前序成功记录不重复清洗
                record = self._clean_row(batch, row, segment)
                if record is None:
                    stats["跳过"] += 1  # 空值剔除策略：不落结果，计入跳过数量
                else:
                    store.rows(DATASET_TABLE).append(record)
                    stats["处理"] += 1
                    stats["成功" if record["异常处理结果"] == "成功" else "异常"] += 1
                batch["游标"] = 行号 + 1  # 每条记录处理完即推进游标
        except ExpressionError as exc:
            # 表达式错误：流程中断，已成功的记录保留在临时结果中不发布
            store.rows(DATASET_TABLE).append({
                "id": _next_id(DATASET_TABLE),
                "清洗编号": batch["清洗编号"],
                "批次号": batch["批次号"],
                "执行段": segment,
                "规则版本": batch["规则版本"],
                "行号": exc.行号,
                "原始行标识": exc.原始行标识,
                "customer_id": "",
                "customer_name": "",
                "原始值": exc.原始值,
                "清洗后值": "",
                "异常处理结果": "失败",
                "处理说明": "",
                "异常原因": f"表达式错误：{exc}",
                "发布状态": "失败留存",
                "发布代次": None,
                "创建时间": _now(),
            })
            batch["失败行号"] = exc.行号
            batch["失败原因"] = str(exc)
            self._chain_set(batch, "空值补全", "完成", f"已处理 {stats['处理']} 行")
            self._chain_set(batch, "格式校验", "失败", f"第 {exc.行号} 行：{exc}")
            self._chain_set(batch, "结果发布", "未触发", "流程中断，临时结果不发布")
            self._chain_set(batch, "去重重算", "未触发", "等待清洗完成后重算")
            self._commit_segment(batch, segment, start_cursor, stats, final="失败")
            return batch, (
                f"清洗流程在第 {exc.行号} 条中断：{exc}；"
                f"前 {exc.行号 - 1} 条成功记录保留在临时结果中不发布，"
                f"修复规则版本后可从第 {exc.行号} 条续做"
            )
        self._chain_set(batch, "空值补全", "完成", f"已处理 {stats['处理']} 行")
        self._chain_set(batch, "格式校验", "完成", f"本段异常 {stats['异常']} 行")
        threshold = self._threshold(batch.get("异常阈值"))
        total_anomalies = len([
            row for row in store.rows(DATASET_TABLE)
            if row.get("批次号") == batch.get("批次号") and row.get("异常处理结果") == "异常"
        ])
        if threshold > 0 and total_anomalies > threshold:
            batch["失败原因"] = f"异常数 {total_anomalies} 超过异常阈值 {threshold}"
            self._chain_set(batch, "结果发布", "未触发", "异常超阈值，临时结果不发布")
            self._chain_set(batch, "去重重算", "未触发", "等待清洗完成后重算")
            self._commit_segment(batch, segment, start_cursor, stats, final="失败")
            return batch, f"清洗批次 {batch['批次号']} 质量阻断：{batch['失败原因']}，临时结果保留待处理"
        self._commit_segment(batch, segment, start_cursor, stats, final="完成")
        return batch, (
            f"清洗批次 {batch['批次号']} 执行完成：本段处理 {stats['处理']} 条"
            f"（成功 {stats['成功']}、异常 {stats['异常']}），跳过前序 {stats['跳过']} 条"
        )

    def _clean_row(self, batch: dict[str, Any], row: dict[str, Any], segment: int) -> dict[str, Any] | None:
        行号 = int(row.get("行号", 0))
        original = row.get("register_date")
        value = "" if original is None else str(original).strip()
        结果, 原因, 说明, cleaned = "成功", "", "格式校验通过", value
        if not value:
            strategy = str(batch.get("空值策略") or "")
            if strategy == "空值补全":
                cleaned, 说明 = NULL_FILL_DEFAULT, "空值补全"
            elif strategy == "空值剔除":
                return None
            else:
                结果, 原因, 说明, cleaned = "异常", f"空值策略「{strategy}」未支持", "", ""
        else:
            # 非空值一律过格式表达式：非空但格式错误的记录进入异常分支，不能默认为成功
            ok, 原因 = _evaluate_format(
                str(batch.get("格式表达式") or ""), value,
                行号=行号, 原始行标识=str(row.get("原始行标识", "")),
            )
            if not ok:
                结果, 说明, cleaned = "异常", "", value  # 异常记录保留原始值
        return {
            "id": _next_id(DATASET_TABLE),
            "清洗编号": batch["清洗编号"],
            "批次号": batch["批次号"],
            "执行段": segment,
            "规则版本": batch["规则版本"],
            "行号": 行号,
            "原始行标识": row.get("原始行标识", ""),
            "customer_id": row.get("customer_id", ""),
            "customer_name": row.get("customer_name", ""),
            "原始值": original if original is not None else "",
            "清洗后值": cleaned,
            "异常处理结果": 结果,
            "处理说明": 说明,
            "异常原因": 原因,
            "发布状态": "临时",
            "发布代次": None,
            "创建时间": _now(),
        }

    # ---------- 段落提交：结果与质量报告同日志落库 ----------

    def _commit_segment(
        self,
        batch: dict[str, Any],
        segment: int,
        start_cursor: int,
        stats: dict[str, int],
        *,
        final: str,
    ) -> None:
        now = _now()
        report = {
            "id": _next_id(REPORT_TABLE),
            "报告编号": f"QR-{batch['批次号']}-V{segment}",
            "清洗编号": batch["清洗编号"],
            "批次号": batch["批次号"],
            "报告版本": segment,  # 规则版本变化时质量报告另起版本
            "规则版本": batch["规则版本"],
            "生效日期": batch["生效日期"],
            "异常阈值": batch["异常阈值"],
            "开始行号": start_cursor,
            "结束行号": int(batch.get("游标", start_cursor)) - 1 if final == "完成" else batch.get("失败行号"),
            "处理总数": stats["处理"],
            "成功数": stats["成功"],
            "异常数": stats["异常"],
            "跳过数量": stats["跳过"],
            "报告状态": final,
            "失败原因": str(batch.get("失败原因") or "") if final == "失败" else "",
            "创建时间": now,
        }
        publish_changes: list[dict[str, Any]] = []
        if final == "完成":
            # 发布：临时 → 已发布；上一代已发布 → 已替换。任何时刻只有一代已发布输出，
            # 去重模块不会同时收到两版输出。
            generation = max(
                (int(row.get("发布代次") or 0) for row in store.rows(DATASET_TABLE)), default=0,
            ) + 1
            for record in store.rows(DATASET_TABLE):
                if record.get("清洗编号") != batch.get("清洗编号"):
                    continue
                if record.get("发布状态") == "已发布":
                    publish_changes.append({
                        "表": DATASET_TABLE, "操作": "更新", "id": record["id"],
                        "数据": {"发布状态": "已替换"},
                    })
                elif record.get("发布状态") == "临时":
                    publish_changes.append({
                        "表": DATASET_TABLE, "操作": "更新", "id": record["id"],
                        "数据": {"发布状态": "已发布", "发布代次": generation},
                    })
            self._chain_set(batch, "结果发布", "完成", f"清洗后数据集第 {generation} 代已发布")
            self._chain_set(batch, "去重重算", "待重算", "等待去重模块读取已发布部分")
        else:
            self._chain_set(batch, "结果发布", "未触发", "临时结果不发布")
        self._chain_set(batch, "质量报告", "完成", report["报告编号"])
        changes = [
            {"表": REPORT_TABLE, "操作": "新增", "数据": report},
            *publish_changes,
            {"表": BATCH_TABLE, "操作": "更新", "id": batch["id"], "数据": {
                "status": final,
                "游标": batch["游标"],
                "调用链": batch["调用链"],
                "版本决策": batch["版本决策"],
                "失败行号": batch.get("失败行号"),
                "失败原因": str(batch.get("失败原因") or ""),
                "完成时间": now if final == "完成" else None,
                "pending": final != "完成",
                "abnormal": final == "失败",
            }},
        ]
        self._atomic_commit(f"批次 {batch['批次号']} 第 {segment} 段（{final}）", changes)

    def _atomic_commit(self, label: str, changes: list[dict[str, Any]]) -> None:
        """结果写入与质量报告落在同一条提交日志里：两侧要么同时生效，要么都不生效。"""
        journal = store.rows(JOURNAL_TABLE)
        entry = {
            "id": _next_id(JOURNAL_TABLE),
            "标签": label,
            "状态": "待提交",
            "变更": copy.deepcopy(changes),  # 快照：恢复重放不受后续运行影响
            "时间": _now(),
        }
        journal.append(entry)
        self._apply_changes(changes)  # 若中途崩溃，日志停留在「待提交」，恢复时重放
        entry["状态"] = "已提交"

    @staticmethod
    def _apply_changes(changes: list[dict[str, Any]]) -> None:
        for change in changes:
            table = store.rows(str(change["表"]))
            if change["操作"] == "新增":
                data = dict(change["数据"])
                existing = next(
                    (row for row in table if int(row.get("id", 0)) == int(data.get("id", 0))), None,
                )
                if existing is None:
                    table.append(data)
                else:
                    existing.update(data)  # 幂等：恢复重放不会重复追加
            elif change["操作"] == "更新":
                row = next(
                    (row for row in table if int(row.get("id", 0)) == int(change["id"])), None,
                )
                if row is not None:
                    row.update(change["数据"])

    # ---------- 崩溃恢复 ----------

    def recover_pending_commits(self) -> list[str]:
        """重放停留在「待提交」的落库日志，保证结果与质量报告两侧一致。"""
        recovered = []
        for entry in store.rows(JOURNAL_TABLE):
            if entry.get("状态") == "待提交":
                self._apply_changes(entry.get("变更", []))
                entry["状态"] = "已提交"
                recovered.append(str(entry.get("标签", "")))
        return recovered

    def recover_interrupted_batches(self) -> list[str]:
        """进程中断恢复：运行中的批次标记为失败（游标逐条落库，可从失败记录续做）。"""
        recovered = []
        for batch in store.rows(BATCH_TABLE):
            if batch.get("status") == "运行":
                batch["status"] = "失败"
                batch["失败原因"] = str(batch.get("失败原因") or "执行进程中断，批次标记为失败，可恢复运行")
                batch["abnormal"] = True
                recovered.append(str(batch.get("批次号", "")))
        return recovered

    # ---------- 小工具 ----------

    @staticmethod
    def _chain_set(batch: dict[str, Any], step_name: str, status: str, note: str = "") -> None:
        for step in batch.get("调用链", []):
            if step.get("步骤") == step_name:
                step["状态"] = status
                step["说明"] = note
                return

    @staticmethod
    def _threshold(raw: Any) -> int:
        try:
            return int(str(raw or "0").strip())
        except ValueError:
            return 0  # 阈值不可解析时不启用阻断
