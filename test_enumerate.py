#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""enumerate_sources 解析 duration / isFinish 的离线测试。"""

import labsafe_player as lp


class FakeClient:
    def raw_get(self, path, params=None):
        if path.endswith("queryGraphTree"):
            return {"result": [{"key": "G", "title": "树"}]}
        if path.endswith("queryTypeById"):
            return {"result": [{"type": "1", "name": "视频课程"}]}
        if path.endswith("queryCourseTypeById"):
            return {"result": [{"id": "C", "name": "课程"}]}
        if path.endswith("detailByTypeAndGraph"):
            return {"result": [
                {"id": "S1", "name": "已完成视频", "duration": "229.506009",
                 "isFinish": "1"},
                {"id": "S2", "name": "未完成视频", "duration": "318.740998",
                 "isFinish": "0"},
            ]}
        return {}


def main() -> int:
    refs = lp.enumerate_sources(FakeClient())
    for r in refs:
        print(r["source_id"], r["duration"], r["is_finish"])
    ok = (len(refs) == 2
          and refs[0]["duration"] == 229 and refs[0]["is_finish"] is True
          and refs[1]["duration"] == 318 and refs[1]["is_finish"] is False)
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
