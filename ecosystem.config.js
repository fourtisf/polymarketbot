// Resolve project root once (so the venv path is correct regardless of cwd
// when pm2 reads the file).
const path = require("path");
const ROOT = __dirname;

module.exports = {
  apps: [{
    name: "polymarket-5m-bot",
    cwd: ROOT,
    script: "bot.py",
    // Use the project-local virtualenv. PEP 668 (Ubuntu 24.04+) blocks
    // installing into system Python, so deps live in ./venv. Bootstrap with:
    //   python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
    interpreter: path.join(ROOT, "venv", "bin", "python3"),
    watch: false,
    autorestart: true,
    max_restarts: 10,
    restart_delay: 5000,
    env: {
      NODE_ENV: "production",
      PYTHONUNBUFFERED: "1"
    },
    log_date_format: "YYYY-MM-DD HH:mm:ss",
    error_file: "./logs/error.log",
    out_file: "./logs/output.log",
    merge_logs: true
  }]
};
