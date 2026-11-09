#!/usr/bin/env python3
"""Fleet-watch triage replay — staged 2026-11-06 for Monday's beta review.

Replays the beta's REAL saved per-group triage inputs (from the Pi and Q
state dirs) through a 3x2 grid: models [qwen3.5:4b, qwen3:8b, qwen3:14b]
x prompts [v1 = the live triage-prompt.txt, v2 = tightened]. Each call
mirrors production's call_model() exactly: POST /api/generate with
prompt = prompt_file + findings header + input, think off,
options {num_predict: 150}, no temperature (production sets none).

Mechanical scoring assists only (leads, not verdicts):
- novel_numbers: numeric tokens in the response that do not appear in
  the input (float-normalized compare).
- placeholders: <...> tokens left in the response.
- marker_hits: counts of because / due to / will / grown (invention-class
  trigger words for the human read).
- delivered: production's strip_model_text() applied (VERDICT/SUGGESTION
  lines dropped, max 4 lines) — what would actually reach the phone.

Writes ~/fleet-watch-replay/out/results.json and summary.md.
Run on the ProDesk (talks to localhost:11434).
"""
import json, os, re, sys, time, urllib.request

BASE = os.path.expanduser("~/fleet-watch-replay")
INDIR = os.path.join(BASE, "inputs")
OUTDIR = os.path.join(BASE, "out")
os.makedirs(OUTDIR, exist_ok=True)
URL = "http://127.0.0.1:11434/api/generate"
MODELS = ["qwen3.5:4b", "qwen3:8b", "qwen3:14b"]
PROMPTS = {"v1": "prompt-v1.txt", "v2": "prompt-v2.txt"}
HEADER = ("\n\n# CURRENT FINDINGS — one issue group "
          "(times America/Chicago)\n")
MARKERS = ["because", "due to", "caused by", "will ", "grown", "has grown",
           "immediately", "critical"]


def numbers(text):
    out = set()
    for tok in re.findall(r"\d+(?:[.,]\d+)*%?", text):
        t = tok.replace(",", "").rstrip("%")
        try:
            out.add(str(float(t)))
        except ValueError:
            out.add(t)
    return out


def strip_model_text(text):
    kept = []
    for ln in text.splitlines():
        if re.match(r"(VERDICT|SUGGESTION):", ln.strip(), re.I):
            continue
        ln = ln.strip()
        if ln:
            kept.append(ln)
    return "\n".join(kept[:4])


def ask(model, prompt_text):
    body = json.dumps({
        "model": model, "prompt": prompt_text, "stream": False,
        "think": False, "options": {"num_predict": 150},
    }).encode()
    req = urllib.request.Request(URL, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.load(r)["response"]


def main():
    prompts = {}
    for tag, fn in PROMPTS.items():
        with open(os.path.join(INDIR, fn), encoding="utf-8") as f:
            prompts[tag] = f.read()
    inputs = {}
    for fn in sorted(os.listdir(INDIR)):
        if fn.endswith(".txt") and not fn.startswith("prompt-"):
            with open(os.path.join(INDIR, fn), encoding="utf-8") as f:
                inputs[fn[:-4]] = f.read()
    print("inputs:", ", ".join(inputs), flush=True)
    results = []
    for name, inp in inputs.items():
        in_nums = numbers(inp)
        for model in MODELS:
            for ptag in ("v1", "v2"):
                t0 = time.time()
                try:
                    resp = ask(model, prompts[ptag] + HEADER + inp)
                    err = ""
                except Exception as e:
                    resp, err = "", str(e)
                dt = time.time() - t0
                novel = sorted(numbers(resp) - in_nums) if resp else []
                low = resp.lower()
                rec = {
                    "input": name, "model": model, "prompt": ptag,
                    "seconds": round(dt, 1), "error": err,
                    "raw": resp, "delivered": strip_model_text(resp),
                    "novel_numbers": novel,
                    "placeholders": re.findall(r"<[a-z_][a-z0-9_]*>", low),
                    "marker_hits": {m: low.count(m) for m in MARKERS
                                    if low.count(m)},
                }
                results.append(rec)
                print("%s | %s | %s | %.1fs | novel=%s ph=%s markers=%s"
                      % (name, model, ptag, dt, novel,
                         rec["placeholders"], rec["marker_hits"]),
                      flush=True)
    with open(os.path.join(OUTDIR, "results.json"), "w") as f:
        json.dump(results, f, indent=1)
    lines = ["# Triage replay — mechanical summary",
             "(novel_numbers = numbers in the response not present in the "
             "input; a lead for the human read, not a verdict)",
             "",
             "| input | model | prompt | novel# | placeholders | markers |",
             "|---|---|---|---|---|---|"]
    for r in results:
        lines.append("| %s | %s | %s | %s | %s | %s |" % (
            r["input"], r["model"], r["prompt"],
            ", ".join(r["novel_numbers"]) or "-",
            ", ".join(r["placeholders"]) or "-",
            ", ".join("%s:%d" % kv for kv in r["marker_hits"].items()) or "-"))
    with open(os.path.join(OUTDIR, "summary.md"), "w") as f:
        f.write("\n".join(lines) + "\n")
    print("saved", os.path.join(OUTDIR, "results.json"))


if __name__ == "__main__":
    sys.exit(main())
