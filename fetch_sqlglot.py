"""Download sqlglot at the pinned commit (allowed: sqlglot's own repo at the pinned commit)."""
import sys, tarfile, io, urllib.request, shutil, os
SHA = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
dest = sys.argv[1]
url = f"https://codeload.github.com/tobymao/sqlglot/tar.gz/{SHA}"
data = urllib.request.urlopen(url, timeout=120).read()
os.makedirs(dest, exist_ok=True)
with tarfile.open(fileobj=io.BytesIO(data)) as tf:
    for m in tf.getmembers():
        parts = m.name.split("/", 1)
        if len(parts) < 2 or not parts[1]:
            continue
        m.name = parts[1]
        tf.extract(m, dest, filter="data")
print("fetched", SHA, file=sys.stderr)
