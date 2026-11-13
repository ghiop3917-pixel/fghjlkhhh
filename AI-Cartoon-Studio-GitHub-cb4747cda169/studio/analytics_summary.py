"""Наблюдаемые показатели историй и персонажей без причинных и выдуманных выводов."""
def studio_summary(store):
    metrics = {r["id"]: r["body"] for r in store.objects("metrics")}
    characters, series = {}, {}
    for episode in store.objects("episode"):
        data = metrics.get(episode["id"])
        if not data:
            continue
        body = episode["body"]
        ids = {s["character_id"] for s in body["script"]["scenes"]}
        for group,keys in ((characters, ids), (series, [body["series"]])):
            for key in keys:
                row = group.setdefault(key, {"videos_with_metrics": 0, "known_views": 0, "videos_with_known_views": 0, "retention_values": []})
                row["videos_with_metrics"] += 1
                if data.get("views") is not None:
                    row["known_views"] += data["views"]
                    row["videos_with_known_views"] += 1
                if data.get("retention") is not None:
                    row["retention_values"].append(data["retention"])
    for group in (characters, series):
        for row in group.values():
            values = row.pop("retention_values")
            row["mean_retention"] = sum(values)/len(values) if values else None
    return {"characters": characters, "series": series, "note": "Просмотры видео с персонажем — наблюдаемый прокси, не доказательство его влияния. В одном видео несколько персонажей; суммы между ними нельзя складывать."}
