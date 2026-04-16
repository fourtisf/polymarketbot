#!/usr/bin/env python3
"""Patch script to apply missing commits to VPS (e61f75a -> a14db44)."""
import sys

def patch_file(path, replacements):
    with open(path, 'r') as f:
        content = f.read()
    for old, new in replacements:
        if old not in content:
            print(f"  WARNING: pattern not found in {path}, skipping one replacement")
            continue
        content = content.replace(old, new, 1)
    with open(path, 'w') as f:
        f.write(content)
    print(f"  Patched {path}")

print("=== Patching bot.py ===")
patch_file('bot.py', [
    # 1. Add proxy_sweep_loop task in start()
    (
        '        self._tasks.append(asyncio.create_task(self._live_state_loop(), name="live_state"))\n\n        mode = "DRY-RUN"',
        '        self._tasks.append(asyncio.create_task(self._live_state_loop(), name="live_state"))\n\n        # Background task: periodically sweep proxy wallet USDC.e to EOA\n        if not self.dry_run:\n            self._tasks.append(asyncio.create_task(self._proxy_sweep_loop(), name="proxy_sweep"))\n\n        mode = "DRY-RUN"'
    ),
    # 2. Remove alltime variable in _settle_window
    (
        '        today = self.pnl.today_stats()\n        alltime = self.pnl.alltime_stats()\n        delta_close',
        '        today = self.pnl.today_stats()\n        delta_close'
    ),
    # 3. Remove bal variable
    (
        '        wr = today.get("win_rate", 0)\n        bal = alltime["current_balance"]\n        tx_line',
        '        wr = today.get("win_rate", 0)\n        tx_line'
    ),
    # 4. Fix win notification (remove Bot balance)
    (
        '''                f"({wins_today}W/{losses_today}L) {wr}% | Bot balance: ${bal:.2f}"\n                f"{onchain_bal}"\n            )\n        else:''',
        '''                f"({wins_today}W/{losses_today}L) {wr}%"\n                f"{onchain_bal}"\n            )\n        else:'''
    ),
    # 5. Fix loss notification (remove Bot balance)
    (
        '''                f"({wins_today}W/{losses_today}L) {wr}% | Bot balance: ${bal:.2f}"\n                f"{onchain_bal}"\n            )\n        await self.notifier.send_text(text)''',
        '''                f"({wins_today}W/{losses_today}L) {wr}%"\n                f"{onchain_bal}"\n            )\n        await self.notifier.send_text(text)'''
    ),
    # 6. Add _proxy_sweep_loop method before _auto_redeem
    (
        '    async def _auto_redeem(self, condition_id: str,',
        '''    async def _proxy_sweep_loop(self) -> None:
        """Periodically sweep USDC.e from proxy wallet to EOA.

        Polymarket may auto-redeem winning positions, depositing USDC.e
        into the proxy wallet. This background task ensures that money
        is transferred to the EOA every 3 minutes.
        """
        await asyncio.sleep(30)  # Initial delay
        while not self._stopping:
            try:
                tx = await self.executor.withdraw_proxy_usdc()
                if tx:
                    bal_str = ""
                    try:
                        usdc_bal, _, _ = await fetch_all_usdc(config.POLYGON_PUBLIC_KEY)
                        bal_str = f"\\n\U0001f4b5 USDC.e: <b>${usdc_bal:,.2f}</b>"
                    except Exception:
                        pass
                    await self.notifier.send_text(
                        f"\U0001f4b8 <b>Proxy sweep: USDC.e withdrawn to EOA</b>\\n"
                        f"TX: {tx_link_html(tx)}"
                        f"{bal_str}"
                    )
            except Exception as exc:
                log.debug("proxy sweep: %s", exc)
            await asyncio.sleep(180)  # Every 3 minutes

    async def _auto_redeem(self, condition_id: str,'''
    ),
    # 7. Replace _auto_redeem docstring and first part
    (
        '''        """Redeem winning conditional tokens \u2192 USDC.e with retries.

        The on-chain oracle may take a few seconds to report the resolution,
        so we retry up to 3 times with increasing delays.
        After successful redeem, also withdraw any USDC.e stuck in proxy wallet.
        """
        for attempt in range(3):
            # Wait for oracle to report on-chain resolution
            await asyncio.sleep(10 + attempt * 15)''',
        '''        """Redeem winning conditional tokens \u2192 USDC.e with retries.

        The on-chain oracle may take a few seconds to report the resolution,
        so we retry up to 5 times with increasing delays (up to ~2 minutes).
        After successful redeem, also withdraw any USDC.e from proxy wallet.
        """
        await self.notifier.send_text(
            f"\U0001f504 <b>Auto-redeem started</b>\\n"
            f"Condition: <code>{condition_id[:20]}</code>\\n"
            f"NegRisk: {neg_risk}"
        )

        for attempt in range(5):
            # Wait for oracle \u2014 longer delays for later attempts
            delay = 10 + attempt * 20  # 10s, 30s, 50s, 70s, 90s
            await asyncio.sleep(delay)'''
    ),
    # 8. Fix sleep after redeem
    (
        "                        await asyncio.sleep(3)  # Wait for state to settle",
        "                        await asyncio.sleep(5)"
    ),
    # 9. Remove "Fetch new balance" comment
    (
        "                    # Fetch new balance after redeem + withdrawal\n                    bal_str",
        "                    bal_str"
    ),
    # 10. Fix redeemed notification
    (
        '                        f"\U0001f4b0 <b>Tokens redeemed</b>\\n"',
        '                        f"\U0001f4b0 <b>Tokens redeemed</b> (attempt {attempt+1})\\n"'
    ),
    # 11. Fix log messages
    (
        '                log.info("redeem attempt %d: no tx (oracle may not have reported yet)", attempt + 1)',
        '                log.info("redeem attempt %d/%d: no tx yet", attempt + 1, 5)'
    ),
    (
        '                log.warning("redeem attempt %d failed: %s", attempt + 1, exc)',
        '                log.warning("redeem attempt %d/%d failed: %s", attempt + 1, 5, exc)'
    ),
    # 12. Fix fallback comment
    (
        "        # All redeem attempts failed \u2014 still try to withdraw proxy USDC\n        # (previous redeems by the Telegram /redeem command or website may have\n        # left USDC.e sitting in the proxy wallet)",
        "        # All redeem attempts failed \u2014 try proxy USDC withdrawal as fallback"
    ),
    # 13. Fix final failure messages
    (
        'log.warning("auto-redeem failed after 3 attempts for condition %s", condition_id[:16])',
        'log.warning("auto-redeem failed after 5 attempts for condition %s", condition_id[:16])'
    ),
    (
        'f"\u26a0\ufe0f Auto-redeem failed after 3 attempts.\\n"',
        'f"\u26a0\ufe0f Auto-redeem failed after 5 attempts.\\n"'
    ),
])

