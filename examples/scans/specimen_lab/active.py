"""One bounded extra view per parent run, with a durable no-retry decision record."""

from .hardware import capture, VIEWS
from .observer import observe
from .store import canonical, now, write_new


def next_view(store, parent_id, *, attended, session_id, timeout=240):
    parent = store.run(parent_id)
    if parent["state"] != "usable":
        raise ValueError("Only a completed, validated observation can propose a view")
    requested = parent["result"]["next_view_id"]
    if requested is None:
        return {"state": "finished", "reason": "Observer requested no further view"}
    if requested not in parent["next_views"] or requested not in VIEWS:
        raise ValueError("View outside both observer and hardware catalogues")
    if not attended:
        raise ValueError("Attended option is required before accepting a view request")
    folder = store.root / "decisions" / parent_id
    decision = dict(parent_run_id=parent_id,requested_view=requested,created=now(),
                    reason=parent["result"]["next_view_reason"],session_id=session_id,
                    policy="one_extra_view_v1",image_budget=2,attended=True)
    # This exclusive file is written BEFORE hardware: invocation after a crash
    # cannot silently execute the same motion again.
    write_new(folder / "intent.json", (canonical(decision)+"\n").encode())
    try:
        image=capture(store,parent["specimen_id"],requested,attended=True,session_id=session_id)
        ids=[*parent["image_ids"],image["id"]]
        # Reuse a known crop transform for this bounded setup view. The original
        # always accompanies it; no claim that the specimen remains in the crop.
        old_crops=[store.image(i) for i in parent["image_ids"] if store.image(i)["role"]=="crop"]
        if old_crops:
            crop=store.crop(image["id"],old_crops[-1]["crop_box"])
            ids.append(crop["id"])
        write_new(folder / "captured.json", (canonical({"image_ids":ids,"created":now()})+"\n").encode())
        result=observe(store,parent["specimen_id"],ids,parent["model"],next_views=(),timeout=timeout)
        terminal=dict(state=result["state"],child_run_id=result["id"],finished=now())
    except Exception as exc:
        terminal=dict(state="failed",error=str(exc),finished=now())
        write_new(folder / "terminal.json",(canonical(terminal)+"\n").encode())
        raise
    write_new(folder / "terminal.json",(canonical(terminal)+"\n").encode())
    return terminal
