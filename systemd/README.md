# Snapshot logger systemd deployment

## On the live VPS

If the bot venv is at `/home/bot/TestCode1/venv/`, swap `/usr/bin/python3` →
`/home/bot/TestCode1/venv/bin/python` in both `.service` files before copying.

```bash
mkdir -p /home/bot/TestCode1/logs /home/bot/TestCode1/snapshot_parquet
sudo cp systemd/snapshot_logger.service /etc/systemd/system/
sudo cp systemd/snapshot_rollup.service /etc/systemd/system/
sudo cp systemd/snapshot_rollup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now snapshot_logger.service
sudo systemctl enable --now snapshot_rollup.timer
sudo systemctl status snapshot_logger
```

## Verify operation

```bash
journalctl -u snapshot_logger -f          # watch live
sqlite3 weather_bot.db "SELECT COUNT(*) FROM bucket_snapshots"
ls -lh snapshot_parquet/
```

## On the local machine (after VPS deployment)

Add to cron (`crontab -e`):

```cron
15 0 * * * /f/CodeProjects/TestCode1/scripts/snapshot_rsync.sh >> /f/CodeProjects/TestCode1/logs/snapshot_rsync.log 2>&1
```
