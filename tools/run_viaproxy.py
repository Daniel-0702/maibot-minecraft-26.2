"""一键下载并启动 ViaProxy（把 26.2 服务器转成插件支持的 1.20.1）。

用法（在插件目录下）::

    python tools/run_viaproxy.py
    python tools/run_viaproxy.py --dir D:\\ViaProxy --no-launch
    python tools/run_viaproxy.py --jar D:\\ViaProxy\\ViaProxy.jar

脚本会：

1. 找 Java（PATH / JAVA_HOME / Windows 常见安装目录）；
2. 没找到就打开 Adoptium 下载页并给出安装说明；
3. 从 GitHub 下载最新 ViaProxy（Java 8 环境自动选 ``+java8`` 构建）；
4. 启动图形界面，提示该填什么。

ViaProxy 的图形界面是 Java 写的，需要 **Java 17+**（推荐 21）才能打开。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import webbrowser
from pathlib import Path

API_LATEST = "https://api.github.com/repos/ViaVersion/ViaProxy/releases/latest"
RELEASES_PAGE = "https://github.com/ViaVersion/ViaProxy/releases/latest"
ADOPTIUM_PAGE = "https://adoptium.net/temurin/releases/?version=21"
USER_AGENT = "MaiBot-Minecraft-Plugin/1.0"


def parse_java_major(output: str) -> int:
    """从 ``java -version`` 输出里解析主版本号；解析失败返回 0。"""
    match = re.search(r'version\s+"(\d+)(?:\.(\d+))?', output)
    if not match:
        return 0
    first = int(match.group(1))
    if first == 1 and match.group(2):
        return int(match.group(2))
    return first


def find_java() -> str | None:
    """在 PATH、JAVA_HOME 和 Windows 常见目录里找 java.exe。"""
    candidate = shutil.which("java")
    if candidate:
        return candidate

    home = os.environ.get("JAVA_HOME", "").strip()
    if home:
        for name in ("java.exe", "java"):
            path = Path(home) / "bin" / name
            if path.exists():
                return str(path)

    if os.name == "nt":
        patterns = [
            r"C:\Program Files\Eclipse Adoptium\*\bin\java.exe",
            r"C:\Program Files\Java\jdk*\bin\java.exe",
            r"C:\Program Files\Java\jre*\bin\java.exe",
            r"C:\Program Files\Microsoft\jdk*\bin\java.exe",
            r"C:\Program Files\Zulu\*\bin\java.exe",
            r"C:\Program Files\BellSoft\*\bin\java.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\Eclipse Adoptium\*\bin\java.exe"),
        ]
        for pattern in patterns:
            matches = _glob(pattern)
            if matches:
                return str(matches[0])
    return None


def _glob(pattern: str) -> list[Path]:
    from glob import glob

    return sorted((Path(p) for p in glob(pattern)), reverse=True)


def java_major(java: str) -> int:
    try:
        result = subprocess.run(
            [java, "-version"],
            capture_output=True,
            text=True,
            timeout=15,
            encoding="utf-8",
            errors="replace",
        )
    except Exception:
        return 0
    output = (result.stderr or "") + (result.stdout or "")
    return parse_java_major(output)


def fetch_latest_release() -> dict:
    request = urllib.request.Request(API_LATEST, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def pick_asset(assets: list[dict], major: int) -> dict | None:
    """按 Java 版本挑选 jar：Java 8 优先 ``+java8`` 构建。"""
    jars = [
        asset
        for asset in assets
        if str(asset.get("name", "")).lower().endswith(".jar")
        and "source" not in str(asset.get("name", "")).lower()
    ]
    if not jars:
        return None
    prefer_java8 = 0 < major <= 8
    preferred = [asset for asset in jars if ("+java8" in asset["name"]) == prefer_java8]
    return (preferred or jars)[0]


def find_existing_jar(folder: Path) -> Path | None:
    """在目录里找已有的 ViaProxy*.jar（下载时文件名带版本号，不一定是 ViaProxy.jar）。"""
    candidates = sorted(
        (path for path in folder.glob("ViaProxy*.jar") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def download(url: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=120) as response, target.open("wb") as handle:
        total = int(response.headers.get("Content-Length", "0") or 0)
        done = 0
        while True:
            block = response.read(1 << 20)
            if not block:
                break
            handle.write(block)
            done += len(block)
            if total:
                print(f"\r下载中 {done * 100 // total}% ({done // (1 << 20)}/{total // (1 << 20)} MiB)", end="")
            else:
                print(f"\r下载中 {done // (1 << 20)} MiB", end="")
    print()


def _explain_no_java() -> None:
    print("没有找到 Java。ViaProxy 是 Java 程序，必须先安装 Java 才能打开 .jar。")
    print("")
    print("推荐安装 Java 21（Windows x64 的 .msi，安装时勾选 Add to PATH）：")
    print(f"  {ADOPTIUM_PAGE}")
    print("")
    print("装完后关掉再重新打开 PowerShell / 命令提示符，运行：")
    print('  java -version')
    print("能看到版本号就说明成功了，再重新运行本脚本。")
    try:
        webbrowser.open(ADOPTIUM_PAGE)
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="下载并启动 ViaProxy（26.2 → 1.20.1 协议转换）")
    parser.add_argument("--dir", default=str(Path(__file__).resolve().parent / "viaproxy"), help="ViaProxy 存放目录")
    parser.add_argument("--jar", default="", help="已有 ViaProxy.jar 的路径")
    parser.add_argument("--no-launch", action="store_true", help="只下载，不启动")
    parser.add_argument("--force", action="store_true", help="强制重新下载")
    args = parser.parse_args(argv)

    workdir = Path(args.dir).expanduser().resolve()
    workdir.mkdir(parents=True, exist_ok=True)

    java = find_java()
    if not java:
        _explain_no_java()
        return 1

    major = java_major(java)
    print(f"找到 Java: {java}（主版本 {major or '未知'}）")
    if major and major < 8:
        print("Java 版本太旧（需要 8 以上，推荐 21），请先安装新版 Java。")
        webbrowser.open(ADOPTIUM_PAGE)
        return 1

    jar_path = Path(args.jar).expanduser().resolve() if args.jar else None
    target = jar_path or (workdir / "ViaProxy.jar")
    existing = find_existing_jar(workdir)
    if jar_path is None and not target.exists() and existing is not None:
        target = existing
        print(f"发现已有 jar：{target}")

    if jar_path is None and (args.force or not target.exists()):
        print("正在查询 ViaProxy 最新版本…")
        try:
            release = fetch_latest_release()
        except Exception as exc:
            print(f"查询 GitHub 失败：{exc}")
            print(f"请手动打开 {RELEASES_PAGE} 下载 ViaProxy.jar，放到 {workdir}")
            return 1
        asset = pick_asset(release.get("assets") or [], major)
        if asset is None:
            print(f"没有找到可下载的 jar，请手动打开 {RELEASES_PAGE}")
            return 1
        print(f"下载 {asset['name']}（{int(asset.get('size', 0)) // (1 << 20)} MiB）…")
        try:
            download(asset["browser_download_url"], target)
        except Exception as exc:
            print(f"下载失败：{exc}")
            print(f"请手动打开 {RELEASES_PAGE} 下载，放到 {workdir}")
            return 1
    elif target.exists():
        print(f"使用已有 jar：{target}")
    else:
        print(f"找不到 jar：{target}")
        return 1

    print("")
    print("准备启动 ViaProxy。启动后请在窗口里填写：")
    print("  1) Target Address：你的 26.2 服务器地址，例如 mc.example.com:25565")
    print("  2) Target Version：选 26.2（不是 1.20.1）")
    print("  3) Bind Address：0.0.0.0:25568")
    print("  4) 正版服：先在 Accounts 页登录微软账号")
    print("  5) 点 Start")
    print("")
    print("然后让插件连接 127.0.0.1:25568（插件配置 [server].host/port）。")
    if args.no_launch:
        print(f"已下载到 {target}（--no-launch，未启动）")
        return 0

    try:
        process = subprocess.Popen([java, "-jar", str(target)], cwd=str(target.parent))
    except Exception as exc:
        print(f"启动失败：{exc}")
        print(f"可以手动运行：\"{java}\" -jar \"{target}\"")
        return 1
    print(f"已启动（PID {process.pid}）。关闭 ViaProxy 窗口即停止代理。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
