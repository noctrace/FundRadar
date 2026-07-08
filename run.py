"""
Fund-Radar 启动入口
====================
用法：python run.py
访问：http://localhost:8080
      http://localhost:8080?y1=100&m6=60&m3=40&m1=25
      http://localhost:8080?tab=daily
"""

import http.server
import socketserver
import urllib.parse
import json
import sys
import io
import os
from datetime import datetime
from pathlib import Path

from fund_data import get_page_data, get_daily_surge_data

# 修复 Windows 控制台编码
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

PORT = 8080
BASE_DIR = Path(__file__).parent

# 启动时读取 HTML 模板
TEMPLATE_PATH = BASE_DIR / "template.html"
with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
    HTML_TEMPLATE = f.read()


class FundRadarHandler(http.server.BaseHTTPRequestHandler):
    """处理 HTTP 请求，解析 URL 参数并返回渲染后的页面。"""

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)

        # 静态文件（icon）
        if parsed.path.startswith('/icon/'):
            file_path = os.path.join(BASE_DIR, parsed.path.lstrip('/'))
            if os.path.exists(file_path):
                self.send_response(200)
                if file_path.endswith('.jpg') or file_path.endswith('.jpeg'):
                    self.send_header("Content-Type", "image/jpeg")
                elif file_path.endswith('.png'):
                    self.send_header("Content-Type", "image/png")
                elif file_path.endswith('.ico'):
                    self.send_header("Content-Type", "image/x-icon")
                self.end_headers()
                with open(file_path, 'rb') as f:
                    self.wfile.write(f.read())
                return
            else:
                self.send_response(404)
                self.end_headers()
                return

        # 每日涨跌 API（返回 JSON）
        if parsed.path == "/api/daily":
            print(f"\n{'='*60}")
            print(f"[API] /api/daily - 每日飙升/暴跌基金")
            print(f"{'='*60}")
            try:
                data = get_daily_surge_data()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps(data, ensure_ascii=False).encode('utf-8'))
            except Exception as e:
                print(f"[API错误] /api/daily: {e}")
                import traceback
                traceback.print_exc()
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}, ensure_ascii=False).encode('utf-8'))
            return

        # 解析参数（默认值 100%, 60%, 40%, 25%）
        params = urllib.parse.parse_qs(parsed.query)
        y1 = float(params.get("y1", ["100"])[0])
        m6 = float(params.get("m6", ["60"])[0])
        m3 = float(params.get("m3", ["40"])[0])
        m1 = float(params.get("m1", ["25"])[0])

        print(f"\n{'='*60}")
        print(f"[请求] {self.path}")
        print(f"[参数] y1={y1}%, m6={m6}%, m3={m3}%, m1={m1}%")
        print(f"{'='*60}")

        try:
            data = get_page_data(y1, m6, m3, m1)

            html = HTML_TEMPLATE.format(
                update_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                y1=int(y1),
                m6=int(m6),
                m3=int(m3),
                m1=int(m1),
                fund_count=data["fund_count"],
                fund_data=json.dumps(data["fund_data"], ensure_ascii=False),
                loss_fund_count=data["loss_fund_count"],
                loss_fund_data=json.dumps(data["loss_fund_data"], ensure_ascii=False),
            )

            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode('utf-8'))

        except Exception as e:
            print(f"[错误] {e}")
            self.send_response(500)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            error_html = f"<h1>服务器错误</h1><pre>{e}</pre>"
            self.wfile.write(error_html.encode('utf-8'))

    def log_message(self, format, *args):
        pass  # 静默日志


if __name__ == "__main__":
    print(f"{'='*60}")
    print(f"  Fund-Radar 全链路生产级服务 v3 (单页标签式)")
    print(f"  访问地址: http://localhost:{PORT}")
    print(f"  带参示例: http://localhost:{PORT}?y1=100&m6=60&m3=40&m1=25")
    print(f"  每日涨跌: http://localhost:{PORT}?tab=daily")
    print(f"  按 Ctrl+C 停止服务器")
    print(f"{'='*60}")

    with socketserver.TCPServer(("", PORT), FundRadarHandler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n服务器已停止")