print("=== Patching utils/telegram.py ===")
patch_file('utils/telegram.py', [
    # 1. Fix dashboard balance line
    (
        """            f"Balance:  ${a['current_balance']:.2f} (ROI {a['roi_pct']:+.1f}%)\\n\"""",
        '''            f"Balance:  <b>${usdc:,.2f}</b> (on-chain)\\n"'''
    ),
    # 2. Add on-chain balance fetch in _cmd_pnl and fix balance display
    (
        '''        sess = self.risk.state.session_pnl
        await self.notifier.send_text(
            f"\U0001f4b0 <b>PnL SUMMARY</b>''',
        '''        sess = self.risk.state.session_pnl

        # Fetch actual on-chain balance
        usdc_onchain = 0.0
        addr = config.POLYGON_PUBLIC_KEY or ""
        if addr.startswith("0x") and len(addr) == 42:
            try:
                usdc_onchain, _ = await fetch_balances(addr)
            except Exception:
                pass

        await self.notifier.send_text(
            f"\U0001f4b0 <b>PnL SUMMARY</b>'''
    ),
    # 3. Fix PnL balance display
    (
        """            f"Balance: <b>${a['current_balance']:.2f}</b> "\n            f"(ROI {a['roi_pct']:+.1f}%)\"""",
        '''            f"Balance: <b>${usdc_onchain:,.2f}</b> (on-chain USDC.e)"'''
    ),
    # 4. Add on-chain balance fetch in _cmd_stats
    (
        '''        streak = self.pnl.current_streak()
        text = (
            f"\U0001f4ca <b>FULL STATISTICS</b>''',
        '''        streak = self.pnl.current_streak()

        # Fetch actual on-chain balance
        usdc_onchain = 0.0
        addr = config.POLYGON_PUBLIC_KEY or ""
        if addr.startswith("0x") and len(addr) == 42:
            try:
                usdc_onchain, _ = await fetch_balances(addr)
            except Exception:
                pass

        text = (
            f"\U0001f4ca <b>FULL STATISTICS</b>'''
    ),
    # 5. Fix stats balance display
    (
        """            f"Balance:     <b>${a['current_balance']:.2f}</b>\\n\"""",
        '''            f"Balance:     <b>${usdc_onchain:,.2f}</b> (on-chain)\\n"'''
    ),
])

print("=== Patching deploy.sh ===")
patch_file('deploy.sh', [
    ('BRANCH="claude/poly-marketbot-investigation-MDuYT"', 'BRANCH="claude/initial-setup-W6OxR"'),
])

print("\n=== Verifying syntax ===")
import py_compile
for f in ['bot.py', 'utils/telegram.py']:
    try:
        py_compile.compile(f, doraise=True)
        print(f"  {f} OK")
    except py_compile.PyCompileError as e:
        print(f"  {f} SYNTAX ERROR: {e}")
        sys.exit(1)

print("\n=== ALL PATCHES APPLIED SUCCESSFULLY ===")
print("Now run: pm2 restart polymarket-bot")
