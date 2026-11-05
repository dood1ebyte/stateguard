"""
Score realhttp rows: strict residual failure rate per server and arm (Wilson 95%).

* One row is one logical invocation; a resend never adds a row. Every row stays in its
  denominator: a driver error, an UNKNOWN, a timeout are failures.
* Fault-free, faulted (transport / session) and schema drift (op "drift") are reported apart.
* Unsafe outcomes (duplicate effect, false success, false failure, wrong content) and
  unresolved outcomes (surfaced unknown, not sent, no answer) are counted apart.
* induced_vs_raw: rows that fail in an arm but succeed in raw on the same (op, mode, trial).
* invocation_record_mismatch: rows where the relay's own ``invocation`` record says the call
  was answered or recovered but ground truth says it failed (or the reverse) -- a check on
  the incidence records themselves.

usage: summarize.py ROWS.jsonl [more.jsonl ...] [--json OUT.json]
"""

import json
import math
import statistics
import sys
from collections import Counter, defaultdict

ORDER = ["raw", "naive", "sg", "sg_keyed", "sg_resumable", "sg_deadline", "sg_keyed_deadline"]
UNSAFE = ("duplicate_effect", "false_success", "false_failure", "wrong_content")
UNRESOLVED = ("surfaced_unknown", "surfaced_not_sent", "no_answer_timeout", "no_answer_crash")
ANSWERED = ("answered", "recovered")


def wilson(k, n, z=1.959964):
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def rate(xs):
    k = sum(1 for r in xs if not r["success"])
    lo, hi = wilson(k, len(xs))
    return {"fail": k, "n": len(xs), "rifr": round(k / len(xs), 4) if xs else None,
            "ci95": [round(lo, 4), round(hi, 4)]}


def latency(xs):
    values = sorted(r["latency_s"] for r in xs if "latency_s" in r)
    if not values:
        return None
    p95 = values[min(len(values) - 1, int(math.ceil(0.95 * len(values))) - 1)]
    return {"n": len(values), "mean": round(statistics.fmean(values), 3),
            "median": round(statistics.median(values), 3), "p95": round(p95, 3)}


def summarize(rows):
    out = {"n_rows": len(rows), "driver_errors": sum("driver_error" in r["kinds"] for r in rows),
           "servers": {}}
    for server in sorted({r["server"] for r in rows}):
        srows = [r for r in rows if r["server"] == server]
        arms = sorted({r["arm"] for r in srows}, key=lambda a: ORDER.index(a) if a in ORDER else 99)
        by = {((r["op"], r["mode"], r["trial"]), r["arm"]): r for r in srows}
        sout = {}
        for arm in arms:
            a = [r for r in srows if r["arm"] == arm]
            drift = [r for r in a if r["op"] == "drift"]
            control = [r for r in a if r["mode"] == "none" and r["op"] != "drift"]
            faulted = [r for r in a if r["mode"] != "none"]
            records = [r for r in a if r.get("relay_invocation")]
            mismatch = [
                r["run"] for r in records
                if (r["relay_invocation"]["resolution"] in ANSWERED) != bool(r["success"])
            ]
            entry = {
                "fault_free": rate(control),
                "drift": rate(drift),
                "faulted": rate(faulted),
                "faulted_transport": rate([r for r in faulted if r.get("family") == "transport"]),
                "faulted_session": rate([r for r in faulted if r.get("family") == "session"]),
                "unsafe": {u: sum(1 for r in a if u in r["kinds"]) for u in UNSAFE},
                "unresolved": {u: sum(1 for r in a if u in r["kinds"]) for u in UNRESOLVED},
                "failure_kinds": dict(Counter(k for r in a if not r["success"] for k in r["kinds"])),
                "server_outcomes": dict(Counter(str(r.get("server_outcome")) for r in a
                                                if r.get("server_outcome"))),
                "invocation_records": len(records),
                "invocation_record_mismatch": len(mismatch),
                "invocation_record_mismatch_runs": mismatch[:20],
                "latency_fault_free": latency(control),
            }
            if arm != "raw":
                induced = [k for (k, ar), r in by.items() if ar == arm and not r["success"]
                           and (k, "raw") in by and by[(k, "raw")]["success"]]
                recovered = [k for (k, ar), r in by.items() if ar == arm and r["success"]
                             and (k, "raw") in by and not by[(k, "raw")]["success"]]
                entry["induced_vs_raw"] = len(induced)
                entry["induced"] = sorted(induced)[:20]
                entry["recovered_vs_raw"] = len(recovered)
            cells = defaultdict(lambda: [0, 0])
            for r in a:
                c = cells[f"{r['op']}/{r['mode']}"]
                c[0] += 0 if r["success"] else 1
                c[1] += 1
            entry["cells"] = dict(sorted(cells.items()))
            sout[arm] = entry
        out["servers"][server] = sout
    return out


def pct(e):
    if not e["n"]:
        return "-"
    lo, hi = e["ci95"]
    return f"{e['fail']}/{e['n']} ({e['fail'] / e['n']:.1%}, ci {lo:.1%}-{hi:.1%})"


def main(argv):
    out_json = None
    if "--json" in argv:
        i = argv.index("--json")
        out_json = argv[i + 1]
        argv = argv[:i] + argv[i + 2:]
    rows = [json.loads(line) for p in argv for line in open(p) if line.strip()]
    s = summarize(rows)
    print(f"rows {s['n_rows']}  driver_errors {s['driver_errors']}")
    for server, arms in s["servers"].items():
        print(f"\n===== {server}")
        for arm, e in arms.items():
            print(f"\n{arm}")
            print(f"  fault-free  {pct(e['fault_free'])}")
            if e["drift"]["n"]:
                print(f"  drift       {pct(e['drift'])}")
            print(f"  faulted     {pct(e['faulted'])}   transport {pct(e['faulted_transport'])}"
                  f"   session {pct(e['faulted_session'])}")
            print(f"  unsafe {e['unsafe']}  unresolved {e['unresolved']}"
                  f"  induced_vs_raw {e.get('induced_vs_raw', '-')}"
                  f"  recovered_vs_raw {e.get('recovered_vs_raw', '-')}")
            if e["invocation_records"]:
                print(f"  relay invocation records {e['invocation_records']}, "
                      f"mismatching ground truth {e['invocation_record_mismatch']}")
            print(f"  latency fault-free {e['latency_fault_free']}")
        cells = sorted({c for e in arms.values() for c in e["cells"]})
        print()
        print(f"{'op/mode':<24}" + "".join(f"{a:>19}" for a in arms))
        for c in cells:
            print(f"{c:<24}" + "".join(
                f"{arms[a]['cells'].get(c, [0, 0])[0]:>12}/{arms[a]['cells'].get(c, [0, 0])[1]:<6}"
                for a in arms))
    if out_json:
        with open(out_json, "w") as fh:
            json.dump(s, fh, indent=1)


if __name__ == "__main__":
    main(sys.argv[1:])
