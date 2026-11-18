import json
import urllib.request

API = "https://api.github.com/repos/Mai-with-u/MaiBot"
RAW = "https://raw.githubusercontent.com/Mai-with-u/MaiBot/v1.4.5/"


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "dsh"})
    return json.load(urllib.request.urlopen(req, timeout=30))


sha = get(API + "/git/refs/tags/v1.4.5")["object"]["sha"]
commit = get(API + "/git/commits/" + sha)
tree = get(API + "/git/trees/" + commit["tree"]["sha"] + "?recursive=1")
paths = [item["path"] for item in tree["tree"] if item["path"].endswith(".py")]
candidates = [p for p in paths if "config" in p.lower() and p.startswith("src/")]
print("candidates", len(candidates))
for path in candidates:
    try:
        text = urllib.request.urlopen(RAW + path, timeout=30).read().decode("utf-8")
    except Exception:
        continue
    if "引用了不存在的联网服务" in text or "web_search" in text:
        print("###", path, len(text.splitlines()))
        for index, line in enumerate(text.splitlines(), 1):
            low = line.lower()
            if "web_search" in low or "不存在的联网" in line or "web_services" in low or "class Web" in line:
                print(index, line.strip()[:180])
