"""把 workflow 這次產生的檔案推回 main，不和其他 workflow 的 bot commit 衝突（只用標準庫與 git）。

fetch.yml、news.yml、intraday.yml、longterm.yml 都用這支 commit 與 push。做法：
1. 先把這次的輸出檔（--paths，扣掉 --exclude 與 data/meta.json）和 data/meta.json 讀進記憶體；
2. 每次嘗試都從 origin/main 最新狀態開始（git fetch＋git reset --hard），把輸出檔寫回去；
3. meta.json 依模式合併：
   --meta-own 前綴…：只把這次的這些資料集蓋到 origin 的 meta 上（news.yml 用 news/，longterm.yml 用 longterm/）
   --meta-all --keep 前綴…：用這次的整份 meta，但 --keep 前綴的資料集保留 origin 上的（fetch.yml 用，保留 news/*、longterm/*）
   合併後重算 summary 與 cross_check_conflicts；
4. commit、push；被拒（別人剛推）就從第 2 步重來，最多 5 次。

用法：
  python publish.py --message "data ..." --paths data --exclude data/news data/tw/intraday.json data/longterm --meta-all --keep news/ longterm/
  python publish.py --message "news ..." --paths data/news --meta-own news/
  python publish.py --message "intraday ..." --paths data/tw/intraday.json
  python publish.py --message "longterm ..." --paths data/longterm --meta-own longterm/
"""
import argparse
import json
import os
import subprocess
import sys
import time

META = "data/meta.json"


def git(*args, check=True):
    r = subprocess.run(["git", *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r


def collect(paths, exclude):
    """回傳 {相對路徑: bytes}；data/meta.json 另外處理，不在這裡。"""
    skip = [os.path.normpath(e) for e in exclude] + [os.path.normpath(META)]

    def excluded(p):
        p = os.path.normpath(p)
        return any(p == e or p.startswith(e + os.sep) for e in skip)

    files = {}
    for path in paths:
        if os.path.isfile(path):
            candidates = [path]
        else:
            candidates = [os.path.join(d, f) for d, _, fs in os.walk(path) for f in fs]
        for p in candidates:
            if not excluded(p) and not p.endswith(".tmp"):
                with open(p, "rb") as fh:
                    files[os.path.normpath(p)] = fh.read()
    return files


def load_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def merge_meta(ours, upstream, own=(), keep=()):
    """own：只用這次的這些前綴資料集；keep：用這次的整份，但這些前綴保留 upstream 的。"""
    upstream = upstream or {"datasets": {}, "errors": []}
    if own:
        out = dict(upstream)
        ds = dict(upstream.get("datasets", {}))
        ds.update({k: v for k, v in ours.get("datasets", {}).items() if k.startswith(tuple(own))})
    else:
        out = dict(ours)
        ds = {k: v for k, v in ours.get("datasets", {}).items() if not k.startswith(tuple(keep))}
        ds.update({k: v for k, v in upstream.get("datasets", {}).items() if k.startswith(tuple(keep))})
    out["datasets"] = ds
    out["summary"] = {s: sum(1 for v in ds.values() if v.get("status") == s) for s in ("ok", "stale", "fail")}
    out["cross_check_conflicts"] = [k for k, v in ds.items() if v.get("cross_check_ok") is False]
    return out


def publish(message, paths, exclude=(), own=(), keep=(), meta_all=False, tries=5, wait=5):
    files = collect(paths, exclude)
    ours = load_json(META) if (own or meta_all) else None
    for i in range(tries):
        git("fetch", "-q", "origin", "main")
        git("reset", "-q", "--hard", "origin/main")
        for p, data in files.items():
            os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
            with open(p, "wb") as fh:
                fh.write(data)
        add = list(files)
        if ours is not None:
            merged = merge_meta(ours, load_json(META), own=own, keep=keep if meta_all else ())
            with open(META, "w", encoding="utf-8") as fh:
                json.dump(merged, fh, ensure_ascii=False, indent=1)
            add.append(META)
        if add:
            git("add", "-A", "--", *add)
        if git("diff", "--cached", "--quiet", check=False).returncode == 0:
            print("沒有變動，不 commit")
            return 0
        git("commit", "-q", "-m", message)
        r = git("push", "-q", "origin", "HEAD:main", check=False)
        if r.returncode == 0:
            print(f"已推送（第 {i + 1} 次嘗試）：{message}")
            return 0
        print(f"push 被拒，{wait * (i + 1)} 秒後從最新的 main 重來：{r.stderr.strip()[:200]}", file=sys.stderr)
        time.sleep(wait * (i + 1))
    print("push 一直失敗，放棄", file=sys.stderr)
    return 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--message", required=True)
    ap.add_argument("--paths", nargs="+", required=True)
    ap.add_argument("--exclude", nargs="*", default=[])
    ap.add_argument("--meta-own", nargs="*", default=[], help="只更新這些前綴的資料集")
    ap.add_argument("--meta-all", action="store_true", help="用這次的整份 meta（搭配 --keep）")
    ap.add_argument("--keep", nargs="*", default=[], help="--meta-all 時保留 origin 上這些前綴的資料集")
    a = ap.parse_args(argv)
    return publish(a.message, a.paths, a.exclude, a.meta_own, a.keep, a.meta_all)


if __name__ == "__main__":
    sys.exit(main())
