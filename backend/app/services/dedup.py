"""重复消除业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.store import store

MODULE = "dedup"
LIST_FIELDS = ["策略编号", "输入数据集", "业务键列", "排序字段", "保留顺序", "时间窗口", "重复阈值", "输出数据集"]
REQUIRED_FIELDS = ["策略编号", "输入数据集", "业务键列"]
STATUS_ORDER = ["草稿", "待校验", "已启用", "已停用"]
ACTION_RULES = {"提交校验": "待校验", "启用配置": "已启用", "停用配置": "已停用"}
NEGATIVE_ACTIONS = ["停用配置"]

# 清洗模块的运行态内部表（只读）与去重重算结果表
BATCH_TABLE = "_cleaning_batch"
DATASET_TABLE = "_cleaning_dataset"
RESULT_TABLE = "_dedup_result"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class DedupService:
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
            rows = [row for row in rows if keyword.casefold() in str(row.get("策略编号", "")).casefold()]
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
            return None, f"去重策略 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于重复消除可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"去重策略已{action}"

    def list_results(self, *, 策略编号: str | None = None) -> list[dict[str, Any]]:
        rows = list(store.rows(RESULT_TABLE))
        if 策略编号:
            rows = [row for row in rows if row.get("策略编号") == 策略编号]
        return sorted(rows, key=lambda row: int(row.get("id", 0)))

    def recompute(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """重算清洗后数据集：只读取已发布部分，且任何时刻只接收一代已发布输出。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"去重策略 {entry_id} 不存在或已归档"
        输入数据集 = str(entry.get("输入数据集") or "")
        策略编号 = str(entry.get("策略编号") or "")
        batches = [row for row in store.rows(BATCH_TABLE) if row.get("输出数据集") == 输入数据集]
        清洗编号s = {row.get("清洗编号") for row in batches}
        published = [
            row for row in store.rows(DATASET_TABLE)
            if row.get("清洗编号") in 清洗编号s and row.get("发布状态") == "已发布"
        ]
        if not published:
            return None, f"数据集 {输入数据集} 没有已发布的清洗结果：去重模块只读取已发布部分，临时结果不参与重算"
        generations = {int(row.get("发布代次") or 0) for row in published}
        if len(generations) > 1:
            return None, "检测到多代已发布输出并存，去重模块不会同时接收两版输出，请先完成发布收敛"
        generation = next(iter(generations))
        versions = sorted({str(row.get("规则版本")) for row in published})
        key_field = str(entry.get("业务键列") or "")
        kept: dict[str, dict[str, Any]] = {}
        duplicates = 0
        for record in sorted(published, key=lambda row: int(row.get("行号", 0))):
            key = str(record.get(key_field) or record.get("原始行标识") or "")
            if key in kept:
                duplicates += 1  # 保留最早一行
                continue
            kept[key] = record
        results = store.rows(RESULT_TABLE)
        results[:] = [row for row in results if row.get("策略编号") != 策略编号]
        base_id = max((int(row.get("id", 0)) for row in results), default=0)
        for offset, record in enumerate(kept.values(), start=1):
            results.append({
                "id": base_id + offset,
                "策略编号": 策略编号,
                "输入数据集": 输入数据集,
                "清洗编号": record.get("清洗编号"),
                "发布代次": generation,
                "规则版本": record.get("规则版本"),
                "原始行标识": record.get("原始行标识"),
                "业务键值": str(record.get(key_field) or record.get("原始行标识") or ""),
                "异常处理结果": record.get("异常处理结果"),
                "重算时间": _now(),
            })
        # 顺着清洗调用链回写：最近完成批次的「去重重算」步骤标记已完成
        done = [row for row in batches if row.get("status") == "完成"]
        if done:
            latest = max(done, key=lambda row: int(row.get("id", 0)))
            for step in latest.get("调用链", []):
                if step.get("步骤") == "去重重算":
                    step["状态"] = "已完成"
                    step["说明"] = f"{策略编号} 已重算第 {generation} 代已发布输出"
        summary = {
            "策略编号": 策略编号,
            "输入数据集": 输入数据集,
            "发布代次": generation,
            "规则版本": "、".join(versions),
            "读取已发布": len(published),
            "去重后": len(kept),
            "重复剔除": duplicates,
        }
        return summary, (
            f"去重重算完成：读取已发布 {len(published)} 条（第 {generation} 代，"
            f"规则版本 {'、'.join(versions)}），去重后 {len(kept)} 条，剔除重复 {duplicates} 条"
        )
