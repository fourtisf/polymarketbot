module.exports = {
  apps: [{
    name: "polymarket-5m-bot",
    script: "bot.py",
    interpreter: "python3",
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
