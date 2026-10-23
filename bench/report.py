"""Print a markdown table from the result_*.json files for one dataset size."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main(n: str) -> None:
    rows = []
    for p in sorted(Path("/home/user/benchdata").glob(f"result_*_{n}.json")):
        d = json.loads(p.read_text())
        v, a, lx = d["vec"], d["vec_acl"], d["lex"]
        size = sum(d["sizes"].values())
        rows.append(f"| {d['candidate']} | {d['load_s']:.0f} | {d['vec_index_s']:.0f} | {d['fts_index_s']:.0f} | "
                    f"{size:.0f} | {v['recall@10']:.3f} | {v['p50']:.1f} / {v['p99']:.1f} | "
                    f"{a['recall@10']:.3f} | {a['p50']:.1f} / {a['p99']:.1f} | "
                    f"{lx['source_hit@10']:.2f} | {lx['p50']:.1f} / {lx['p99']:.1f} |")
    print(f"### n = {n}, 384-dim\n")
    print("| engine | load s | vec idx s | fts idx s | disk MB | recall | p50/p99 ms | ACL recall | ACL p50/p99 ms | lex hit | lex p50/p99 ms |")
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    print("\n".join(rows))


main(sys.argv[1])
