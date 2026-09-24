"""Structural checks for atem2fcp output: spine contiguity, end alignment, counts."""
import sys, xml.etree.ElementTree as ET
from fractions import Fraction as Fr

def t(v): return Fr(v[:-1]) if v else Fr(0)

def check(path, expect_shots=None, expect_trans=None):
    root = ET.parse(path).getroot()
    fmt = root.find("resources/format"); fd = t(fmt.get("frameDuration"))
    seq = root.find(".//sequence"); tc0, dur = t(seq.get("tcStart")), t(seq.get("duration"))
    items = list(seq.find("spine")); errs = []
    clips = [e for e in items if e.tag != "transition"]
    trans = [e for e in items if e.tag == "transition"]
    pos = tc0
    for e in clips:
        if t(e.get("offset")) != pos: errs.append(f"gap/overlap at {e.get('offset')}")
        for a in ("offset", "duration", "start"):
            if e.get(a) and (t(e.get(a)) / fd).denominator != 1: errs.append(f"{a} off-frame on {e.get('name')}")
        pos = t(e.get("offset")) + t(e.get("duration"))
    if pos != tc0 + dur: errs.append(f"last clip ends {pos}, sequence ends {tc0 + dur}")
    # each transition centred on an edit point, within its neighbours
    for i, e in enumerate(items):
        if e.tag != "transition": continue
        prev, nxt = items[i - 1], items[i + 1]
        edit = t(prev.get("offset")) + t(prev.get("duration"))
        o, d = t(e.get("offset")), t(e.get("duration"))
        if o + d / 2 != edit or t(nxt.get("offset")) != edit: errs.append(f"transition at {e.get('offset')} not centred")
        if prev.tag != "mc-clip" or nxt.tag != "mc-clip": errs.append("transition next to a gap")
    audio = {s.get("angleID") for c in clips if c.tag == "mc-clip" for s in c if s.get("srcEnable") in ("audio", "all")}
    if len(audio) > 1: errs.append(f"audio angle changes: {audio}")
    if expect_shots is not None and len(clips) != expect_shots: errs.append(f"{len(clips)} shots, expected {expect_shots}")
    if expect_trans is not None and len(trans) != expect_trans: errs.append(f"{len(trans)} transitions, expected {expect_trans}")
    return len(clips), len(trans), errs

if __name__ == "__main__":
    n, tr, errs = check(sys.argv[1], *(int(a) for a in sys.argv[2:4]))
    print(f"{n} clips, {tr} transitions:", "OK" if not errs else "\n  ".join(["FAIL"] + errs))
    sys.exit(1 if errs else 0)
