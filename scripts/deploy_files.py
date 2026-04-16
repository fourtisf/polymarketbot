#!/usr/bin/env python3
"""
Deploy updated files to VPS without git.
Decodes gzipped base64 data and writes files.
Run: venv/bin/python3 scripts/deploy_files.py
"""
import base64
import gzip
import sys
import os

# Files to deploy - populated by the deploy command
FILES = {}

def deploy():
    os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    print(f"Deploying to: {os.getcwd()}")

    for path, data_b64 in FILES.items():
        try:
            data = gzip.decompress(base64.b64decode(data_b64))
            # Backup
            if os.path.exists(path):
                bak = path + ".bak"
                with open(bak, 'rb') as f:
                    old = f.read() if os.path.exists(bak) else b''
                with open(path, 'rb') as f:
                    old = f.read()
                with open(bak, 'wb') as f:
                    f.write(old)
            with open(path, 'wb') as f:
                f.write(data)
            print(f"  OK: {path} ({len(data)} bytes)")
        except Exception as e:
            print(f"  FAIL: {path}: {e}")
            sys.exit(1)

    # Verify syntax
    import py_compile
    for path in FILES:
        if path.endswith('.py'):
            try:
                py_compile.compile(path, doraise=True)
                print(f"  Syntax OK: {path}")
            except py_compile.PyCompileError as e:
                print(f"  SYNTAX ERROR: {path}: {e}")
                print("  Restoring backup...")
                bak = path + ".bak"
                if os.path.exists(bak):
                    os.rename(bak, path)
                sys.exit(1)

    print("\nAll files deployed successfully!")
    print("Run: pm2 restart polymarket-bot")

if __name__ == "__main__":
    deploy()
