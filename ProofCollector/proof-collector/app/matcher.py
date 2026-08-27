"""Canary matching over normalized candidates (SPEC §8, §9).

A match requires the canary VALUE to appear (case-sensitive substring) in a
candidate. Per canary we keep the "best" hit = the one with the deepest
encoding_path (most specific carrier), which is what the demo wants to show.
"""


def match(cands, canaries):
    hits = []
    for cand in cands:
        for can in canaries:
            token = can["token"]
            pos = cand["text"].find(token)
            if pos >= 0:
                hits.append({
                    "canary_id": can["canary_id"],
                    "label": can["label"],
                    "secret_kind": can["secret_kind"],
                    "token": token,
                    "matched_source": cand["source"],
                    "encoding_path": cand["encoding_path"],
                    "cand": cand,
                    "pos": pos,
                })

    best = {}
    for h in hits:
        cur = best.get(h["canary_id"])
        if cur is None or len(h["encoding_path"]) > len(cur["encoding_path"]):
            best[h["canary_id"]] = h
    return list(best.values())
