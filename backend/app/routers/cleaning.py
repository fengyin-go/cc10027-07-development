"""数据清洗接口：维护清洗规则，覆盖提交校验、启用配置、停用配置等动作。

清洗后数据集的执行接口也挂在本路由：批次（创建/启动/恢复/归档）、
清洗后数据集（已发布部分）、异常回查与质量报告。
"""
from __future__ import annotations

import csv
import io

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntryPayload, PageResult
from app.services.cleaning import CleaningRunService, CleaningService

router = APIRouter(prefix="/api/cleaning", tags=["数据清洗"])

service = CleaningService()
run_service = CleaningRunService()

# 启动恢复：重放未完成的落库日志（结果与质量报告两侧一致），中断的运行批次标记为失败
run_service.recover_pending_commits()
run_service.recover_interrupted_batches()

LIST_FIELDS = ["清洗编号", "输入数据集", "空值策略", "格式表达式", "异常阈值", "输出字段", "规则版本", "生效日期"]
STATUSES = ["草稿", "待校验", "已启用", "已停用"]


def query_filters(request: Request) -> dict[str, str]:
    """只接受当前模块声明过的字段，忽略分页等保留参数。"""
    return {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in LIST_FIELDS and value.strip()
    }


@router.get("", response_model=PageResult[dict])
def list_entries(
    request: Request,
    keyword: str | None = Query(default=None, description="按清洗编号检索"),
    status: str | None = Query(default=None, description="草稿、待校验、已启用、已停用"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """按页面字段、清洗编号关键字与状态过滤数据清洗列表。"""
    items, total = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries(
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> StreamingResponse:
    """下载当前筛选条件下的 UTF-8 CSV；必须声明在 /{entry_id} 之前。"""
    items, _ = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=1, size=1000000)
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *LIST_FIELDS, "status"])
    for item in items:
        row = []
        for value in [item.get("id"), *(item.get(field, "") for field in LIST_FIELDS), item.get("status", "")]:
            text = "" if value is None else str(value)
            row.append("'" + text if text.startswith(("=", "+", "-", "@")) else text)
        writer.writerow(row)
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="cleaning.csv"'})


# ---- 清洗后数据集：输入回查、批次执行、质量报告（均需声明在 /{entry_id} 之前） ----


@router.get("/inputs/{dataset_name}", response_model=PageResult[dict])
def list_input_rows(
    dataset_name: str,
    page: int = Query(default=1, ge=1),
    size: int = Query(default=50, ge=1, le=500),
) -> PageResult[dict]:
    """回查输入数据集的原始行（如 input_customer）。"""
    rows = run_service.list_input_rows(dataset_name)
    start = (page - 1) * size
    return PageResult(items=rows[start:start + size], total=len(rows), page=page, size=size)


@router.post("/runs", response_model=ActionResult)
def create_run(payload: dict[str, object]) -> ActionResult:
    """为指定清洗编号创建清洗批次（草稿）；规则版本冲突时按生效日期最新取舍。"""
    batch, message = run_service.create_run(str(payload.get("清洗编号") or ""))
    if batch is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=batch)


@router.get("/runs", response_model=PageResult[dict])
def list_runs(
    清洗编号: str | None = Query(default=None),
    status: str | None = Query(default=None, description="草稿、运行、完成、失败、归档"),
) -> PageResult[dict]:
    """清洗批次列表：状态按草稿→运行→完成/失败→归档单向流转。"""
    items = run_service.list_batches(清洗编号=清洗编号, status=status)
    return PageResult(items=items, total=len(items), page=1, size=max(len(items), 1))


@router.get("/runs/{batch_id}", response_model=dict)
def get_run(batch_id: int) -> dict:
    """批次详情：游标、失败行号、版本决策与清洗调用链（可顺链找到受影响步骤）。"""
    batch = run_service.get_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"清洗批次 {batch_id} 不存在")
    return batch


@router.post("/runs/{batch_id}/start", response_model=ActionResult)
def start_run(batch_id: int) -> ActionResult:
    """启动批次：草稿 → 运行，逐行清洗并在每条记录后推进游标。"""
    batch, message = run_service.start_run(batch_id)
    if batch is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=batch.get("status") == "完成", message=message, entry=batch)


@router.post("/runs/{batch_id}/resume", response_model=ActionResult)
def resume_run(batch_id: int) -> ActionResult:
    """从失败记录续做：恢复不重置游标，前序成功记录不重复清洗、不重复导出。"""
    batch, message = run_service.resume_run(batch_id)
    if batch is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=batch.get("status") == "完成", message=message, entry=batch)


@router.post("/runs/{batch_id}/archive", response_model=ActionResult)
def archive_run(batch_id: int) -> ActionResult:
    """归档批次：归档是终态，归档后不允许切换规则版本。"""
    batch, message = run_service.archive_run(batch_id)
    if batch is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=batch)


@router.get("/runs/{batch_id}/records", response_model=PageResult[dict])
def run_records(
    batch_id: int,
    全部执行段: bool = Query(default=False, description="默认只看当前执行段"),
) -> PageResult[dict]:
    """当前执行段的输出记录：恢复后的新执行批次只包含从失败行开始的记录。"""
    rows = run_service.batch_records(batch_id, current_segment_only=not 全部执行段)
    if rows is None:
        raise HTTPException(status_code=404, detail=f"清洗批次 {batch_id} 不存在")
    return PageResult(items=rows, total=len(rows), page=1, size=max(len(rows), 1))


@router.get("/datasets/{code}", response_model=PageResult[dict])
def dataset_records(
    code: str,
    发布状态: str | None = Query(default="已发布", description="默认只给已发布部分"),
) -> PageResult[dict]:
    """清洗后数据集：每条记录保留清洗编号、规则版本和异常处理结果。"""
    rows = run_service.dataset_records(code, 发布状态=发布状态)
    return PageResult(items=rows, total=len(rows), page=1, size=max(len(rows), 1))


@router.get("/datasets/{code}/anomalies", response_model=PageResult[dict])
def dataset_anomalies(code: str) -> PageResult[dict]:
    """异常与失败记录回查：保留原始值、规则版本和行号，供质量人员回查。"""
    rows = run_service.dataset_anomalies(code)
    return PageResult(items=rows, total=len(rows), page=1, size=max(len(rows), 1))


@router.get("/reports", response_model=PageResult[dict])
def list_reports(清洗编号: str | None = Query(default=None)) -> PageResult[dict]:
    """质量报告：保留异常阈值与生效日期，规则版本变化时另起版本。"""
    rows = run_service.list_reports(清洗编号=清洗编号)
    return PageResult(items=rows, total=len(rows), page=1, size=max(len(rows), 1))


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条清洗规则明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"清洗规则 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: dict[str, object]) -> ActionResult:
    """登记一条清洗规则，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="清洗规则已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    """对单条清洗规则执行提交校验、启用配置、停用配置；不允许的动作会被拦下并说明原因。"""
    entry, message = service.run_action(entry_id, payload.action.strip())
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
