"""publish.py 的測試：用暫存的 bare repo 和多個 clone 模擬 workflow 同時推送，不連網。

執行：python -m unittest discover -s tests（在 repo 根目錄；需要 git）
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import publish  # noqa: E402


def run(*args, cwd):
    return subprocess.run(list(args), cwd=cwd, capture_output=True, text=True, check=True).stdout


def write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        if isinstance(obj, str):
            fh.write(obj)
        else:
            json.dump(obj, fh, ensure_ascii=False, indent=1)


def read_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


@unittest.skipUnless(shutil.which("git"), "需要 git")
class PublishTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.remote = os.path.join(self.tmp, "remote.git")
        run("git", "init", "-q", "--bare", "-b", "main", self.remote, cwd=self.tmp)
        seed = self.clone("seed")
        write(os.path.join(seed, "data", "meta.json"),
              {"datasets": {"us/VOO": {"status": "ok", "last": "2026-10-21"}}, "errors": []})
        write(os.path.join(seed, "data", "us", "VOO.csv"), "2026-10-21,1,1,1,1,700\n")
        run("git", "add", "-A", cwd=seed)
        run("git", "commit", "-q", "-m", "seed", cwd=seed)
        run("git", "push", "-q", "origin", "HEAD:main", cwd=seed)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def clone(self, name):
        path = os.path.join(self.tmp, name)
        run("git", "clone", "-q", self.remote, path, cwd=self.tmp)
        run("git", "config", "user.name", "test", cwd=path)
        run("git", "config", "user.email", "test@example.com", cwd=path)
        run("git", "checkout", "-q", "-B", "main", cwd=path)
        return path

    def publish(self, cwd, *args):
        r = subprocess.run([sys.executable, os.path.join(ROOT, "publish.py"), *args], cwd=cwd,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def remote_state(self):
        c = self.clone(f"check{len(os.listdir(self.tmp))}")
        return c, read_json(os.path.join(c, "data", "meta.json"))

    def test_news_then_stale_fetch_keeps_both(self):
        fetch = self.clone("fetch")  # fetch 先 checkout，跑 3 分鐘
        news = self.clone("news")
        # news 這段時間先推：自己的檔案＋meta 的 news/*
        write(os.path.join(news, "data", "news", "headlines.json"), {"items": [1]})
        meta = read_json(os.path.join(news, "data", "meta.json"))
        meta["datasets"]["news/headlines"] = {"status": "ok", "last": "2026-10-22 08:21"}
        write(os.path.join(news, "data", "meta.json"), meta)
        self.publish(news, "--message", "news", "--paths", "data/news", "--meta-own", "news/")
        # fetch 用舊的 checkout 產生整份 meta（沒有 news/*）
        write(os.path.join(fetch, "data", "us", "VOO.csv"), "2026-10-21,1,1,1,1,700\n2026-10-22,1,1,1,1,705\n")
        write(os.path.join(fetch, "data", "meta.json"),
              {"datasets": {"us/VOO": {"status": "ok", "last": "2026-10-22"}}, "errors": []})
        self.publish(fetch, "--message", "data", "--paths", "data", "--exclude", "data/news",
                     "--meta-all", "--keep", "news/")
        c, meta = self.remote_state()
        self.assertEqual(meta["datasets"]["us/VOO"]["last"], "2026-10-22")
        self.assertEqual(meta["datasets"]["news/headlines"]["last"], "2026-10-22 08:21")
        self.assertEqual(meta["summary"], {"ok": 2, "stale": 0, "fail": 0})
        self.assertTrue(os.path.exists(os.path.join(c, "data", "news", "headlines.json")))
        with open(os.path.join(c, "data", "us", "VOO.csv")) as fh:
            self.assertIn("705", fh.read())
        log = run("git", "log", "--format=%s", cwd=c).split()
        self.assertEqual(log[:2], ["data", "news"])

    def test_news_after_fetch_only_touches_news(self):
        news = self.clone("news")  # news 先 checkout
        fetch = self.clone("fetch")
        write(os.path.join(fetch, "data", "meta.json"),
              {"datasets": {"us/VOO": {"status": "stale", "last": "2026-10-21"}}, "errors": ["x"]})
        self.publish(fetch, "--message", "data", "--paths", "data", "--exclude", "data/news",
                     "--meta-all", "--keep", "news/")
        write(os.path.join(news, "data", "news", "headlines.json"), {"items": [2]})
        meta = read_json(os.path.join(news, "data", "meta.json"))  # 舊的 meta：us/VOO 還是 ok
        meta["datasets"]["news/headlines"] = {"status": "partial", "last": "2026-10-22 08:21"}
        write(os.path.join(news, "data", "meta.json"), meta)
        self.publish(news, "--message", "news", "--paths", "data/news", "--meta-own", "news/")
        _, meta = self.remote_state()
        self.assertEqual(meta["datasets"]["us/VOO"]["status"], "stale")  # 沒被 news 的舊 meta 蓋回去
        self.assertEqual(meta["errors"], ["x"])
        self.assertEqual(meta["datasets"]["news/headlines"]["status"], "partial")

    def test_plain_file_and_no_change(self):
        a = self.clone("a")
        write(os.path.join(a, "data", "tw", "intraday.json"), {"session": "盤中"})
        self.assertIn("已推送", self.publish(a, "--message", "intraday", "--paths", "data/tw/intraday.json"))
        self.assertIn("沒有變動", self.publish(a, "--message", "intraday", "--paths", "data/tw/intraday.json"))

    def test_merge_meta_recomputes_summary(self):
        ours = {"datasets": {"us/A": {"status": "ok"}, "news/headlines": {"status": "fail"}}}
        up = {"datasets": {"news/headlines": {"status": "ok"}, "us/B": {"status": "ok", "cross_check_ok": False}}}
        m = publish.merge_meta(ours, up, keep=("news/",))
        self.assertEqual(m["datasets"]["news/headlines"]["status"], "ok")
        self.assertNotIn("us/B", m["datasets"])
        m = publish.merge_meta(ours, up, own=("news/",))
        self.assertEqual(m["datasets"]["news/headlines"]["status"], "fail")
        self.assertEqual(m["cross_check_conflicts"], ["us/B"])


if __name__ == "__main__":
    unittest.main()
