#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
labsafe 学习进度诊断（只读优先）
================================

回答两个问题：

1. 我的账号在 labsafe 上到底学了多少、答了多少？（只读，不产生任何写操作）
2. `finishRate` / `finish` 返回 success 之后，服务端的“已完成课程数”有没有真的变化？
   （`--report` 才会真正上报，并回读计数做前后对比）

依据是线上前端 chunk-59452944（courseDetail）的真实实现：

    timeupdate：currentTime % 10 == 0 ->
        POST /jcedutec/courseSource/finishRate {id, graphId, watchDuration}
    ended ->
        POST /jcedutec/courseSource/finish {id, graphId}
    弹题提交 ->
        POST /jcedutec/courseSource/submitAnswer {id, graphId, questionId, option}

用法：

    # 1) 只读盘点：列出树 -> 类型 -> 课程 -> 每个视频的完成/答题计数
    python labsafe_diag.py --token <TOKEN>

    # 2) 只读看单个视频
    python labsafe_diag.py --token <TOKEN> --graph <GRAPH_ID> --id <SOURCE_ID>

    # 3) 真正的上报对照实验（会写进度；先跑 1 和 2 再决定）
    python labsafe_diag.py --token <TOKEN> --graph <G> --id <I> \
        --report --seconds 60 --step 10

判定规则（与 aqzrui 的 getStudyinfo 复核同一思路）：

- 上报前后 `corseSourceFinishCount` 增加 -> 计入成功；
- 计数不变 -> 服务端没把这次上报算进“已完成”，脚本打印每个请求的
  success/code/message，并指出最可能的拦截点。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, Iterable, List, Optional

from labsafe_client import LabsafeClient, LabsafeError, LabsafeAuthError


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------
def _result(j: Dict[str, Any]) -> Any:
    return j.get("result")


def _as_list(v: Any) -> List[Dict[str, Any]]:
    if isinstance(v, list):
        return [x for x in v if isinstance(x, dict)]
    if isinstance(v, dict):
        return [v]
    return []


def _short(j: Dict[str, Any]) -> str:
    """把响应压成一行，保留 success/code/message，便于对照。"""
    return json.dumps({
        "success": j.get("success"),
        "code": j.get("code"),
        "message": j.get("message") or j.get("msg"),
    }, ensure_ascii=False)


def _pick(d: Dict[str, Any], *keys: str) -> Any:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


# --------------------------------------------------------------------------
# 只读：树 -> 类型 -> 课程 -> 计数
# --------------------------------------------------------------------------
def walk_courses(c: LabsafeClient) -> List[Dict[str, Any]]:
    """返回 [{graph, graph_title, type, type_name, course}] 的扁平列表。"""
    rows: List[Dict[str, Any]] = []
    tree = _as_list(_result(c.raw_get("/jcedutec/knowledgeGraph/queryGraphTree", {})))

    for node in tree:
        graph_id = str(_pick(node, "key", "id", "eventKey") or "")
        graph_title = str(_pick(node, "title", "name") or "")
        if not graph_id:
            continue

        tj = c.raw_get("/jcedutec/knowledgeGraph/queryTypeById", {"id": graph_id})
        types = _as_list(_result(tj))
        if not types:
            print(f"  [warn] graph {graph_id} 没取到类型：{_short(tj)}")

        for t in types:
            course_type = str(_pick(t, "type", "id") or "")
            type_name = str(_pick(t, "name", "title") or "")
            if not course_type:
                continue

            cj = c.raw_get("/jcedutec/knowledgeGraph/queryCourseTypeById",
                           {"id": graph_id, "courseType": course_type})
            courses = _as_list(_result(cj))
            if not courses:
                print(f"  [warn] graph={graph_id} type={course_type} 没取到课程：{_short(cj)}")
            for course in courses:
                rows.append({
                    "graph": graph_id,
                    "graph_title": graph_title,
                    "type": course_type,
                    "type_name": type_name,
                    "course": course,
                })
    return rows


def print_inventory(rows: Iterable[Dict[str, Any]]) -> None:
    header = (f"{'graph':>20} {'类型':<12} {'课程ID':>20} {'已完成/总课程':>12} "
              f"{'已答/总题':>10} {'答错':>6}  课程名")
    print(header)
    print("-" * len(header))
    for r in rows:
        course = r["course"]
        cid = str(_pick(course, "id", "courseId") or "")
        done = _pick(course, "corseSourceFinishCount", "courseSourceFinishCount")
        total = _pick(course, "corseSourceCount", "courseSourceCount")
        donum = _pick(course, "donum")
        qtotal = _pick(course, "total")
        wrong = _pick(course, "unCorrectNum")
        name = str(_pick(course, "name", "title") or "")
        print(f"{r['graph']:>20} {r['type_name'][:12]:<12} {cid:>20} "
              f"{str(done):>6}/{str(total):<5} {str(donum):>4}/{str(qtotal):<5} "
              f"{str(wrong):>6}  {name}")


