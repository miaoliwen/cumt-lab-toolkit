#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GUI labsafe 集成的离线冒烟测试（不打真实请求）。"""

import time
import tkinter as tk

import aqzrui_gui as gui
import labsafe_player as lp

CALLS = {}


def fake_enum(_client):
    return [{"graph": "G", "source_id": "S", "source_name": "视频A",
             "course_name": "课程A"}]


def fake_run(refs, **kw):
    CALLS.update(kw)
    CALLS["refs"] = refs
    lp.LOG("开始 1 个视频  线程2  1.0x  演练")
    lp.LOG("[done] [视频A] 10s/10s 心跳1 弹题0/0")
    return 1, 0


lp.enumerate_sources = fake_enum       # type: ignore[assignment]
lp.run_playback = fake_run             # type: ignore[assignment]


def main() -> int:
    root = tk.Tk()
    root.withdraw()
    app = gui.App(root)
    root.update_idletasks()
    print(f"req_size={root.winfo_reqwidth()}x{root.winfo_reqheight()}")

    app.var_ls_token.set("T")
    app.var_ls_dry.set(True)
    app.var_ls_threads.set(3)
    app.on_ls_play_all()

    deadline = time.time() + 8
    while app.busy and time.time() < deadline:
        root.update()
        time.sleep(0.05)
    root.update()

    tail = app.txt.get("1.0", "end").strip().splitlines()[-6:]
    print("busy=", app.busy)
    print("threads=", CALLS.get("threads"), "dry=", CALLS.get("dry"),
          "token=", CALLS.get("token"), "refs=", len(CALLS.get("refs") or []))
    for line in tail:
        print("  ", line)

    ok = (not app.busy and CALLS.get("threads") == 3
          and CALLS.get("dry") is True and CALLS.get("token") == "T"
          and len(CALLS.get("refs") or []) == 1
          and any("视频A" in ln for ln in tail))
    print("PASS" if ok else "FAIL")
    root.destroy()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
