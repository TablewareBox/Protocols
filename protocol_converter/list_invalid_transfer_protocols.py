#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
列出「无有效 transfer_actions」的协议（与批量导出跳过条件一致）。
在 protocol_converter 目录下执行:
  python list_invalid_transfer_protocols.py
  python list_invalid_transfer_protocols.py -o my_invalid.txt
"""
from __future__ import annotations

import argparse
import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    os.chdir(here)

    ap = argparse.ArgumentParser()
    ap.add_argument(
        "-o",
        "--output",
        default="invalid_transfer_protocols.txt",
        help="输出文本路径（相对 protocol_converter）",
    )
    ap.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="静默模式（不打印各协议的 Processing 日志）",
    )
    args = ap.parse_args()

    steps_dir = os.path.join(here, "steps")
    if not os.path.isdir(steps_dir):
        print("steps 目录不存在", file=sys.stderr)
        return 1

    protocols = sorted(
        f.replace(".json", "")
        for f in os.listdir(steps_dir)
        if f.endswith(".json")
    )

    from change_to_transfer_group import generate_transfer_actions

    invalid: list[str] = []
    for i, name in enumerate(protocols, 1):
        if not args.quiet and (i % 50 == 0 or i == 1):
            print(f"[{i}/{len(protocols)}] scanning...", flush=True)
        try:
            if args.quiet:
                buf = StringIO()
                with redirect_stdout(buf), redirect_stderr(buf):
                    ta, _ = generate_transfer_actions(name)
            else:
                ta, _ = generate_transfer_actions(name)
            if not ta:
                invalid.append(name)
        except Exception:
            invalid.append(name)

    out_path = os.path.join(here, args.output)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("# 无有效 transfer_liquid（generate_transfer_actions 返回空）\n")
        f.write(f"# 共 {len(invalid)} / {len(protocols)}\n\n")
        for name in invalid:
            f.write(name + "\n")

    print(f"完成: {len(invalid)} 个无效（共 {len(protocols)} 个 steps 协议）")
    print(f"已写入: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