# --------------------------------------------------------------------------
# 只读：单个视频
# --------------------------------------------------------------------------
def inspect_source(c: LabsafeClient, graph_id: str, source_id: str) -> Dict[str, Any]:
    print(f"\n== 视频详情 graph={graph_id} id={source_id} ==")

    mj = c.raw_get("/jcedutec/courseSource/ismyCourse", {"id": source_id})
    print(f"ismyCourse: {_short(mj)}  message={mj.get('message')!r}")

    dj = c.raw_get("/jcedutec/courseSource/getById",
                   {"id": source_id, "graphId": graph_id})
    detail = _result(dj) or {}
    print(f"getById:    {_short(dj)}")
    if isinstance(detail, dict):
        # 原样打印，便于发现 duration / finish 等隐藏字段
        print("getById result:")
        print(json.dumps(detail, ensure_ascii=False, indent=2)[:4000])

    qj = c.raw_get("/jcedutec/courseSource/queryCourseQuestionRelaByMainId",
                   {"id": source_id, "graphId": graph_id})
    questions = _as_list(_result(qj))
    print(f"弹题:       {_short(qj)}  共 {len(questions)} 道")
    for q in questions:
        print("   - id={id} kind={kind} ejectTime={ejectTime} stem={stem}".format(
            id=q.get("id"), kind=q.get("kind"), ejectTime=q.get("ejectTime"),
            stem=str(q.get("stem") or "")[:60]))

    return {"detail": detail, "questions": questions, "ismy": mj}


def guess_duration(detail: Dict[str, Any], questions: List[Dict[str, Any]]) -> Optional[int]:
    for k in ("duration", "allTime", "videoTime", "timeLength", "longTime", "length",
              "totalTime", "courseTime"):
        v = detail.get(k)
        if isinstance(v, (int, float)) and v > 0:
            return int(v)
        if isinstance(v, str) and v.isdigit() and int(v) > 0:
            return int(v)
    # 弹题的弹出时间通常小于总时长，只能当下界
    times = [q.get("ejectTime") for q in questions
             if isinstance(q.get("ejectTime"), (int, float))]
    if times:
        return int(max(times))
    return None


# --------------------------------------------------------------------------
# 上报 + 回读
# --------------------------------------------------------------------------
def snapshot_count(c: LabsafeClient, graph_id: str, course_type: str,
                   course_id: str) -> Optional[Dict[str, Any]]:
    j = c.raw_get("/jcedutec/knowledgeGraph/queryCourseTypeById",
                  {"id": graph_id, "courseType": course_type})
    for course in _as_list(_result(j)):
        if str(_pick(course, "id", "courseId")) == str(course_id):
            return course
    return None


