import json
import urllib.request

API = "https://api.github.com/repos/ViaVersion/ViaProxy"
RAW = "https://raw.githubusercontent.com/ViaVersion/ViaProxy/main/"


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "dsh"})
    return json.load(urllib.request.urlopen(req, timeout=30))


repo = get(API)
branch = repo["default_branch"]
tree = get(API + f"/git/trees/{branch}?recursive=1")
paths = [item["path"] for item in tree["tree"]]
for path in paths:
    low = path.lower()
    if "loader" in low or "config" in low and path.endswith((".java", ".yml", ".md")):
        print(path)
