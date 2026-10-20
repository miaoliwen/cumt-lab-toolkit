#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""run_study 的 DOCKIND 过滤逻辑冒烟测试（桩客户端，不发真实请求）。"""

from typing import Any, Dict, List

import aqzrui_speedrun as sr

WATCHED: List = []


class FakeClient:
    def security_kinds(self, page: int, size: int) -> Dict[str, Any]:
        return {"rows": [{"ID": "11", "TYPENAME": "测试类别"}]}

    def study_doc_index(self, top: int = 10) -> Dict[str, Any]:
        # 索引里混一条文档(901)和一条视频(902)
        return {"rows": [
            {"ID": "901", "DOCKIND": "1", "PRENAME": "文档A"},
            {"ID": "902", "DOCKIND": "2", "PRENAME": "视频B"},
        ]}

    def study_docs(self, page: int, size: int, security_kind_id: str = "",
                   ismust: str = "1", doc_kind: str = "0", **kw) -> Dict[str, Any]:
        # 类别列表里混一条视频(903)和一条文档(904)
        return {"rows": [
            {"ID": "903", "DOCKIND": "2", "PRENAME": "视频C"},
            {"ID": "904", "DOCKIND": "1", "PRENAME": "文档D"},
        ], "total": 2}

    def study_doc_one(self, did: str) -> Dict[str, Any]:
        return {"data": [{"RID": f"R{did}", "ISDOWN": "0", "TIMES": None}]}

    def watch_time(self, rid: str, secs: str) -> Dict[str, Any]:
        WATCHED.append((rid, secs))
        return {"Flag": "1"}

    def study_info(self) -> Dict[str, Any]:
        return {"data": {"AQLB": [{"ID": "11", "Count": [4, 4, 1, 1]}]}}


def main() -> int:
    run = sr.SpeedRun(FakeClient(), bank=None, dry_run=False, retry_study=True)
    # 断点读写替换为内存操作，避免碰真实 checkpoint
    run.cp = {"done_practice": [], "done_exam": [], "watchtime": []}
    run._save_cp = lambda: None            # type: ignore[assignment]
    run._mark = lambda bucket, key: run.cp.setdefault(bucket, []).append(key)
    run._unmark = lambda bucket, key: run.cp.get(bucket, []).remove(key)

    run.run_study()

    rids = sorted(set(r for r, _ in WATCHED))
    expect = sorted(["R902", "R903"])
    print("WATCHED:", WATCHED)
    print("checkpoint watchtime:", run.cp["watchtime"])
    ok = rids == expect
    print("PASS" if ok else f"FAIL 期望 {expect}，实际 {rids}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
