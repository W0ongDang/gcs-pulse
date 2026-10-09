"""Pure, deterministic aggregation shared by admin and public result endpoints."""
def aggregate(sessions, records, members, selected_id=None):
    selected = next((s for s in sessions if s["id"] == selected_id), None)
    closed = [s for s in sessions if s["status"] == "closed" and
              (not selected or s["date"] <= selected["date"])]
    ids = {s["id"] for s in closed}
    valid = sorted([r for r in records if not r.get("cancelled_at") and r["session_id"] in ids],
                   key=lambda r: (r["checked_at"], r["id"]))
    firsts = {}
    for r in valid:
        if r["source"] == "qr":
            firsts.setdefault(r["session_id"], r)
    counts, wins, first_session = {}, {}, {}
    for s in sorted(closed, key=lambda s: (s["date"], s["number"])):
        for r in valid:
            if r["session_id"] == s["id"]:
                first_session.setdefault(r["user_id"], s["id"])
                counts[r["user_id"]] = counts.get(r["user_id"], 0) + 1
    for r in firsts.values():
        wins[r["user_id"]] = wins.get(r["user_id"], 0) + 1
    people = {m["id"]: m for m in members}
    for r in valid:
        people.setdefault(r["user_id"], {"id": r["user_id"], "name": r["name"], "affiliation": r["affiliation"]})
    individuals = [{**m, "attendance_count": counts.get(uid, 0), "first_count": wins.get(uid, 0),
                    "ratio": counts.get(uid, 0) / len(closed) if closed else None}
                   for uid, m in people.items()]
    leaderboard = sorted([m.copy() for m in individuals if m["attendance_count"]],
                         key=lambda m: (-m["first_count"], -m["attendance_count"], m["id"]))
    previous, rank = None, 0
    for i, m in enumerate(leaderboard, 1):
        key = (m["first_count"], m["attendance_count"])
        if key != previous:
            rank = i
        m["rank"], previous = rank, key
    current = [r for r in valid if r["session_id"] == selected_id]
    return {"closed_count": len(closed), "average": len(valid) / len(closed) if closed else None,
            "count": len(current), "winner": firsts.get(selected_id),
            "newcomers": [r for r in current if first_session[r["user_id"]] == selected_id],
            "leaderboard": leaderboard, "individuals": individuals}