def run_report(c: LabsafeClient, graph_id: str, source_id: str,
               duration: int, step: int, interval: float,
               course_type: Optional[str], course_id: Optional[str],
               answer_all: Optional[str], realtime: bool = False) -> int:
    print("\n== 上报对照实验 ==")
    before = None
    if course_type and course_id:
        before = snapshot_count(c, graph_id, course_type, course_id)
        print("上报前课程计数:", json.dumps(before, ensure_ascii=False) if before else "未找到")

    # 真实播放器会在 ejectTime 暂停并要求答题；先答完再推进更接近真实链路。
    if answer_all:
        print(f"\n[0] submitAnswer（每题都填 {answer_all}，仅用于验证答题链路）")
        qj = c.raw_get("/jcedutec/courseSource/queryCourseQuestionRelaByMainId",
                       {"id": source_id, "graphId": graph_id})
        for q in _as_list(_result(qj)):
            opt: Any = answer_all
            if str(q.get("kind")) == "3":
                opt = [x for x in answer_all.split(",") if x]
            try:
                j = c.submit_answer(source_id, graph_id, str(q.get("id")), opt)
                print(f"    q={q.get('id')} kind={q.get('kind')} -> {_short(j)}")
            except LabsafeError as e:
                print(f"    q={q.get('id')} -> 失败：{e}")

    print("\n[1] updateVisits")
    print("   ", _short(c.update_visits(source_id, graph_id)))

    print(f"\n[2] finishRate {step}..{duration} step={step}"
          f"{'（按 1x 实时节奏）' if realtime else ''}")
    for t in range(step, int(duration) + 1, step):
        j = c.finish_rate(source_id, graph_id, t)
        print(f"    t={t:<5} {_short(j)}")
        time.sleep(step if realtime else interval)

    print("\n[3] finish")
    fin = c.finish(source_id, graph_id)
    print("   ", _short(fin))

    print("\n[5] 回读")
    dj = c.raw_get("/jcedutec/courseSource/getById",
                   {"id": source_id, "graphId": graph_id})
    print("    getById:", json.dumps(_result(dj) or {}, ensure_ascii=False)[:1500])

    if course_type and course_id:
        after = snapshot_count(c, graph_id, course_type, course_id)
        print("上报后课程计数:", json.dumps(after, ensure_ascii=False) if after else "未找到")
        if before and after:
            b_done = _pick(before, "corseSourceFinishCount", "courseSourceFinishCount")
            a_done = _pick(after, "corseSourceFinishCount", "courseSourceFinishCount")
            b_ans = _pick(before, "donum")
            a_ans = _pick(after, "donum")
            print(f"\n已完成: {b_done} -> {a_done}   已答: {b_ans} -> {a_ans}")
            if str(b_done) == str(a_done):
                print("⚠ 完成数没有变化：finishRate/finish 返回 success 但服务端没有计入。")
                print("  下一步优先查：--id 是否为视频源 ID、ismyCourse 是否为 true、")
                print("  duration 是否小于视频真实时长、该视频是否属于当前 graph。")
                return 1
            print("✔ 完成数已增加，上报链路有效。")
    return 0


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="labsafe 学习进度诊断")
    ap.add_argument("--token", default=os.environ.get("LABSAFE_TOKEN", ""))
    ap.add_argument("--graph", default="", help="graphId（课程树节点 key）")
    ap.add_argument("--id", default="", help="视频源 ID（courseDetail URL 的 id）")
    ap.add_argument("--type", default="", help="课程类型 tab，例如 1=必修 2=选修")
    ap.add_argument("--course", default="", help="课程 ID（用于回读 corseSourceFinishCount）")
    ap.add_argument("--report", action="store_true", help="真正调用 finishRate/finish（会写进度）")
    ap.add_argument("--seconds", type=int, default=60, help="上报到多少秒，默认 60")
    ap.add_argument("--step", type=int, default=10)
    ap.add_argument("--interval", type=float, default=0.4)
    ap.add_argument("--answer-all", default="", help="给所有弹题都提交该选项，如 A 或 A,B")
    ap.add_argument("--realtime", action="store_true",
                    help="按 1x 真实节奏上报（每 step 秒一次），用于排查服务端是否校验真实耗时")
    ap.add_argument("--duration", type=int, default=0,
                    help="视频总时长（秒）；给了就用它，否则用 --seconds")
    args = ap.parse_args(argv)

    token = (args.token or "").strip()
    if not token:
        print("缺少 token：--token 或环境变量 LABSAFE_TOKEN", file=sys.stderr)
        return 2

    c = LabsafeClient(token=token, log=lambda m: None)

    try:
        if not args.graph or not args.id:
            print("== 只读盘点：知识树 / 课程 / 完成计数 ==")
            rows = walk_courses(c)
            if not rows:
                print("没有取到任何课程，检查 token 或接口返回。")
                return 0
            print_inventory(rows)
            print("\n说明：'已完成/总课程' 来自 corseSourceFinishCount/corseSourceCount，")
            print("      是服务端记录的完成数，不是脚本自报的 success 次数。")
            return 0

        info = inspect_source(c, args.graph, args.id)
        dur = args.duration or guess_duration(
            info["detail"] if isinstance(info["detail"], dict) else {},
            info["questions"])
        if not args.report:
            print(f"\n推测视频时长: {dur if dur else '未知（需 --duration / --seconds 手动指定）'}")
            print("（只读模式，未上报。加 --report 做前后计数对照）")
            return 0

        return run_report(c, args.graph, args.id, args.duration or args.seconds,
                          args.step, args.interval, args.type or None,
                          args.course or None, args.answer_all or None,
                          args.realtime)

    except LabsafeAuthError as e:
        print(f"✗ {e}（token 失效，需要重新 CAS 登录换 token）", file=sys.stderr)
        return 1
    except LabsafeError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
