import os, sys, tempfile, threading, time
from pathlib import Path

os.environ["PEAKCUT_DATA"] = tempfile.mkdtemp(prefix="pc-test-live-")
os.environ["DATABASE_URL"] = ""
sys.path.insert(0, ".")

from studio import auth, creators, ctx, db, jobs, live

db.init()
threading.Thread(target=jobs._live_worker, daemon=True).start()

fails = []
def check(name, ok, extra=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  {extra}" if extra and not ok else ""))
    if not ok:
        fails.append(name)

u, _ = auth._create("liveclip@example.com", "LiveClip", password_hash="x")
with ctx.as_user(u.id):
    cr = creators.create({"name": "TestStreamer", "channel": "https://www.twitch.tv/test"})
    j = jobs.create({"type": "live", "url": "https://www.twitch.tv/test", "name": "Live Test"}, {},
                    enqueue=False, creator=cr["id"], kind="live")
    jobs.save(j)
    s = live.Session(j, cr)

    # 1. Simulate p01.csv with segments spanning 0 to 60 seconds
    p = "p01"
    csv = s.buf / f"{p}.csv"
    lines = []
    t = 0.0
    for i in range(15):
        dur = 4.0
        seg_file = s.buf / f"{p}_{i:05d}.ts"
        seg_file.write_bytes(b"\x47" * 188)
        lines.append(f"{seg_file.name},{t:.6f},{t + dur:.6f}")
        t += dur
    csv.write_text("\n".join(lines), encoding="utf-8")
    s.part = 1
    s._scan_segments()

    check("Segments scanned spanning 60 seconds", len(s.segments) == 15, f"count={len(s.segments)}")
    check("Session seg_clock advanced to 60s", round(s.seg_clock) == 60, f"clock={s.seg_clock}")

    # Mock media operations
    real_probe = live.media.probe
    real_run = live.media.run
    real_align = s._align
    real_render_one = jobs.render_one

    rendered = []
    live.media.probe = lambda path: {"width": 1280, "height": 720}
    live.media.run = lambda cmd: (s.dir / "clips" / f"{cmd[-1].split('/')[-1]}").write_bytes(b"\x47" * 188)
    s._align = lambda src, guess, dur: guess
    jobs.render_one = lambda j, c, w: rendered.append(c["id"])

    try:
        # 2. Test clip rendering within buffer
        clip = {"id": "clip001", "start": 20.0, "end": 40.0, "status": "queued"}
        s.job["clips"].append(clip)
        s._render(clip)
        check("Clip renders without buffer error", clip.get("status") != "error", str(clip.get("error")))
        check("Clip was submitted to render_one", "clip001" in rendered)

        # 3. Test tolerant boundary clamping: clip requested starting slightly earlier than buffer
        clip2 = {"id": "clip002", "start": -2.0, "end": 15.0, "status": "queued"}
        s.job["clips"].append(clip2)
        s._render(clip2)
        check("Clip starting near buffer start is clamped and succeeds", clip2.get("status") != "error", str(clip2.get("error")))
        check("Clamped start is >= 0", clip2.get("start") >= 0, f"start={clip2.get('start')}")
        check("Clip2 was submitted to render_one", "clip002" in rendered)
    finally:
        live.media.probe = real_probe
        live.media.run = real_run
        s._align = real_align
        jobs.render_one = real_render_one

print("ALL PASSED" if not fails else f"{len(fails)} failed")
sys.exit(1 if fails else 0)
