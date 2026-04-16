#!/usr/bin/env python3
"""Patch #2: Add /pos and /exit commands to telegram bot."""
import sys, os
os.chdir(os.path.dirname(os.path.abspath(__file__)))

def patch(path, replacements):
    with open(path) as f:
        c = f.read()
    for old, new in replacements:
        if old not in c:
            print(f"  SKIP: pattern not found in {path}")
            continue
        c = c.replace(old, new, 1)
    with open(path, 'w') as f:
        f.write(c)
    print(f"  Patched {path}")

print("=== Adding /pos and /exit commands ===")
patch('utils/telegram.py', [
    # 1. Add command handlers
    (
        '            "/redeem": self._cmd_redeem,\n            "/claim": self._cmd_redeem,',
        '            "/redeem": self._cmd_redeem,\n            "/claim": self._cmd_redeem,\n            "/pos": self._cmd_position,\n            "/position": self._cmd_position,\n            "/exit": self._cmd_exit,\n            "/close": self._cmd_exit,'
    ),
    # 2. Add callback handlers
    (
        '        # Wallet refresh / redeem\n        elif data == "wallet:refresh":\n            await self._cmd_wallet([], chat_id)\n        elif data == "wallet:redeem":\n            await self._cmd_redeem([], chat_id)',
        '        # Wallet refresh / redeem / position\n        elif data == "wallet:refresh":\n            await self._cmd_wallet([], chat_id)\n        elif data == "wallet:redeem":\n            await self._cmd_redeem([], chat_id)\n        elif data == "nav:position":\n            await self._cmd_position([], chat_id)\n        elif data == "nav:exit":\n            await self._cmd_exit([], chat_id)'
    ),
    # 3. Add position info and buttons to dashboard
    (
        '''        text = (
            f"\U0001f3e0 <b>DASHBOARD</b>\\n"
            f"{status} \xb7 {mode}\\n"
            f"{BAR}\\n"
            f"\U0001f45b {wallet_link_html(addr, addr)}\\n"
            f"\U0001f4b5 ${usdc:,.2f} \xb7 \u26fd {pol:,.4f}\\n"
            f"{BAR}\\n"
            f"Session:  <b>{session_pnl:+.2f}</b> {_ico(session_pnl)}\\n"
            f"Today:    {t['pnl']:+.2f} \xb7 {t['trades']}t \xb7 WR {t['win_rate']}%\\n"
            f"All-time: {a['pnl']:+.2f} \xb7 {a['trades']}t \xb7 WR {a['win_rate']}%\\n"''',
        '''        # Current position info
        pos_text = ""
        bot = self.trading_bot
        if bot and bot.state.entered_this_window and bot.state.entry_record:
            rec = bot.state.entry_record
            w = bot.state.window
            side = rec.get("side", "?")
            entry_px = rec.get("entry_price", 0)
            shares = rec.get("shares", 0)
            cost = rec.get("cost", 0)
            secs = w.seconds_remaining if w else 0
            cur_px = 0.0
            if w and bot.state.token_up_price and side == "UP":
                cur_px = bot.state.token_up_price
            elif w and bot.state.token_down_price and side == "DOWN":
                cur_px = bot.state.token_down_price
            cur_val = cur_px * shares if cur_px > 0 else 0
            unreal_pnl = cur_val - cost if cur_val > 0 else 0
            pos_text = (
                f"\\n\U0001f4cd <b>OPEN POSITION</b>\\n"
                f"Side: {side} | Entry: ${entry_px:.3f}\\n"
                f"Shares: {shares:.0f} | Cost: ${cost:.2f}\\n"
                f"Current: ${cur_px:.3f} | Value: ${cur_val:.2f}\\n"
                f"Unrealized: <b>{unreal_pnl:+.2f}</b> {_ico(unreal_pnl)}\\n"
                f"Closes in: {secs}s\\n"
            )

        text = (
            f"\U0001f3e0 <b>DASHBOARD</b>\\n"
            f"{status} \xb7 {mode}\\n"
            f"{BAR}\\n"
            f"\U0001f45b {wallet_link_html(addr, addr)}\\n"
            f"\U0001f4b5 ${usdc:,.2f} \xb7 \u26fd {pol:,.4f}\\n"
            f"{BAR}\\n"
            f"Session:  <b>{session_pnl:+.2f}</b> {_ico(session_pnl)}\\n"
            f"Today:    {t['pnl']:+.2f} \xb7 {t['trades']}t \xb7 WR {t['win_rate']}%\\n"
            f"All-time: {a['pnl']:+.2f} \xb7 {a['trades']}t \xb7 WR {a['win_rate']}%\\n"'''
    ),
    # 4. Fix balance line to include pos_text
    (
        '''            f"Balance:  <b>${usdc:,.2f}</b> (on-chain)\\n"
            f"{BAR}\\n"
            f"Pick an action:"''',
        '''            f"Balance:  <b>${usdc:,.2f}</b> (on-chain)"
            f"{pos_text}\\n"
            f"{BAR}\\n"
            f"Pick an action:"'''
    ),
    # 5. Add Position and Exit buttons to dashboard
    (
        '''        kb = {"inline_keyboard": [
            [
                {"text": "\u25b6\ufe0f Start Trading", "callback_data": "nav:go"},
                {"text": "\u23f8 Stop Trading",  "callback_data": "nav:stop"},
            ],
            [
                {"text": "\U0001f4ca Stats",  "callback_data": "nav:stats"},
                {"text": "\U0001f4b0 PnL",    "callback_data": "nav:pnl"},
            ],''',
        '''        kb = {"inline_keyboard": [
            [
                {"text": "\u25b6\ufe0f Start Trading", "callback_data": "nav:go"},
                {"text": "\u23f8 Stop Trading",  "callback_data": "nav:stop"},
            ],
            [
                {"text": "\U0001f4cd Position", "callback_data": "nav:position"},
                {"text": "\U0001f6aa Exit",     "callback_data": "nav:exit"},
            ],
            [
                {"text": "\U0001f4ca Stats",  "callback_data": "nav:stats"},
                {"text": "\U0001f4b0 PnL",    "callback_data": "nav:pnl"},
            ],'''
    ),
    # 6. Add command menu entries
    (
        '            {"command": "redeem", "description": "Claim winning positions \u2192 USDC.e"},',
        '            {"command": "pos", "description": "Current open position details"},\n            {"command": "exit", "description": "Close/sell current position"},\n            {"command": "redeem", "description": "Claim winning positions \u2192 USDC.e"},'
    ),
    # 7. Add /pos and /exit to help text
    (
        '            "/go     \u2014 Start auto trading\\n"\n            "/stop   \u2014 Stop auto trading\\n"\n            "/mode   \u2014 Toggle DRY-RUN / LIVE\\n"',
        '            "/go     \u2014 Start auto trading\\n"\n            "/stop   \u2014 Stop auto trading\\n"\n            "/pos    \u2014 Current open position details\\n"\n            "/exit   \u2014 Close/sell current position\\n"\n            "/mode   \u2014 Toggle DRY-RUN / LIVE\\n"'
    ),
    # 8. Add _cmd_position and _cmd_exit methods before _cmd_risk
    (
        '    async def _cmd_risk(self, args, chat_id):',
        '''    async def _cmd_position(self, args, chat_id):
        """Show current open position with real-time details."""
        bot = self.trading_bot
        if not bot or not bot.state.entered_this_window or not bot.state.entry_record:
            await self.notifier.send_text(
                "\U0001f4cd <b>NO OPEN POSITION</b>\\n"
                f"{BAR}\\n"
                "No active trade in current window.\\n"
                "Bot will enter on next signal.",
                chat_id=chat_id,
            )
            return

        rec = bot.state.entry_record
        w = bot.state.window
        side = rec.get("side", "?")
        entry_px = rec.get("entry_price", 0)
        shares = rec.get("shares", 0)
        cost = rec.get("cost", 0)
        confidence = rec.get("confidence", 0)
        order_id = rec.get("order_id", "")
        tx_hash = rec.get("tx_hash", "")
        condition_id = rec.get("condition_id", "")
        slug = rec.get("window_slug", "")
        secs = w.seconds_remaining if w else 0
        ptb = w.price_to_beat if w else 0
        btc_now = bot.state.current_btc or 0

        cur_px = 0.0
        if side == "UP" and bot.state.token_up_price:
            cur_px = bot.state.token_up_price
        elif side == "DOWN" and bot.state.token_down_price:
            cur_px = bot.state.token_down_price

        cur_val = cur_px * shares if cur_px > 0 else 0
        unreal_pnl = cur_val - cost if cur_val > 0 else 0

        delta_str = ""
        if ptb and btc_now:
            delta = (btc_now - ptb) / ptb * 100
            direction = "UP" if btc_now >= ptb else "DOWN"
            delta_str = f"BTC: ${btc_now:,.2f} ({delta:+.3f}%) = {direction}\\n"

        max_payout = shares * 1.0
        max_profit = max_payout - cost

        tx_line = f"TX: {tx_link_html(tx_hash)}\\n" if tx_hash else ""
        order_line = f"Order: <code>{order_id[:20]}</code>\\n" if order_id else ""
        market_lnk = market_link_html(slug) if slug else ""

        text = (
            f"\U0001f4cd <b>OPEN POSITION</b>\\n"
            f"{BAR}\\n"
            f"Side: <b>{side}</b> | Score: {confidence}/100\\n"
            f"Entry: ${entry_px:.3f} x {shares:.0f} shares\\n"
            f"Cost: <b>${cost:.2f}</b>\\n"
            f"{BAR}\\n"
            f"Current price: ${cur_px:.3f}\\n"
            f"Current value: ${cur_val:.2f}\\n"
            f"Unrealized PnL: <b>{unreal_pnl:+.2f}</b> {_ico(unreal_pnl)}\\n"
            f"{BAR}\\n"
            f"{delta_str}"
            f"Price to beat: ${ptb:,.2f}\\n"
            f"Closes in: <b>{secs}s</b>\\n"
            f"{BAR}\\n"
            f"If WIN:  +${max_profit:.2f} (shares x $1.00)\\n"
            f"If LOSS: -${cost:.2f}\\n"
            f"{BAR}\\n"
            f"{order_line}"
            f"{tx_line}"
            f"Condition: <code>{condition_id[:20] if condition_id else 'pending'}</code>\\n"
            + (f"Market: {market_lnk}" if market_lnk else "")
        )

        kb = {"inline_keyboard": [
            [{"text": "\U0001f504 Refresh", "callback_data": "nav:position"}],
            [{"text": "\U0001f6aa Exit Position", "callback_data": "nav:exit"}],
            [{"text": "\U0001f3e0 Dashboard", "callback_data": "nav:dashboard"}],
        ]}
        await self.notifier.send_text(text, chat_id=chat_id, reply_markup=kb)

    async def _cmd_exit(self, args, chat_id):
        """Exit/close current position by selling tokens back on CLOB."""
        bot = self.trading_bot
        if not bot or not bot.state.entered_this_window or not bot.state.entry_record:
            await self.notifier.send_text(
                "\U0001f6aa No open position to exit.", chat_id=chat_id
            )
            return

        rec = bot.state.entry_record
        side = rec.get("side", "?")
        token_id = rec.get("token_id", "")
        shares = rec.get("shares", 0)
        cost = rec.get("cost", 0)
        entry_px = rec.get("entry_price", 0)

        if not token_id:
            await self.notifier.send_text(
                "\U0001f6aa Cannot exit: no token_id in position.", chat_id=chat_id
            )
            return

        best_bid = 0.0
        if side == "UP" and bot.state.token_up_price:
            best_bid = max(0.01, bot.state.token_up_price - 0.01)
        elif side == "DOWN" and bot.state.token_down_price:
            best_bid = max(0.01, bot.state.token_down_price - 0.01)
        else:
            best_bid = max(0.01, entry_px - 0.02)

        await self.notifier.send_text(
            f"\U0001f6aa <b>CLOSING POSITION...</b>\\n"
            f"Selling {shares:.0f} {side} shares @ ${best_bid:.3f}",
            chat_id=chat_id,
        )

        try:
            client = self.executor._init_client()
            if client is None:
                await self.notifier.send_text(
                    "\u274c CLOB client unavailable.", chat_id=chat_id
                )
                return

            from py_clob_client.clob_types import OrderArgs, OrderType

            def _sell():
                order_args = OrderArgs(
                    price=round(best_bid, 2),
                    size=int(shares),
                    side="SELL",
                    token_id=token_id,
                )
                signed = client.create_order(order_args)
                return client.post_order(signed, OrderType.GTC)

            resp = await asyncio.to_thread(_sell)

            if isinstance(resp, dict) and resp.get("orderID"):
                sell_pnl = (best_bid - entry_px) * shares
                await asyncio.sleep(3)

                bal_str = ""
                addr = config.POLYGON_PUBLIC_KEY or ""
                if addr.startswith("0x") and len(addr) == 42:
                    try:
                        usdc_bal, _, _ = await fetch_all_usdc(addr)
                        bal_str = f"\\n\U0001f4b5 USDC.e: <b>${usdc_bal:,.2f}</b>"
                    except Exception:
                        pass

                await self.notifier.send_text(
                    f"\U0001f6aa <b>POSITION CLOSED</b>\\n"
                    f"{BAR}\\n"
                    f"Sold: {shares:.0f} {side} @ ${best_bid:.3f}\\n"
                    f"Entry: ${entry_px:.3f} | Exit: ${best_bid:.3f}\\n"
                    f"PnL: <b>{sell_pnl:+.2f}</b>\\n"
                    f"Order: <code>{resp['orderID'][:20]}</code>"
                    f"{bal_str}",
                    chat_id=chat_id,
                )
                bot.state.entered_this_window = True
                bot.state.entry_record = None
            else:
                err = resp.get("errorMsg") or resp.get("error") or str(resp)
                await self.notifier.send_text(
                    f"\u274c Exit failed: {str(err)[:100]}", chat_id=chat_id
                )
        except Exception as exc:
            await self.notifier.send_text(
                f"\u274c Exit error: {str(exc)[:100]}", chat_id=chat_id
            )

    async def _cmd_risk(self, args, chat_id):'''
    ),
])

# Verify
import py_compile
try:
    py_compile.compile('utils/telegram.py', doraise=True)
    print("  Syntax OK!")
except py_compile.PyCompileError as e:
    print(f"  SYNTAX ERROR: {e}")
    sys.exit(1)

print("\n=== DONE! Run: pm2 restart polymarket-bot ===")
