"""Local immutable assets/run evidence with a small SQLite query index."""

from __future__ import annotations

import datetime as dt
from contextlib import contextmanager
import hashlib
import io
import json
from pathlib import Path
import re
import sqlite3
import uuid

from PIL import Image

from .contract import FEATURES, VERSION, canonical, digest, output_schema, prompt, validate


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def uid(prefix):
    return prefix + "_" + uuid.uuid4().hex[:16]


def write_new(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as out:
        out.write(data)


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS specimens(id TEXT PRIMARY KEY, label TEXT NOT NULL, created TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS images(id TEXT PRIMARY KEY, specimen_id TEXT NOT NULL, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, specimen_id TEXT NOT NULL, state TEXT NOT NULL, data TEXT NOT NULL, result TEXT);
            CREATE TABLE IF NOT EXISTS reviews(id TEXT PRIMARY KEY, run_id TEXT NOT NULL, feature_id TEXT NOT NULL, verdict TEXT NOT NULL, note TEXT NOT NULL, reviewer TEXT NOT NULL, created TEXT NOT NULL);
            """)

    @contextmanager
    def db(self):
        conn = sqlite3.connect(self.root / "index.sqlite3", timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def specimen(self, specimen_id, label):
        if not re.fullmatch(r"[A-Za-z0-9:_-]{1,80}", specimen_id):
            raise ValueError("Invalid specimen ID")
        with self.db() as db:
            existing = db.execute("SELECT label FROM specimens WHERE id=?", (specimen_id,)).fetchone()
            if existing and existing[0] != label:
                raise ValueError("Specimen exists with a different label")
            db.execute("INSERT OR IGNORE INTO specimens VALUES(?,?,?)", (specimen_id, label, now()))
        return specimen_id

    def add_image(self, specimen_id, source, *, pose=None, role="original", parent_id=None, crop_box=None):
        if role not in {"original", "crop"}:
            raise ValueError("Unknown image role")
        if role == "original" and (parent_id is not None or crop_box is not None):
            raise ValueError("Original images cannot claim a crop transform")
        if role == "crop":
            parent = self.image(parent_id)
            if parent["specimen_id"] != specimen_id or not crop_box:
                raise ValueError("Crop must link to the same specimen")
        raw = Path(source).read_bytes() if isinstance(source, (str, Path)) else source
        with Image.open(io.BytesIO(raw)) as im:
            im.verify()
        with Image.open(io.BytesIO(raw)) as im:
            width, height = im.size
            fmt = im.format
        if fmt not in {"JPEG", "PNG"}:
            raise ValueError("Only JPEG/PNG images are supported")
        sha = hashlib.sha256(raw).hexdigest()
        filename = "assets/" + sha + (".jpg" if fmt == "JPEG" else ".png")
        target = self.root / filename
        with self.db() as db:
            if not db.execute("SELECT 1 FROM specimens WHERE id=?", (specimen_id,)).fetchone():
                raise ValueError("Unknown specimen")
        if target.exists():
            if hashlib.sha256(target.read_bytes()).hexdigest() != sha:
                raise ValueError("Stored asset hash mismatch")
        else:
            write_new(target, raw)
        data = dict(id=uid("image"), specimen_id=specimen_id, width=width, height=height,
                    sha256=sha, path=filename, role=role, parent_id=parent_id, crop_box=crop_box,
                    pose=pose or {"kind": "unregistered", "note": "No calibrated pose supplied"}, created=now())
        with self.db() as db:
            db.execute("INSERT INTO images VALUES(?,?,?)", (data["id"], specimen_id, canonical(data)))
        return data

    def image(self, image_id):
        with self.db() as db:
            row = db.execute("SELECT data FROM images WHERE id=?", (image_id,)).fetchone()
        if not row:
            raise ValueError("Unknown image")
        return json.loads(row[0])

    def image_path(self, image_id, verify=False):
        data = self.image(image_id)
        path = (self.root / data["path"]).resolve()
        if not path.is_relative_to(self.root / "assets"):
            raise ValueError("Asset path outside store")
        if verify and hashlib.sha256(path.read_bytes()).hexdigest() != data["sha256"]:
            raise ValueError("Asset was modified")
        return path

    def crop(self, image_id, box):
        parent = self.image(image_id)
        x, y, w, h = box
        if any(type(v) is not int for v in box) or x < 0 or y < 0 or min(w, h) <= 0 or x+w > parent["width"] or y+h > parent["height"]:
            raise ValueError("Crop outside image")
        with Image.open(self.image_path(image_id, verify=True)) as im:
            cropped = im.crop((x, y, x+w, y+h))
            buf = io.BytesIO()
            cropped.save(buf, "PNG")
        return self.add_image(parent["specimen_id"], buf.getvalue(), pose=parent["pose"], role="crop",
                              parent_id=image_id, crop_box=box)

    def begin_run(self, specimen_id, image_ids, model, *, next_views=(), engine="codex-cli", prompt_suffix=""):
        images = [self.image(i) for i in image_ids]
        if not images or len(set(image_ids)) != len(image_ids) or any(i["specimen_id"] != specimen_id for i in images):
            raise ValueError("Run must use unique images of one specimen")
        for i in image_ids:
            self.image_path(i, verify=True)
        run_id = uid("run")
        run_dir = self.root / "runs" / run_id
        schema = output_schema(image_ids, next_views)
        text = prompt(images, next_views) + prompt_suffix
        data = dict(id=run_id, specimen_id=specimen_id, image_ids=image_ids, model=model, engine=engine,
                    created=now(), schema_version=VERSION, schema_sha256=digest(schema),
                    vocabulary_sha256=digest(FEATURES), prompt_sha256=hashlib.sha256(text.encode()).hexdigest(),
                    images=images, next_views=list(next_views), review_status="unreviewed",
                    qualification="engineering_pilot_not_blinded_benchmark")
        write_new(run_dir / "request.json", (canonical(data)+"\n").encode())
        write_new(run_dir / "schema.json", (canonical(schema)+"\n").encode())
        write_new(run_dir / "prompt.txt", text.encode())
        with self.db() as db:
            db.execute("INSERT INTO runs VALUES(?,?,?,?,NULL)", (run_id, specimen_id, "running", canonical(data)))
        return data

    def run(self, run_id):
        with self.db() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise ValueError("Unknown run")
        return {**json.loads(row["data"]), "state": row["state"], "result": json.loads(row["result"]) if row["result"] else None}

    def finish_run(self, run_id, raw, *, execution_error=None, telemetry=None):
        run = self.run(run_id)
        if run["state"] != "running":
            raise ValueError("Run is terminal and cannot be overwritten")
        run_dir = self.root / "runs" / run_id
        # Exclusive create prevents concurrent finalizers overwriting evidence.
        write_new(run_dir / "raw-response.txt", raw.encode())
        errors = []
        payload = None
        try:
            payload = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Non-finite JSON")))
            errors = validate(payload, run["image_ids"], run["next_views"])
            for image_id in run["image_ids"]:
                self.image_path(image_id, verify=True)
        except (ValueError, TypeError, OSError) as exc:
            errors.append(str(exc))
        if execution_error:
            errors.append(execution_error)
        final = dict(finished=now(), errors=errors, telemetry=telemetry or {},
                     raw_sha256=hashlib.sha256(raw.encode()).hexdigest(),
                     semantic_accuracy="unverified", state="failed" if errors else "usable")
        write_new(run_dir / "terminal.json", (canonical(final)+"\n").encode())
        data = {k:v for k,v in run.items() if k not in {"state", "result"}}
        data.update(final)
        with self.db() as db:
            db.execute("UPDATE runs SET state=?,data=?,result=? WHERE id=? AND state='running'",
                       (final["state"], canonical(data), canonical(payload) if not errors else None, run_id))
        return self.run(run_id)

    def review(self, run_id, feature_id, verdict, note, reviewer="local_operator"):
        run = self.run(run_id)
        if run["state"] != "usable" or feature_id not in FEATURES or verdict not in {"accepted", "rejected", "needs_review"}:
            raise ValueError("Invalid review target or verdict")
        if not isinstance(note, str) or not note.strip() or len(note) > 4000:
            raise ValueError("Review requires a note of at most 4000 characters")
        data = dict(id=uid("review"), run_id=run_id, feature_id=feature_id, verdict=verdict,
                    note=note, reviewer=reviewer, created=now())
        with self.db() as db:
            db.execute("INSERT INTO reviews VALUES(?,?,?,?,?,?,?)", tuple(data.values()))
        return data

    def snapshot(self):
        with self.db() as db:
            specimens = [dict(r) for r in db.execute("SELECT * FROM specimens ORDER BY created")]
            images = [json.loads(r[0]) for r in db.execute("SELECT data FROM images")]
            run_ids = [r[0] for r in db.execute("SELECT id FROM runs ORDER BY rowid")]
            reviews = [dict(r) for r in db.execute("SELECT * FROM reviews ORDER BY rowid")]
        decisions=[]
        for path in sorted((self.root/"decisions").glob("*/intent.json")):
            data=json.loads(path.read_text())
            final=path.with_name("terminal.json")
            data["terminal"]=json.loads(final.read_text()) if final.exists() else {"state":"pending_or_interrupted"}
            decisions.append(data)
        actions=[]
        for path in sorted((self.root/"hardware").glob("*/*/intent.json")):
            data=json.loads(path.read_text())
            final=path.with_name("terminal.json")
            data["terminal"]=json.loads(final.read_text()) if final.exists() else {"state":"pending_or_interrupted"}
            resolution=path.with_name("resolution.json")
            if resolution.exists():data["resolution"]=json.loads(resolution.read_text())
            actions.append(data)
        demos=[json.loads(p.read_text()) for p in sorted((self.root/'demos').glob('demo_*/progress.json'))]
        return dict(specimens=specimens, images=images, runs=[self.run(r) for r in run_ids],
                    reviews=reviews, features=FEATURES, schema_version=VERSION, decisions=decisions, hardware_actions=actions,demos=demos)

    def graph(self):
        snap = self.snapshot()
        nodes, edges = {}, {}
        def node(key, label, kind, **data):
            nodes[key] = dict(id=key, label=label, kind=kind, **data)
        def edge(source, target, label):
            key = "edge:" + digest([source, target, label])[:24]
            edges[key] = dict(id=key, source=source, target=target, label=label)
        for s in snap["specimens"]:
            node("specimen:"+s["id"], s["label"], "specimen", record=s)
        for run in snap["runs"]:
            if run["state"] != "usable":
                continue
            node(run["id"], run["model"], "model_run", run_id=run["id"], record={k:v for k,v in run.items() if k != "result"})
            for o in run["result"]["observations"]:
                key = run["id"] + ":" + o["feature_id"]
                f_id = "feature:" + o["feature_id"]
                node(f_id, FEATURES[o["feature_id"]]["label"], "feature", feature_id=o["feature_id"], record=FEATURES[o["feature_id"]])
                value = "unknown" if o["status"] == "unknown" else "not applicable" if o["status"] == "not_applicable" else canonical(o["value"])
                node(key, value, "observation", run_id=run["id"], feature_id=o["feature_id"], status=o["status"], record=o)
                edge("specimen:"+run["specimen_id"], key, "has observation")
                edge(key, f_id, "measures")
                edge(key, run["id"], "generated by")
                for image_id in sorted({e["image_id"] for e in o["evidence"]}):
                    node(image_id, image_id[-6:], "image", image_id=image_id, record=self.image(image_id))
                    edge(key, image_id, "supported by")
        for r in snap["reviews"]:
            node(r["id"], r["verdict"], "review", run_id=r["run_id"], feature_id=r["feature_id"], record=r)
            edge(r["id"], r["run_id"]+":"+r["feature_id"], "reviews")
        # Include originals and crop provenance even if only a crop was cited.
        for i in snap["images"]:
            node(i["id"], i["id"][-6:], "image", image_id=i["id"], record=i)
            edge("specimen:"+i["specimen_id"], i["id"], "depicted in")
            if i["parent_id"]:
                edge(i["id"], i["parent_id"], "derived from")
        for action in snap["hardware_actions"]:
            node(action["action_id"],action["view_id"],"capture_action",record=action)
            image_id=action["terminal"].get("image_id")
            if image_id in nodes:
                edge(image_id,action["action_id"],"captured by")
        for decision in snap["decisions"]:
            parent_id=decision["parent_run_id"]
            child_id=decision["terminal"].get("child_run_id")
            if parent_id in nodes and child_id in nodes:
                key="decision:"+parent_id
                node(key,decision["requested_view"],"view_decision",record=decision)
                edge(key,parent_id,"requested by")
                edge(child_id,key,"followed view decision")
        return dict(nodes=list(nodes.values()), edges=list(edges.values()))

    def jsonld(self):
        graph = self.graph()
        # Local vocabulary is deliberately not represented as an agreed NHM ontology.
        return {"@context": {"@version": 1.1, "lab": "urn:specimen-lab:", "prov": "http://www.w3.org/ns/prov#", "record": {"@id": "lab:record", "@type": "@json"}},
                "@graph": [{"@id": "urn:specimen-lab:"+n["id"], "@type": "lab:"+n["kind"], "lab:label": n["label"], "record": n.get("record", {})} for n in graph["nodes"]] +
                          [{"@id": "urn:specimen-lab:"+e["source"], "lab:"+e["label"].replace(" ", "_"): {"@id": "urn:specimen-lab:"+e["target"]}} for e in graph["edges"]]}

    def audit(self):
        """Check durable evidence against stored hashes, without changing a run."""
        snap=self.snapshot()
        errors=[]
        for i in snap['images']:
            try:self.image_path(i['id'],verify=True)
            except (ValueError,OSError) as exc:errors.append(i['id']+': '+str(exc))
        for run in snap['runs']:
            folder=self.root/'runs'/run['id']
            try:
                request=json.loads((folder/'request.json').read_text())
                if request['id']!=run['id'] or request['image_ids']!=run['image_ids']:
                    raise ValueError('Request/index mismatch')
                if hashlib.sha256((folder/'prompt.txt').read_bytes()).hexdigest()!=run['prompt_sha256']:
                    raise ValueError('Prompt hash mismatch')
                if digest(json.loads((folder/'schema.json').read_text()))!=run['schema_sha256']:
                    raise ValueError('Schema hash mismatch')
                if run['state']=='running':
                    raise ValueError('Run lacks terminal state; inspect before retrying')
                if hashlib.sha256((folder/'raw-response.txt').read_bytes()).hexdigest()!=run['raw_sha256']:
                    raise ValueError('Raw response hash mismatch')
                terminal=json.loads((folder/'terminal.json').read_text())
                if terminal['state']!=run['state'] or terminal['raw_sha256']!=run['raw_sha256']:
                    raise ValueError('Terminal/index mismatch')
                if run['state']=='usable' and json.loads((folder/'raw-response.txt').read_text())!=run['result']:
                    raise ValueError('Parsed result differs from raw response')
            except (ValueError,OSError,KeyError) as exc:errors.append(run['id']+': '+str(exc))
        return dict(ok=not errors,errors=errors,images=len(snap['images']),runs=len(snap['runs']),
                    usable_runs=sum(r['state']=='usable' for r in snap['runs']),reviews=len(snap['reviews']),
                    note='Evidence integrity only; biological accuracy remains unverified')
