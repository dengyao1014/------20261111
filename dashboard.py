from __future__ import annotations

import argparse
import html
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

from demand_miner.settings import load_settings
from demand_miner.store import Store

log = logging.getLogger("dashboard")

LABELS = {
    "high": ("高", "#0f9d58"),
    "medium-high": ("较高", "#f4b400"),
    "medium": ("中", "#999"),
    "issue": ("issue", "#bbb"),
}

CSS = """
body{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;
     max-width:1100px;margin:40px auto;padding:0 20px;color:#222}
h1{font-size:22px}
.meta{color:#777;font-size:13px;margin-bottom:24px}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{text-align:left;padding:10px 8px;border-bottom:1px solid #eee;vertical-align:top}
th{color:#888;font-weight:500;font-size:12px}
.score{font-variant-numeric:tabular-nums;font-weight:600}
.tag{font-size:12px;padding:1px 6px;border-radius:4px;color:#fff}
.quote{color:#666;font-size:12px;margin-top:4px}
form{display:inline}
button{border:1px solid #ddd;background:#fff;border-radius:6px;padding:2px 8px;
       cursor:pointer;font-size:13px}
button:hover{background:#f5f5f5}
.voted{background:#eaf7ee;border-color:#9cd3ac}
h2{font-size:16px;margin-top:36px}
"""


def render(store: Store, days: int) -> str:
    rows = store.recent_demands(days)
    votes = store.votes()
    weights = store.source_weights()
    opportunities = [row for row in rows if row["is_opportunity"]]

    body = []
    for row in rows:
        demand = row["demand"]
        label, color = LABELS.get(row["label"], (row["label"], "#999"))
        vote = votes.get(row["key"])
        good_cls = "voted" if vote == 1 else ""
        bad_cls = "voted" if vote == -1 else ""
        title = html.escape(demand.get("title_en", ""))
        body.append(
            f"<tr>"
            f"<td class='score'>{row['score']}</td>"
            f"<td><a href='{html.escape(row['url'])}' target='_blank'>{title}</a>"
            f"<div class='quote'>{html.escape(demand.get('quote', '')[:180])}</div></td>"
            f"<td>{html.escape(row['platform'])}</td>"
            f"<td><span class='tag' style='background:{color}'>{html.escape(label)}</span></td>"
            f"<td>"
            f"<form method='post' action='/vote'><input type='hidden' name='key' value='{html.escape(row['key'])}'>"
            f"<input type='hidden' name='vote' value='1'>"
            f"<button class='{good_cls}'>👍</button></form> "
            f"<form method='post' action='/vote'><input type='hidden' name='key' value='{html.escape(row['key'])}'>"
            f"<input type='hidden' name='vote' value='-1'>"
            f"<button class='{bad_cls}'>👎</button></form>"
            f"</td></tr>"
        )

    weight_text = "、".join(f"{k}×{v}" for k, v in weights.items()) or "暂无足够反馈"
    return f"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><title>需求挖掘看板</title>
<style>{CSS}</style></head><body>
<h1>📊 需求挖掘看板</h1>
<div class="meta">近 {days} 天 · {len(rows)} 条需求 · {len(opportunities)} 条通过硬门槛 ·
来源权重：{html.escape(weight_text)}</div>
<table>
<thead><tr><th>分</th><th>需求</th><th>来源</th><th>潜力</th><th>反馈</th></tr></thead>
<tbody>{''.join(body) or "<tr><td colspan='5'>还没有数据，先跑一轮 main.py</td></tr>"}</tbody>
</table>
<h2>说明</h2>
<p class="meta">👍/👎 会写入 feedback 表；某个来源累计 5 票以上时，好评率 ≥70% 来源权重 ×1.15，
≤30% ×0.85，下一轮打分自动生效。</p>
</body></html>"""


def build_handler(store: Store, days: int):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.split("?")[0] != "/":
                self.send_error(404)
                return
            payload = render(store, days).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):
            if self.path != "/vote":
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length", "0"))
            form = parse_qs(self.rfile.read(length).decode("utf-8"))
            key = form.get("key", [""])[0]
            try:
                vote = int(form.get("vote", ["0"])[0])
            except ValueError:
                vote = 0
            if key and vote:
                store.vote(key, vote)
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()

        def log_message(self, fmt, *args):
            log.info("%s - %s", self.address_string(), fmt % args)

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description="需求挖掘看板")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--days", type=int, default=14)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    settings = load_settings()
    store = Store(settings.db_path)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), build_handler(store, args.days))
    print(f"看板已启动：http://127.0.0.1:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        store.close()


if __name__ == "__main__":
    main()
