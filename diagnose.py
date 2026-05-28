import sys
import platform
import traceback
from datetime import datetime

import akshare as ak
import requests


def env_info():
    print("Python:", sys.executable, platform.python_version())
    try:
        import pip
        print("pip:", pip.__version__)
    except Exception:
        pass
    try:
        import akshare
        print("akshare:", akshare.__version__)
    except Exception:
        print("akshare: not importable")


def test_http():
    urls = ["https://api.ipify.org?format=json", "https://www.baidu.com"]
    for u in urls:
        try:
            r = requests.get(u, timeout=10)
            print(u, "->", r.status_code)
        except Exception as e:
            print(u, "-> error:", repr(e))


def fetch_one(code):
    print("\n== Testing code:", code)
    try:
        df = ak.stock_zh_a_hist(symbol=code, period="daily", start_date="20150101", end_date=datetime.now().strftime("%Y%m%d"), adjust="")
        print("Returned type:", type(df), "len:" , None if df is None else (len(df) if hasattr(df, '__len__') else 'n/a'))
        if df is not None and hasattr(df, 'head'):
            try:
                print(df.head(3))
            except Exception:
                pass
    except Exception:
        traceback.print_exc()


if __name__ == '__main__':
    print("Diagnostics start")
    env_info()
    test_http()
    # sample failing codes observed
    codes = ["000001", "000002", "000004", "600000"]
    for c in codes:
        fetch_one(c)
    print("Diagnostics end")
