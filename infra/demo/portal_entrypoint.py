"""既存StreamlitとReact BFFを選択し、同じ内部8501で起動する。"""

import os
import sys
from pathlib import Path


def command(values=None):
    """起動引数へ資格情報を入れず、暗黙fallbackもしない。"""
    values = os.environ if values is None else values
    mode = values.get("PORTAL_UI_MODE", "streamlit")
    if mode == "streamlit":
        # 既存Docker CMDと同じ操作面・設定を維持する。
        return [
            "streamlit",
            "run",
            "infra/demo/app.py",
            "--server.address=0.0.0.0",
            "--server.port=8501",
            "--server.headless=true",
            "--browser.gatherUsageStats=false",
        ]
    if mode == "react":
        return [
            sys.executable,
            "-m",
            "uvicorn",
            "portal_gateway:create_app",
            "--factory",
            "--app-dir",
            str(Path(__file__).parent),
            "--host",
            "0.0.0.0",
            "--port",
            "8501",
            "--workers",
            "1",
            "--no-access-log",
            "--no-proxy-headers",
        ]
    raise ValueError("PORTAL_UI_MODE must be streamlit or react.")


def main():
    """既存の環境を保ったまま、選択したserverへprocessを置き換える。"""
    try:
        arguments = command()
    except ValueError:
        print("PORTAL_UI_MODE must be streamlit or react.", file=sys.stderr)
        return 2
    os.execvpe(arguments[0], arguments, dict(os.environ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
